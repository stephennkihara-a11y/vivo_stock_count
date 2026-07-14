from odoo import _, SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


SECTION_STATES = [
    ("draft", "Draft"),
    ("scanning", "Scanning"),
    # Two-party flow, reconciled at STORE level: the scanner scans a rack and
    # finishes it, which leaves the rack "scanned, awaiting store reconcile"
    # (pending_review) — there is NO separate second-person approval. A
    # DIFFERENT auditor then reviews, reconciles the WHOLE store in one pass
    # (vivo.count.session.action_reconcile_session) and applies to stock.
    ("pending_review", "Scanned — Awaiting Store Reconcile"),
    ("reconciled", "Reconciled"),
    # A rack still unapproved when the store is reconciled (below the 100%
    # threshold) is left out of that reconcile and flagged here — not counted,
    # excluded from the GL post and the reconciliation report.
    ("excluded", "Left Out — Not Counted"),
    # Legacy states retained only for data compatibility. The two-party flow
    # never enters them; a migration moves any live `physical_review` rows to
    # `pending_review` (the old second-person approval step is removed).
    ("physical_review", "Physical Review (legacy)"),
    ("physical_count", "Physical Count (legacy)"),
    ("variance_rescan", "Variance Re-scan (legacy)"),
]


class VivoCountSection(models.Model):
    _name = "vivo.count.section"
    _description = "Vivo Count Section (Rack)"
    _order = "session_id, zone_id, sequence, name"

    name = fields.Char(required=True)
    session_id = fields.Many2one(
        "vivo.count.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    zone_id = fields.Many2one("vivo.count.zone", required=True)
    template_id = fields.Many2one("vivo.count.section.template", ondelete="set null")
    sequence = fields.Integer(default=10)

    state = fields.Selection(SECTION_STATES, default="draft", required=True, copy=False)

    scanner_id = fields.Many2one("res.users", string="Scanner")
    # Legacy field, kept for data/report continuity with prior flows. The
    # two-party flow has NO per-rack approver, so it is never stamped going
    # forward; segregation of duties now lives at the session level
    # (auditor != scanner — vivo.count.session._check_reviewer_not_scanner).
    physical_counter_id = fields.Many2one("res.users", string="Approver (legacy)")

    line_ids = fields.One2many("vivo.count.line", "section_id", string="SKU Lines")

    # Option 2 — counted vs. uncounted split. A section loads every store SKU,
    # but only the ones physically scanned on this rack (counted_qty > 0) are
    # "counted". The rest are "not counted here" — pending on other racks, not
    # variances — and are kept out of the reconcile/variance path.
    # Kept in separate compute methods on purpose: a non-stored One2many and
    # stored Integers cannot share one compute method (Odoo requires a
    # consistent `store`/`compute_sudo` across fields computed together).
    not_counted_line_ids = fields.One2many(
        "vivo.count.line",
        "section_id",
        compute="_compute_not_counted_lines",
        string="Not Counted Here",
    )
    counted_line_count = fields.Integer(compute="_compute_line_counts", store=True)
    not_counted_line_count = fields.Integer(compute="_compute_line_counts", store=True)

    scan_total_qty = fields.Float(
        compute="_compute_totals", store=True, digits="Product Unit of Measure",
        help="Sum of counted quantities on this rack. Only counted lines "
             "contribute — 'not counted here' lines are zero by definition.",
    )
    physical_total_qty = fields.Float(
        string="Physical Count",
        digits="Product Unit of Measure",
        help="Independent headcount from the physical counter. Enter it here "
             "(desktop) or via the mobile PWA, then Submit Physical Count. The "
             "section reconciles automatically when this equals the scan total.",
    )
    is_reconciled = fields.Boolean(compute="_compute_is_reconciled", store=True)

    rescan_count = fields.Integer(default=0, readonly=True, copy=False)
    reconciled_at = fields.Datetime(readonly=True, copy=False)
    reconciled_by_id = fields.Many2one(
        "res.users",
        string="Reconciled By",
        readonly=True,
        copy=False,
        help="Auditor who confirmed the reconciliation from Pending Review. "
             "Empty when the section auto-reconciled (no genuine variance).",
    )

    # Auditor force-reconcile of a persistent scan-vs-physical disagreement.
    # When the counters cannot agree even after re-scanning, an auditor sets an
    # authoritative physical count and records why; the section reconciles even
    # though scan_total_qty != physical_total_qty.
    force_reconciled = fields.Boolean(
        readonly=True,
        copy=False,
        help="Set when an auditor reconciled this section despite the scan and "
             "physical counts still disagreeing.",
    )
    force_reconcile_reason = fields.Text(
        string="Auditor Reconciliation Reason",
        readonly=True,
        copy=False,
        help="Why the auditor force-reconciled a persistent scan-vs-physical mismatch.",
    )
    # Mandatory note the reviewer records when reconciling a section in the
    # approve-then-review flow (there is no independent physical count to match,
    # so a recorded variance note is the integrity gate for reconciliation).
    review_note = fields.Text(
        string="Reviewer Variance Note",
        readonly=True,
        copy=False,
        help="Mandatory note the manager/auditor records when reconciling.",
    )

    # Soft-lock: the scanner currently working this section. Released after
    # `vivo_count.section_lock_minutes` of inactivity.
    locked_by_id = fields.Many2one("res.users", string="In Progress — User", copy=False)
    locked_at = fields.Datetime(copy=False)

    # PWA physical-submit idempotency: stores the last accepted key so
    # offline replay does not double-submit (AC #8).
    last_physical_idem_key = fields.Char(readonly=True, copy=False)

    # Append-only audit trail for reject-and-recount. A recount is destructive
    # (every scanned line is wiped), so who did it and when is recorded here so
    # the wipe is never invisible. Never cleared, even across repeated recounts.
    recount_log = fields.Text(
        string="Recount Audit Log",
        readonly=True,
        copy=False,
        help="Who rejected & recounted this rack, when, and how many lines were "
             "cleared each time. Append-only — the destructive wipe is logged so "
             "it leaves a trace.",
    )

    has_unresolved_no_barcode = fields.Boolean(compute="_compute_no_barcode")

    _sql_constraints = [
        (
            "rescan_non_negative",
            "CHECK (rescan_count >= 0)",
            "Rescan count cannot be negative.",
        ),
    ]

    # ------------------------------------------------------------------
    # CRUD guards — force-reconcile is a manager/auditor-only privilege
    # ------------------------------------------------------------------
    # `force_reconciled` is what lets a section land in `reconciled` while
    # scan_total_qty != physical_total_qty (see `_check_reconcile_match`), i.e.
    # it overrides the two-counter integrity check. The store-level reconcile
    # (`vivo.count.session.action_reconcile_session`) already gates that, but the
    # counter record rule grants counters write access to sections in an
    # in-progress session — so a plain ORM write/create of these fields must be
    # blocked here too, or the method gate is trivially bypassable.
    _FORCE_FIELDS = ("force_reconciled", "force_reconcile_reason")

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            vals.get(f) for vals in vals_list for f in self._FORCE_FIELDS
        ):
            self._check_auditor_band()
        return super().create(vals_list)

    def write(self, vals):
        # Force-reconcile fields stay manager-only (preserves the earlier
        # security fix). The approve-then-review flow makes EVERY reconcile a
        # manager decision, so moving a section into `reconciled` — or leaving it
        # out via `excluded` at store reconcile — by any write path is gated
        # here, closing the direct-write bypass the counter record rule would
        # otherwise leave open.
        if (
            any(vals.get(f) for f in self._FORCE_FIELDS)
            or vals.get("state") in ("reconciled", "excluded")
        ):
            self._check_auditor_band()
        return super().write(vals)

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("line_ids.counted_qty")
    def _compute_totals(self):
        for section in self:
            section.scan_total_qty = sum(section.line_ids.mapped("counted_qty"))

    @api.depends("line_ids.line_status")
    def _compute_not_counted_lines(self):
        for section in self:
            section.not_counted_line_ids = section.line_ids.filtered(
                lambda l: l.line_status == "not_counted"
            )

    @api.depends("line_ids.line_status")
    def _compute_line_counts(self):
        for section in self:
            not_counted = len(
                section.line_ids.filtered(lambda l: l.line_status == "not_counted")
            )
            section.not_counted_line_count = not_counted
            section.counted_line_count = len(section.line_ids) - not_counted

    @api.depends("state")
    def _compute_is_reconciled(self):
        # Reconciliation is now a reviewer decision (approve-then-review), not a
        # scan==physical match, so the state alone is authoritative.
        for section in self:
            section.is_reconciled = section.state == "reconciled"

    @api.depends("line_ids.no_barcode_flag", "line_ids.no_barcode_resolved")
    def _compute_no_barcode(self):
        for section in self:
            section.has_unresolved_no_barcode = any(
                l.no_barcode_flag and not l.no_barcode_resolved
                for l in section.line_ids
            )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def action_start_scanning(self):
        """draft -> scanning (or variance_rescan -> scanning)."""
        for section in self:
            if section.state not in {"draft", "variance_rescan"}:
                raise UserError(
                    _("Section %s is in state %s; cannot start scanning.")
                    % (section.name, section.state)
                )
            if not section.scanner_id:
                section.scanner_id = self.env.user
            section.write(
                {
                    "state": "scanning",
                    "locked_by_id": self.env.user.id,
                    "locked_at": fields.Datetime.now(),
                }
            )
        return True

    def action_finish_scanning(self):
        """scanning -> pending_review ("scanned, awaiting store reconcile").

        Two-party flow: the scanner finishing a rack makes it ready for the
        auditor's store reconcile. There is NO separate second-person approval —
        the scanner scanning the rack is enough to make it ready.
        """
        for section in self:
            if section.state != "scanning":
                raise UserError(_("Section %s is not in scanning state.") % section.name)
            if section.has_unresolved_no_barcode:
                raise UserError(
                    _("Section %s has unresolved 'no barcode' lines.") % section.name
                )
            section.write(
                {"state": "pending_review", "locked_by_id": False, "locked_at": False}
            )
        return True

    # ------------------------------------------------------------------
    # Recount gate — Finish Scanning when physical count != scanned total
    # ------------------------------------------------------------------
    def _counts_match(self):
        """True when the manual Physical Count equals the scanned total.

        Compares ``physical_total_qty`` (manual entry) against
        ``scan_total_qty`` (sum of counted_qty). Neither is changed here —
        the gate is a read-only comparison used by both surfaces on Finish.
        """
        self.ensure_one()
        return self.physical_total_qty == self.scan_total_qty

    def action_finish_scanning_gate(self):
        """Desktop Finish Scanning entry point.

        If the Physical Count matches the scanned total, finish exactly as
        ``action_finish_scanning`` does (advance to pending_review). If they
        DISAGREE, open the recount-gate wizard — a red alert with two choices
        (Proceed and accept the discrepancy, or Reject & recount) — instead of
        finishing silently.
        """
        self.ensure_one()
        if self.state != "scanning":
            raise UserError(_("Section %s is not in scanning state.") % self.name)
        if self._counts_match():
            return self.action_finish_scanning()
        return {
            "type": "ir.actions.act_window",
            "name": _("Rack Count Mismatch"),
            "res_model": "vivo.count.recount.gate.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_section_id": self.id},
        }

    def action_reject_and_recount(self):
        """Wipe EVERY scanned line on this rack and send it back to 'scanning'
        for a full rescan. Destructive by design — the caller's confirmation and
        the ``recount_log`` audit entry are the safeguards; there is no undo.

        Allowed only while the rack is still pre-reconcile (scanning or the
        just-finished submit state, pending_review). NEVER once the rack has been
        reconciled / excluded — that would erase data already counted toward the
        store. Enforced server-side so no UI path can bypass it. Atomic: the
        state reset and the line wipe happen in one transaction.

        Any counter may trigger it (no role restriction) — it is a deliberate
        recount, not a stealth edit.
        """
        self.ensure_one()
        RECOUNTABLE = {"scanning", "pending_review"}
        if self.state not in RECOUNTABLE:
            raise UserError(
                _(
                    "Rack %(rack)s can no longer be recounted (state: %(state)s). "
                    "A reconciled or excluded rack is locked."
                )
                % {"rack": self.name, "state": self.state}
            )
        cleared = len(self.line_ids)
        # Flip to 'scanning' FIRST so the shared unlink() guard (which only
        # permits deletion while scanning) allows this deliberate wipe.
        self.write(
            {
                "state": "scanning",
                "locked_by_id": self.env.user.id,
                "locked_at": fields.Datetime.now(),
            }
        )
        if self.line_ids:
            self.line_ids.unlink()
        # Clear the stale physical count + idempotency key so the rescan starts
        # from a clean slate (scan_total_qty recomputes to 0 from the empty rack).
        self.write({"physical_total_qty": 0.0, "last_physical_idem_key": False})
        self._log_recount(cleared)
        return cleared

    def _log_recount(self, cleared):
        """Append a timestamped audit line recording the destructive wipe."""
        self.ensure_one()
        entry = _(
            "Rack rejected & recounted by %(user)s at %(time)s, "
            "%(n)s line(s) cleared."
        ) % {
            "user": self.env.user.name,
            "time": fields.Datetime.to_string(fields.Datetime.now()),
            "n": cleared,
        }
        self.recount_log = (
            (self.recount_log + "\n" + entry) if self.recount_log else entry
        )

    def action_submit_physical_count(self, physical_qty=None):
        """physical_count -> pending_review | reconciled | variance_rescan.

        On a scan-vs-physical match the section routes to `pending_review`
        for the store-level reconcile (see
        `vivo.count.session.action_reconcile_session`). If the
        `vivo_count.auto_close_zero_variance` toggle is on (default) and the
        section carries no genuine variance to audit, it reconciles
        automatically, skipping the review step. A mismatch still routes to
        `variance_rescan` unchanged.

        Legacy path (old independent dual-count); the two-party flow never
        reaches `physical_count`.
        """
        for section in self:
            if section.state != "physical_count":
                raise UserError(
                    _("Section %s is not awaiting physical count.") % section.name
                )
            if not section.physical_counter_id:
                section.physical_counter_id = self.env.user
            if physical_qty is not None:
                section.physical_total_qty = physical_qty
            if section.scan_total_qty == section.physical_total_qty:
                if (
                    section._auto_close_zero_variance_enabled()
                    and not section._review_variance_lines()
                ):
                    # No genuine variance -> reconcile automatically (no auditor).
                    section._do_reconcile(reconciled_by=False)
                else:
                    # Genuine variance -> hold for auditor review.
                    section.write({"state": "pending_review"})
            else:
                # Scan and physical disagree. Allow a bounded re-scan loop, but
                # a persistent disagreement must not loop forever nor auto-
                # reconcile — after the threshold it escalates to the auditor.
                new_rescan = section.rescan_count + 1
                if new_rescan > section._rescan_review_threshold():
                    section.write({"state": "pending_review", "rescan_count": new_rescan})
                else:
                    section.write({"state": "variance_rescan", "rescan_count": new_rescan})
        return True

    def _rescan_review_threshold(self):
        val = self.env["ir.config_parameter"].sudo().get_param(
            "vivo_count.rescan_review_threshold", "1"
        )
        try:
            return int(val)
        except (TypeError, ValueError):
            return 1

    # ------------------------------------------------------------------
    # Auditor-confirmed reconciliation
    # ------------------------------------------------------------------
    def _review_variance_lines(self):
        """Counted lines with a genuine count-vs-system variance to audit.

        A line qualifies when the system expected a quantity
        (``system_qty`` set) and the count differs. Pure rack scans created
        by the PWA carry ``system_qty`` 0 (the snapshot lives in the
        catch-all section), so they are not treated as section-level
        variances here — the real per-product reconciliation of those
        happens at session Apply, and a genuine shortage is still caught by
        ``vivo.count.session._check_variance_reasons`` at approval.

        NOTE: this is a deliberate coupling to the snapshot design in
        ``vivo.count.session._snapshot_system_quantities`` (baseline written
        only to ``section_ids[:1]``). If snapshotting ever becomes per-rack —
        populating ``system_qty`` on each rack's scanned lines — every matched
        rack section would start routing through review instead of
        auto-closing. The boundary is pinned by
        ``test_section_review.test_pwa_zero_baseline_scan_auto_closes`` and
        ``test_match_with_variance_goes_to_pending_review``; revisit both if
        that design changes.
        """
        self.ensure_one()
        return self.line_ids.filtered(
            lambda l: l.line_status == "counted"
            and l.system_qty
            and l.difference != 0.0
        )

    def _auto_close_zero_variance_enabled(self):
        val = self.env["ir.config_parameter"].sudo().get_param(
            "vivo_count.auto_close_zero_variance", "True"
        )
        return str(val).strip().lower() not in ("false", "0", "")

    def _do_reconcile(self, reconciled_by=None):
        """Transition to reconciled, stamping the audit trail, then let the
        session auto-advance. ``reconciled_by`` is the confirming auditor, or
        False for an automatic zero-variance close."""
        for section in self:
            section.write(
                {
                    "state": "reconciled",
                    "reconciled_at": fields.Datetime.now(),
                    "reconciled_by_id": reconciled_by.id if reconciled_by else False,
                }
            )
            section.session_id._maybe_auto_advance_to_counted()
        return True

    def _check_counted_variance_reasons(self):
        """Per-line variance reasons/notes are OPTIONAL and never block reconcile.

        The old requirement (every counted variance line needs a reason, and a
        note for reason 'Other') has been removed. The fields stay visible and
        fillable, just not mandatory. The mandatory store-level reviewer note is
        a separate control enforced in ``_reconcile_sections`` — that gate is
        unchanged. Kept as a no-op so the reconcile call site is stable.
        """
        return

    def _check_auditor_band(self):
        """Raise unless the current user is a manager/auditor band.

        Store Manager, Regional Manager and CFOO/Audit may confirm and
        force-reconcile; a plain Counter may not — they cannot override the
        two-counter integrity guarantee. Checked explicitly against all three
        groups (not via implication) so the gate holds even if the group
        hierarchy changes. The superuser bypasses it (setup / automation).
        """
        if self.env.uid == SUPERUSER_ID:
            return
        user = self.env.user
        if not (
            user.has_group("vivo_stock_count.group_vivo_count_store_manager")
            or user.has_group("vivo_stock_count.group_vivo_count_regional")
            or user.has_group("vivo_stock_count.group_vivo_count_cfoo_audit")
        ):
            raise AccessError(
                _(
                    "Only a Store Manager, Regional Manager or CFOO/Audit user "
                    "can confirm or force-reconcile a section. Counters cannot "
                    "override the two-counter check."
                )
            )

    def _reconcile_sections(self, review_note, reconciled_by):
        """Reconcile approved racks — the internal worker behind the store-level
        reconcile (``vivo.count.session.action_reconcile_session``).

        Reconciliation is no longer a per-rack action: there is no rack-level
        button. The session reconcile calls this on every APPROVED rack
        (``pending_review``) in one transaction, after it has checked the
        auditor band and the configured approval threshold. Each rack still
        passes the per-line variance-reason gate and records the mandatory
        reviewer note; the band is re-enforced on the ``reconciled`` write
        (see ``write``), so this stays safe even if called directly.
        """
        if not review_note:
            raise ValidationError(
                _("A variance/review note is required to reconcile.")
            )
        for section in self:
            if section.state != "pending_review":
                raise UserError(
                    _("Section %s is not awaiting store reconcile.") % section.name
                )
            section._check_counted_variance_reasons()
            section.write({"review_note": review_note})
            section._do_reconcile(reconciled_by=reconciled_by)
        return True

    def _bounce_from_review(self):
        """Manager-driven bounce from review back to scanning.

        Per A3: rescan_count increments, counted_qty wiped on all lines,
        but scan history is preserved on `vivo.count.scan.event`. Manager
        may reassign scanner/physical_counter afterwards.
        """
        for section in self:
            section.line_ids.write({"counted_qty": 0.0, "variance_reason": False, "variance_note": False})
            section.write(
                {
                    "state": "scanning",
                    "physical_total_qty": 0.0,
                    "rescan_count": section.rescan_count + 1,
                    "reconciled_at": False,
                    "reconciled_by_id": False,
                    "force_reconciled": False,
                    "force_reconcile_reason": False,
                    "locked_by_id": False,
                    "locked_at": False,
                }
            )
        return True

    # ------------------------------------------------------------------
    # Soft locking (Phase 1 minimum; PWA enforcement in Phase 3)
    # ------------------------------------------------------------------
    def acquire_lock(self):
        """Attempt to soft-lock this section to the current user (AC #14)."""
        self.ensure_one()
        Param = self.env["ir.config_parameter"].sudo()
        lock_minutes = int(Param.get_param("vivo_count.section_lock_minutes", "30"))
        from datetime import timedelta

        now = fields.Datetime.now()
        if self.locked_by_id and self.locked_by_id != self.env.user:
            if self.locked_at and (now - self.locked_at) < timedelta(minutes=lock_minutes):
                raise UserError(
                    _("Section %(name)s is in progress with %(user)s.")
                    % {"name": self.name, "user": self.locked_by_id.name}
                )
        self.write({"locked_by_id": self.env.user.id, "locked_at": now})
        return True

    def release_lock(self):
        self.write({"locked_by_id": False, "locked_at": False})
        return True

    # Segregation of duties in the two-party flow is enforced at the SESSION
    # level (the auditor who reviews/reconciles/applies must differ from the
    # scanner — vivo.count.session._check_reviewer_not_scanner), not per rack.
    # The old rack-level scanner!=approver constraint is gone with the approver.

    # ------------------------------------------------------------------
    # PWA API (Phase 3)
    # ------------------------------------------------------------------
    @api.model
    def list_for_pwa(self, session_id):
        """Return the section list a counter needs to drive the PWA UI."""
        sections = self.search(
            [("session_id", "=", session_id)], order="zone_id, sequence, name"
        )
        return [
            {
                "id": s.id,
                "name": s.name,
                "zone_id": s.zone_id.id,
                "zone_name": s.zone_id.name,
                "state": s.state,
                "rescan_count": s.rescan_count,
                "scan_total_qty": s.scan_total_qty,
                "physical_total_qty": s.physical_total_qty,
                "scanner_id": s.scanner_id.id,
                "scanner_name": s.scanner_id.name or "",
                "physical_counter_id": s.physical_counter_id.id,
                "physical_counter_name": s.physical_counter_id.name or "",
                "locked_by_id": s.locked_by_id.id,
                "locked_by_name": s.locked_by_id.name or "",
                "is_mine": s.locked_by_id.id == self.env.uid
                or s.scanner_id.id == self.env.uid,
            }
            for s in sections
        ]

    def open_for_scanning(self):
        """Atomic: acquire soft-lock + transition to scanning state.

        Used by the PWA when a scanner picks a rack. If a different user
        already holds the lock within the idle window, raises UserError
        (AC #14). The same user re-opening is a no-op.
        """
        self.ensure_one()
        self.acquire_lock()
        if self.state in {"draft", "variance_rescan"}:
            self.action_start_scanning()
        elif self.state == "scanning":
            # Already scanning, ensure scanner_id is current.
            if not self.scanner_id:
                self.scanner_id = self.env.uid
        else:
            raise UserError(
                _("Section %s is not available for scanning (state: %s).")
                % (self.name, self.state)
            )
        return {
            "id": self.id,
            "state": self.state,
            "scanner_id": self.scanner_id.id,
            "scan_total_qty": self.scan_total_qty,
        }

    def finish_scanning_pwa(self, force=False):
        """Scanner finishes the rack. If the manual Physical Count matches the
        scanned total (or ``force`` is set — the counter chose Proceed on the
        mismatch alert), advance to pending_review as the two-party flow does.

        On a mismatch with ``force`` unset, DO NOT advance: return a payload the
        PWA uses to raise its red alert with the two choices (Proceed / Reject &
        recount). No second-person approval in the two-party flow.
        """
        self.ensure_one()
        if self.state != "scanning":
            # Nothing to gate — already advanced or not scanning; report state.
            return {"id": self.id, "state": self.state, "mismatch": False}
        if not force and not self._counts_match():
            return {
                "id": self.id,
                "state": self.state,
                "mismatch": True,
                "scan_total_qty": self.scan_total_qty,
                "physical_total_qty": self.physical_total_qty,
                "line_count": len(self.line_ids),
            }
        self.action_finish_scanning()
        return {"id": self.id, "state": self.state, "mismatch": False}

    def reject_and_recount_pwa(self):
        """Reject the rack and wipe it for a full rescan (PWA). Delegates to the
        guarded ``action_reject_and_recount`` and reports the cleared count and
        the rack's fresh 'scanning' state so the PWA can reopen the empty rack."""
        self.ensure_one()
        cleared = self.action_reject_and_recount()
        return {"id": self.id, "state": self.state, "cleared": cleared}

    def submit_physical_pwa(self, physical_qty, idempotency_key=None):
        """Physical counter submits their independent headcount.

        Idempotent: if `idempotency_key` matches the last accepted key, the
        submission is treated as a replay and current state is returned
        without re-applying. Supports Phase 3 offline drain.
        """
        self.ensure_one()
        if idempotency_key and self.last_physical_idem_key == idempotency_key:
            return {
                "id": self.id,
                "state": self.state,
                "idempotent": True,
                "scan_total_qty": self.scan_total_qty,
                "physical_total_qty": self.physical_total_qty,
                "is_reconciled": self.is_reconciled,
            }
        self.action_submit_physical_count(physical_qty=physical_qty)
        if idempotency_key:
            self.last_physical_idem_key = idempotency_key
        return {
            "id": self.id,
            "state": self.state,
            "scan_total_qty": self.scan_total_qty,
            "physical_total_qty": self.physical_total_qty,
            "is_reconciled": self.is_reconciled,
        }

    @api.constrains("state", "review_note", "force_reconcile_reason")
    def _check_reconcile_requires_note(self):
        """Approve-then-review integrity gate: a section may only be
        `reconciled` once a reviewer has recorded the mandatory variance note.

        Replaces the old scan==physical match — there is no longer an
        independent physical count. The legacy force_reconcile_reason also
        satisfies the gate for any section reconciled via that path.
        """
        for section in self:
            if section.state == "reconciled" and not (
                section.review_note or section.force_reconcile_reason
            ):
                raise ValidationError(
                    _(
                        "Section %s cannot be reconciled without a reviewer's "
                        "variance note."
                    )
                    % section.name
                )
