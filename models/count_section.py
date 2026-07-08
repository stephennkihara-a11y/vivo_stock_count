from odoo import _, SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


SECTION_STATES = [
    ("draft", "Draft"),
    ("scanning", "Scanning"),
    # Approve-then-review flow: the scanner scans, a DIFFERENT second person
    # approves (or rejects) the scanned result, then a manager/auditor reviews,
    # records a variance note, and reconciles.
    ("physical_review", "Physical Review"),
    ("pending_review", "Pending Review"),
    ("reconciled", "Reconciled"),
    # Legacy states retained only for data compatibility with sessions counted
    # under the old independent-dual-count flow. The new flow never enters them.
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
    # The second person in the approve-then-review flow: they APPROVE the
    # scanned result (they do not independently re-count). Must differ from the
    # scanner — enforced by _check_segregation_of_duties. Field name kept for
    # data/report continuity with the prior dual-count flow.
    physical_counter_id = fields.Many2one("res.users", string="Approver (2nd person)")

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
    # it overrides the two-counter integrity check. `action_confirm_reconcile`
    # already gates that, but the counter record rule grants counters write
    # access to sections in an in-progress session — so a plain ORM
    # write/create of these fields must be blocked here too, or the method
    # gate is trivially bypassable.
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
        # manager decision, so moving a section into `reconciled` by any write
        # path is also gated here — closing the direct-write bypass the counter
        # record rule would otherwise leave open.
        if any(vals.get(f) for f in self._FORCE_FIELDS) or vals.get("state") == "reconciled":
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
        """scanning -> physical_review (the second person's approve/reject stage)."""
        for section in self:
            if section.state != "scanning":
                raise UserError(_("Section %s is not in scanning state.") % section.name)
            if section.has_unresolved_no_barcode:
                raise UserError(
                    _("Section %s has unresolved 'no barcode' lines.") % section.name
                )
            section.write({"state": "physical_review", "locked_by_id": False, "locked_at": False})
        return True

    def action_approve_scan(self):
        """physical_review -> pending_review.

        The second person approves the scanned result (they do NOT re-count).
        They are stamped as the approver (physical_counter_id); segregation of
        duties (approver != scanner) is enforced by
        `_check_segregation_of_duties` on that write.
        """
        for section in self:
            if section.state != "physical_review":
                raise UserError(
                    _("Section %s is not awaiting approval.") % section.name
                )
            section.write(
                {
                    "physical_counter_id": self.env.user.id,
                    "state": "pending_review",
                }
            )
        return True

    def action_reject_scan(self):
        """physical_review -> scanning (reject; the scanner re-scans).

        Increments rescan_count and clears the pending approval. The scanned
        lines are preserved so the scanner can correct rather than start over
        (a manager bounce, `_bounce_from_review`, is the wipe-and-redo path).
        """
        for section in self:
            if section.state != "physical_review":
                raise UserError(
                    _("Section %s is not awaiting approval.") % section.name
                )
            if (
                section.scanner_id
                and self.env.user == section.scanner_id
                and self.env.uid != SUPERUSER_ID
            ):
                raise ValidationError(
                    _("The scanner cannot reject their own scan (section %s).")
                    % section.name
                )
            section.write(
                {
                    "state": "scanning",
                    "physical_counter_id": False,
                    "rescan_count": section.rescan_count + 1,
                    "locked_by_id": False,
                    "locked_at": False,
                }
            )
        return True

    def action_submit_physical_count(self, physical_qty=None):
        """physical_count -> pending_review | reconciled | variance_rescan.

        On a scan-vs-physical match the section routes to `pending_review`
        for auditor sign-off (see `action_confirm_reconcile`). If the
        `vivo_count.auto_close_zero_variance` toggle is on (default) and the
        section carries no genuine variance to audit, it reconciles
        automatically, skipping the review step. A mismatch still routes to
        `variance_rescan` unchanged.

        Required-different-user enforced by `_check_segregation_of_duties`.
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
        """Every counted line with a variance needs a reason (and a note when
        the reason is 'Other') before the section can be reconciled."""
        self.ensure_one()
        counted = self.line_ids.filtered(lambda l: l.line_status == "counted")
        # Only lines with a real system baseline (system_qty set) can carry a
        # section-level variance to reason for. Pure PWA rack scans have
        # system_qty 0 (the snapshot lives in the catch-all section), so they
        # are not treated as per-line variances here — consistent with
        # `_review_variance_lines`.
        missing = counted.filtered(
            lambda l: l.system_qty and l.difference != 0.0 and not l.variance_reason
        )
        if missing:
            raise ValidationError(
                _(
                    "Section %(name)s cannot be reconciled — these counted lines "
                    "have a variance but no reason: %(lines)s"
                )
                % {
                    "name": self.name,
                    "lines": ", ".join(missing.mapped("product_id.display_name")),
                }
            )
        bad_other = counted.filtered(
            lambda l: l.variance_reason == "other" and not l.variance_note
        )
        if bad_other:
            raise ValidationError(
                _(
                    "Section %(name)s: lines with reason 'Other' need a note: "
                    "%(lines)s"
                )
                % {
                    "name": self.name,
                    "lines": ", ".join(bad_other.mapped("product_id.display_name")),
                }
            )

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

    def action_confirm_reconcile(self, review_note=None, physical_qty=None, force_reason=None):
        """pending_review -> reconciled, stamping reconciled_by_id.

        Approve-then-review flow: the reviewer must be a manager/auditor band
        (`_check_auditor_band`) and must record a MANDATORY variance note. There
        is no independent physical count to match — the recorded note is the
        integrity gate (`_check_reconcile_requires_note`).

        Legacy force path preserved: if an authoritative ``physical_qty`` is
        supplied and differs from the scan total, the override is recorded via
        ``force_reconciled`` / ``force_reconcile_reason`` (whose write is itself
        band-guarded), keeping that control intact.
        """
        for section in self:
            if section.state != "pending_review":
                raise UserError(
                    _("Section %s is not pending review.") % section.name
                )
            section._check_auditor_band()
            note = review_note or force_reason
            if not note:
                raise ValidationError(
                    _(
                        "Section %s: a variance note is required to reconcile."
                    )
                    % section.name
                )
            vals = {"review_note": note}
            if physical_qty is not None:
                section.physical_total_qty = physical_qty
            if section.physical_total_qty and section.scan_total_qty != section.physical_total_qty:
                # Authoritative physical figure differs from the scan — record
                # the override on the audit trail (band-guarded write).
                vals["force_reconciled"] = True
                vals["force_reconcile_reason"] = note
            section.write(vals)
            section._check_counted_variance_reasons()
            section._do_reconcile(reconciled_by=self.env.user)
        return True

    def action_open_section_review_wizard(self):
        """Open the Review & Reconcile wizard for a pending-review section."""
        self.ensure_one()
        if self.state != "pending_review":
            raise UserError(_("Section %s is not pending review.") % self.name)
        return {
            "type": "ir.actions.act_window",
            "name": _("Review & Reconcile — %s") % self.name,
            "res_model": "vivo.count.section.review.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_section_id": self.id},
        }

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

    # ------------------------------------------------------------------
    # Constraints — segregation of duties (AC #3)
    # ------------------------------------------------------------------
    @api.constrains("scanner_id", "physical_counter_id")
    def _check_segregation_of_duties(self):
        for section in self:
            if (
                section.scanner_id
                and section.physical_counter_id
                and section.scanner_id == section.physical_counter_id
            ):
                raise ValidationError(
                    _(
                        "Section %s: the scanner and the physical counter must be "
                        "two different users. No one can verify their own scan."
                    )
                    % section.name
                )

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

    def finish_scanning_pwa(self):
        self.ensure_one()
        self.action_finish_scanning()
        return {"id": self.id, "state": self.state}

    def approve_scan_pwa(self, idempotency_key=None):
        """Second person approves the scanned result (physical_review ->
        pending_review). Idempotent for offline replay."""
        self.ensure_one()
        if idempotency_key and self.last_physical_idem_key == idempotency_key:
            return {"id": self.id, "state": self.state, "idempotent": True}
        self.action_approve_scan()
        if idempotency_key:
            self.last_physical_idem_key = idempotency_key
        return {"id": self.id, "state": self.state}

    def reject_scan_pwa(self):
        """Second person rejects the scan (physical_review -> scanning)."""
        self.ensure_one()
        self.action_reject_scan()
        return {"id": self.id, "state": self.state, "rescan_count": self.rescan_count}

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
