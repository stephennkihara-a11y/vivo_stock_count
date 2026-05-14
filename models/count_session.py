from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


SESSION_STATES = [
    ("draft", "Draft"),
    ("in_progress", "In Progress"),
    ("counted", "Counted"),
    ("review", "Review"),
    ("approved", "Approved"),
    ("applied", "Applied"),
    ("cancelled", "Cancelled"),
]

BAND_AUTO = "auto"
BAND_REGIONAL = "regional"
BAND_CFOO = "cfoo"


class VivoCountSession(models.Model):
    _name = "vivo.count.session"
    _description = "Vivo Count Session"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "scheduled_date desc, id desc"

    name = fields.Char(
        string="Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
        tracking=True,
    )
    location_id = fields.Many2one(
        "stock.location",
        string="Store Location",
        required=True,
        domain="[('usage', '=', 'internal')]",
        tracking=True,
    )
    company_id = fields.Many2one(
        "res.company",
        related="location_id.company_id",
        store=True,
        readonly=True,
    )
    currency_id = fields.Many2one(
        "res.currency",
        related="company_id.currency_id",
        readonly=True,
    )
    zone_id = fields.Many2one(
        "vivo.count.zone",
        string="Zone (optional)",
        help=(
            "If set, the session is scoped to a single zone. "
            "Leave empty for a whole-store count spanning multiple zones."
        ),
    )
    category_ids = fields.Many2many(
        "product.category",
        string="Product Categories",
        help="Optional: restrict count scope to these categories.",
    )
    section_ids = fields.One2many(
        "vivo.count.section",
        "session_id",
        string="Rack Sections",
    )
    line_ids = fields.One2many(
        "vivo.count.line",
        "session_id",
        string="Lines",
    )
    reconciliation_id = fields.Many2one(
        "vivo.count.reconciliation",
        string="Reconciliation Report",
        readonly=True,
        copy=False,
    )

    state = fields.Selection(
        SESSION_STATES,
        default="draft",
        required=True,
        copy=False,
        tracking=True,
    )

    sections_total = fields.Integer(compute="_compute_section_counts", store=True)
    sections_reconciled = fields.Integer(compute="_compute_section_counts", store=True)
    sections_outstanding = fields.Integer(compute="_compute_section_counts", store=True)

    scheduled_date = fields.Datetime(default=fields.Datetime.now, tracking=True)
    start_date = fields.Datetime(readonly=True, copy=False)
    end_date = fields.Datetime(readonly=True, copy=False)

    reviewer_id = fields.Many2one("res.users", string="Reviewer (Store Manager)", tracking=True)
    approver_id = fields.Many2one("res.users", string="Approver", tracking=True)
    applied_by_id = fields.Many2one("res.users", string="Applied By", readonly=True, copy=False)

    variance_value = fields.Monetary(
        compute="_compute_variance_value",
        store=True,
        currency_field="currency_id",
        help="Absolute stock variance value across all lines — drives approval band.",
    )
    variance_line_count = fields.Integer(
        compute="_compute_variance_summary", store=True,
    )
    sections_with_variance = fields.Integer(
        compute="_compute_variance_summary", store=True,
    )
    unreasoned_line_count = fields.Integer(
        compute="_compute_variance_summary", store=True,
    )
    tolerance_band = fields.Selection(
        [(BAND_AUTO, "Store Manager"), (BAND_REGIONAL, "Regional Manager"), (BAND_CFOO, "CFOO")],
        compute="_compute_tolerance_band",
        store=True,
    )
    notes = fields.Html()

    # ETA / trading-deadline warning (live progress indicator, Spec 4.3)
    minutes_per_section = fields.Float(
        compute="_compute_eta", help="Rolling avg minutes per reconciled section.",
    )
    estimated_completion = fields.Datetime(compute="_compute_eta")
    is_behind_trading_deadline = fields.Boolean(compute="_compute_eta")
    progress_pct = fields.Float(compute="_compute_eta")

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("section_ids.state")
    def _compute_section_counts(self):
        for session in self:
            sections = session.section_ids
            session.sections_total = len(sections)
            session.sections_reconciled = sum(1 for s in sections if s.state == "reconciled")
            session.sections_outstanding = session.sections_total - session.sections_reconciled

    @api.depends(
        "line_ids.counted_qty",
        "line_ids.system_qty",
        "line_ids.unit_cost",
        "line_ids.section_id.state",
    )
    def _compute_variance_value(self):
        """Sum of absolute variance value across the session.

        Aggregates per product first to avoid double-counting when the
        same SKU appears in multiple sections (e.g. snapshot line in one
        section + scanned lines in others). variance_value is |Σ(counted)
        − Σ(system)| × unit_cost per product, summed across products.
        """
        for session in self:
            by_product = {}
            for line in session.line_ids:
                if line.section_id.state != "reconciled":
                    continue
                pid = line.product_id.id
                d = by_product.setdefault(
                    pid,
                    {"diff": 0.0, "unit_cost": line.unit_cost or 0.0},
                )
                d["diff"] += (line.counted_qty or 0.0) - (line.system_qty or 0.0)
                # Use the latest non-zero unit_cost we see.
                if line.unit_cost:
                    d["unit_cost"] = line.unit_cost
            total = sum(abs(v["diff"] * v["unit_cost"]) for v in by_product.values())
            session.variance_value = total

    @api.depends(
        "line_ids.counted_qty",
        "line_ids.system_qty",
        "line_ids.variance_reason",
        "line_ids.section_id.state",
    )
    def _compute_variance_summary(self):
        """Variance counts at the product level.

        variance_line_count = number of products with a net variance.
        sections_with_variance = number of sections that have at least one
        variance line (still per-line, since manager review is per-line).
        unreasoned_line_count = number of variance LINES (per-line) lacking
        a reason — drives the AC #11 approval block.
        """
        for session in self:
            by_product = {}
            unreasoned_lines = 0
            variance_sections = set()
            for line in session.line_ids:
                if line.section_id.state != "reconciled":
                    continue
                pid = line.product_id.id
                by_product[pid] = by_product.get(pid, 0.0) + (
                    (line.counted_qty or 0.0) - (line.system_qty or 0.0)
                )
                if line.counted_qty != line.system_qty:
                    if not line.variance_reason:
                        unreasoned_lines += 1
                    variance_sections.add(line.section_id.id)
            session.variance_line_count = sum(
                1 for d in by_product.values() if d != 0.0
            )
            session.sections_with_variance = len(variance_sections)
            session.unreasoned_line_count = unreasoned_lines

    @api.depends(
        "start_date",
        "sections_total",
        "sections_reconciled",
        "state",
    )
    def _compute_eta(self):
        from datetime import datetime, timedelta

        Param = self.env["ir.config_parameter"].sudo()
        deadline_str = Param.get_param("vivo_count.trading_deadline", "09:30")
        try:
            deadline_h, deadline_m = (int(p) for p in deadline_str.split(":"))
        except (ValueError, AttributeError):
            deadline_h, deadline_m = 9, 30

        now = fields.Datetime.now()
        for session in self:
            total = session.sections_total or 0
            done = session.sections_reconciled or 0
            session.progress_pct = (done / total * 100.0) if total else 0.0
            if (
                session.start_date
                and done > 0
                and session.state in {"in_progress", "counted"}
            ):
                elapsed_seconds = (now - session.start_date).total_seconds()
                per_section_seconds = elapsed_seconds / done
                session.minutes_per_section = per_section_seconds / 60.0
                remaining_seconds = per_section_seconds * (total - done)
                eta = now + timedelta(seconds=remaining_seconds)
                session.estimated_completion = eta
                deadline_today = datetime(
                    eta.year, eta.month, eta.day, deadline_h, deadline_m
                )
                session.is_behind_trading_deadline = eta > deadline_today
            else:
                session.minutes_per_section = 0.0
                session.estimated_completion = False
                session.is_behind_trading_deadline = False

    @api.depends("variance_value")
    def _compute_tolerance_band(self):
        Param = self.env["ir.config_parameter"].sudo()
        store_band = float(Param.get_param("vivo_count.store_band_kes", "5000"))
        regional_band = float(Param.get_param("vivo_count.regional_band_kes", "25000"))
        for session in self:
            v = session.variance_value or 0.0
            if v <= store_band:
                session.tolerance_band = BAND_AUTO
            elif v <= regional_band:
                session.tolerance_band = BAND_REGIONAL
            else:
                session.tolerance_band = BAND_CFOO

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                seq = self.env["ir.sequence"].next_by_code("vivo.count.session") or _("New")
                vals["name"] = seq
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def _ensure_state(self, allowed):
        for session in self:
            if session.state not in allowed:
                raise UserError(
                    _("Session %(name)s is in state %(state)s; expected one of %(allowed)s.")
                    % {"name": session.name, "state": session.state, "allowed": ", ".join(allowed)}
                )

    def action_start(self):
        """draft -> in_progress.

        Materialises rack sections from the zone templates and snapshots
        `system_qty` for every SKU in scope (A5: snapshot at the
        draft->in_progress transition, inside this transaction).
        """
        self._ensure_state({"draft"})
        Section = self.env["vivo.count.section"]
        for session in self:
            if not session.location_id:
                raise UserError(_("Set a store location before starting the count."))
            templates = self._collect_section_templates(session)
            if not templates:
                raise UserError(
                    _("Zone(s) for %s have no rack templates configured. Add racks first.")
                    % session.location_id.display_name
                )
            for tpl in templates:
                Section.create(
                    {
                        "session_id": session.id,
                        "name": tpl.name,
                        "zone_id": tpl.zone_id.id,
                        "template_id": tpl.id,
                        "sequence": tpl.sequence,
                    }
                )
            session._snapshot_system_quantities()
            session.write({"state": "in_progress", "start_date": fields.Datetime.now()})
        return True

    def _collect_section_templates(self, session):
        Template = self.env["vivo.count.section.template"]
        if session.zone_id:
            return Template.search(
                [("zone_id", "=", session.zone_id.id), ("active", "=", True)],
                order="sequence, name",
            )
        return Template.search(
            [("location_id", "=", session.location_id.id), ("active", "=", True)],
            order="zone_id, sequence, name",
        )

    def _snapshot_system_quantities(self):
        """Snapshot on-hand quantities for every SKU in scope.

        Stored on `vivo.count.line.system_qty` and is the immutable basis
        for both variance computation and the reconciliation's
        `qty_before` (A5).
        """
        self.ensure_one()
        Quant = self.env["stock.quant"]
        Line = self.env["vivo.count.line"]
        location = self.location_id
        domain = [("location_id", "child_of", location.id), ("quantity", ">", 0)]
        if self.category_ids:
            domain.append(("product_id.categ_id", "child_of", self.category_ids.ids))
        quants = Quant.read_group(
            domain,
            fields=["product_id", "quantity:sum"],
            groupby=["product_id"],
        )
        if not self.section_ids:
            return
        catchall = self.section_ids[:1]
        for row in quants:
            qty = row["quantity"] or 0.0
            product = self.env["product.product"].browse(row["product_id"][0])
            Line.create(
                {
                    "session_id": self.id,
                    "section_id": catchall.id,
                    "product_id": product.id,
                    "system_qty": qty,
                    "unit_cost": product.standard_price,
                }
            )

    def _maybe_auto_advance_to_counted(self):
        """in_progress -> counted when every section has reconciled.

        Spec 4.1: 'The session auto-advances; it cannot reach this state
        while any section is still unreconciled.'
        """
        for session in self:
            if (
                session.state == "in_progress"
                and session.section_ids
                and all(s.state == "reconciled" for s in session.section_ids)
            ):
                session.state = "counted"

    def action_submit_for_review(self):
        """in_progress -> counted -> review.

        Auto-advances when all sections are reconciled (AC #4). Reviewer is
        auto-set to the user clicking the button so the audit trail records
        who actually performed the review action; manual override remains
        available on the form.
        """
        self._ensure_state({"in_progress", "counted"})
        for session in self:
            outstanding = session.section_ids.filtered(lambda s: s.state != "reconciled")
            if outstanding:
                raise UserError(
                    _("Cannot submit for review — these sections are not reconciled: %s")
                    % ", ".join(outstanding.mapped("name"))
                )
            session.write(
                {
                    "state": "review",
                    "reviewer_id": session.reviewer_id.id or self.env.user.id,
                }
            )
        return True

    def action_bounce_sections(self, section_ids):
        """review -> in_progress for selected sections.

        Manager bounces specific section(s) back for re-count. Per A3:
        rescan_count increments, counted_qty wiped on the section's lines
        but the scan history is preserved on `vivo.count.scan.event`. Other
        reconciled sections are untouched (AC #5).
        """
        self.ensure_one()
        self._ensure_state({"review"})
        Section = self.env["vivo.count.section"]
        sections = Section.browse(section_ids).filtered(lambda s: s.session_id == self)
        if not sections:
            raise UserError(_("Pick at least one section to bounce back."))
        sections._bounce_from_review()
        self.state = "in_progress"
        return True

    def _check_variance_reasons(self):
        """AC #11: every line with non-zero difference needs a reason."""
        self.ensure_one()
        missing = self.line_ids.filtered(
            lambda l: l.section_id.state == "reconciled"
            and l.difference != 0.0
            and not l.variance_reason
        )
        if missing:
            raise UserError(
                _("These lines have a variance but no reason: %s")
                % ", ".join(missing.mapped("product_id.display_name"))
            )
        bad_other = self.line_ids.filtered(
            lambda l: l.variance_reason == "other" and not l.variance_note
        )
        if bad_other:
            raise UserError(
                _("Lines with reason 'Other' need a free-text note: %s")
                % ", ".join(bad_other.mapped("product_id.display_name"))
            )

    def action_approve(self):
        """review -> approved. Routing checks (AC #7) live here."""
        self._ensure_state({"review"})
        for session in self:
            session._check_variance_reasons()
            session._check_approver_authority()
            session._check_counter_not_approver()
            session.write(
                {
                    "state": "approved",
                    "approver_id": session.approver_id.id or self.env.user.id,
                }
            )
        return True

    def _check_approver_authority(self):
        """Block approval when the current user's band is below required."""
        self.ensure_one()
        user = self.env.user
        band = self.tolerance_band
        if band == BAND_AUTO:
            if not user.has_group("vivo_stock_count.group_vivo_count_store_manager"):
                raise AccessError(
                    _("Only a Store Manager (or higher) can approve this session.")
                )
        elif band == BAND_REGIONAL:
            if not user.has_group("vivo_stock_count.group_vivo_count_regional"):
                raise AccessError(
                    _(
                        "Variance value %.2f exceeds the Store Manager band — "
                        "Regional Manager (or higher) approval required."
                    )
                    % self.variance_value
                )
        elif band == BAND_CFOO:
            if not user.has_group("vivo_stock_count.group_vivo_count_cfoo_audit"):
                raise AccessError(
                    _(
                        "Variance value %.2f exceeds the Regional band — "
                        "CFOO / Audit approval required."
                    )
                    % self.variance_value
                )

    def _check_counter_not_approver(self):
        """SoD: a user who counted in a section cannot approve the session."""
        self.ensure_one()
        user = self.env.user
        section_counters = self.section_ids.mapped("scanner_id") | self.section_ids.mapped(
            "physical_counter_id"
        )
        if user in section_counters:
            raise UserError(
                _("You cannot approve a session in which you scanned or counted.")
            )

    def action_apply(self):
        """approved -> applied.

        Phase 4: full Apply. In one transaction (per A7):
          1. Write counted quantities to stock.quant via the native
             inventory-adjustment pipeline (creates stock.move records;
             account.move is created automatically by Odoo's valuation
             logic for real-time-valued products).
          2. Generate the immutable vivo.count.reconciliation report.
          3. Transition session to 'applied'.

        If any step raises, the entire transaction rolls back and the
        session remains 'approved' so the user can retry. (Risk #4 in
        spec Section 14.)
        """
        self._ensure_state({"approved"})
        for session in self:
            if self.env.user.has_group("vivo_stock_count.group_vivo_count_counter") and not (
                self.env.user.has_group("vivo_stock_count.group_vivo_count_store_manager")
                or self.env.user.has_group("vivo_stock_count.group_vivo_count_regional")
                or self.env.user.has_group("vivo_stock_count.group_vivo_count_cfoo_audit")
            ):
                # AC #1: counters can never apply.
                raise AccessError(_("Counters cannot post stock counts to the GL."))
            session._check_approver_authority()
            # Single savepoint wraps GL writes + reconciliation generation
            # + state transition (A7). On any exception, all three roll
            # back together; session stays at 'approved' for retry.
            with session.env.cr.savepoint():
                session._apply_inventory_adjustment()
                recon = session._generate_reconciliation()
                session.write(
                    {
                        "state": "applied",
                        "applied_by_id": self.env.user.id,
                        "end_date": fields.Datetime.now(),
                        "reconciliation_id": recon.id,
                    }
                )
        return True

    # ------------------------------------------------------------------
    # Apply — inventory adjustment and reconciliation generation
    # ------------------------------------------------------------------
    def _apply_inventory_adjustment(self):
        """Write counted quantities to stock.quant via the native pipeline.

        Aggregates counted_qty across all sections per product, then uses
        stock.quant's inventory-mode update so Odoo creates the standard
        adjustment moves (and journal entries for real-time-valued
        products) — the module never bypasses native accounting.
        """
        self.ensure_one()
        if not self.location_id:
            raise UserError(_("Session has no location."))
        Quant = self.env["stock.quant"]
        # Aggregate per product across reconciled sections.
        per_product = self._aggregate_counts_by_product()
        for product_id, counted in per_product.items():
            quant = Quant.search(
                [
                    ("product_id", "=", product_id),
                    ("location_id", "=", self.location_id.id),
                ],
                limit=1,
            )
            if quant:
                quant.with_context(inventory_mode=True).write(
                    {"inventory_quantity": counted}
                )
            else:
                quant = Quant.with_context(inventory_mode=True).create(
                    {
                        "product_id": product_id,
                        "location_id": self.location_id.id,
                        "inventory_quantity": counted,
                    }
                )
            # action_apply_inventory creates the stock.move (and account.move
            # if the product is in real-time valuation).
            quant.action_apply_inventory()

    def _aggregate_counts_by_product(self):
        """Return {product_id: total_counted_qty} for reconciled sections."""
        self.ensure_one()
        agg = {}
        for line in self.line_ids:
            if line.section_id.state != "reconciled":
                continue
            agg[line.product_id.id] = agg.get(line.product_id.id, 0.0) + (
                line.counted_qty or 0.0
            )
        return agg

    def _generate_reconciliation(self):
        """Create the immutable Stock Take Reconciliation report.

        One reconciliation record per applied session, with one
        reconciliation line PER PRODUCT — aggregating across sections so
        the variance is the genuine net per-SKU figure (AC #16). zone_id
        and section_id on the recon line point to the predominant section
        for that product when it lives in exactly one rack; left blank
        when the product is split across racks (the report renders
        'Multiple' in that case).
        """
        self.ensure_one()
        Recon = self.env["vivo.count.reconciliation"].sudo()
        ReconLine = self.env["vivo.count.reconciliation.line"].sudo()
        seq = self.env["ir.sequence"].next_by_code(
            "vivo.count.reconciliation"
        ) or _("New")

        per_product = {}
        for line in self.line_ids:
            if line.section_id.state != "reconciled":
                continue
            pid = line.product_id.id
            d = per_product.setdefault(
                pid,
                {
                    "product": line.product_id,
                    "qty_before": 0.0,
                    "qty_after": 0.0,
                    "unit_cost": line.unit_cost or 0.0,
                    "sections": set(),
                    "zones": set(),
                    "max_rescan": 0,
                    "reasons": set(),
                },
            )
            d["qty_before"] += line.system_qty or 0.0
            d["qty_after"] += line.counted_qty or 0.0
            if line.unit_cost:
                d["unit_cost"] = line.unit_cost
            if line.counted_qty:
                d["sections"].add(line.section_id.id)
                d["zones"].add(line.section_id.zone_id.id)
            d["max_rescan"] = max(d["max_rescan"], line.section_id.rescan_count)
            if line.variance_reason:
                d["reasons"].add(line.variance_reason)

        recon = Recon.create(
            {
                "name": seq,
                "session_id": self.id,
                "generated_at": fields.Datetime.now(),
                "variance_band": self.tolerance_band,
                "scanner_ids": [
                    (6, 0, self.section_ids.mapped("scanner_id").ids)
                ],
                "physical_counter_ids": [
                    (6, 0, self.section_ids.mapped("physical_counter_id").ids)
                ],
                "reviewer_id": self.reviewer_id.id or False,
                "approver_id": self.approver_id.id or False,
                "applied_by_id": self.env.user.id,
            }
        )

        for pid, d in per_product.items():
            section_id = list(d["sections"])[0] if len(d["sections"]) == 1 else False
            zone_id = list(d["zones"])[0] if len(d["zones"]) == 1 else False
            ReconLine.create(
                {
                    "reconciliation_id": recon.id,
                    "product_id": pid,
                    "barcode": d["product"].barcode or "",
                    "section_id": section_id,
                    "zone_id": zone_id,
                    "qty_before": d["qty_before"],
                    "qty_after": d["qty_after"],
                    "value_before": d["qty_before"] * d["unit_cost"],
                    "value_after": d["qty_after"] * d["unit_cost"],
                    "variance_reason": ", ".join(sorted(d["reasons"])) or "",
                    "section_rescan_count": d["max_rescan"],
                }
            )
        # AC #17: notify the audit group the moment the report exists.
        recon.notify_audit()
        return recon

    def action_open_section_board(self):
        """Open the colour-coded section progress board for this session."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Section Progress — %s") % self.name,
            "res_model": "vivo.count.section",
            "view_mode": "kanban,list,form",
            "domain": [("session_id", "=", self.id)],
            "context": {"default_session_id": self.id, "search_default_group_state": 1},
            "views": [
                (self.env.ref("vivo_stock_count.view_vivo_count_section_kanban").id, "kanban"),
                (self.env.ref("vivo_stock_count.view_vivo_count_section_list").id, "list"),
                (self.env.ref("vivo_stock_count.view_vivo_count_section_form").id, "form"),
            ],
        }

    def action_open_variance_dashboard(self):
        """Pivot dashboard on lines with non-zero variance for this session."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Variance Dashboard — %s") % self.name,
            "res_model": "vivo.count.line",
            "view_mode": "pivot,graph,list",
            "domain": [
                ("session_id", "=", self.id),
                ("difference", "!=", 0),
                ("section_id.state", "=", "reconciled"),
            ],
            "views": [
                (self.env.ref("vivo_stock_count.view_vivo_count_line_pivot").id, "pivot"),
                (self.env.ref("vivo_stock_count.view_vivo_count_line_graph").id, "graph"),
                (self.env.ref("vivo_stock_count.view_vivo_count_line_list").id, "list"),
            ],
        }

    def action_open_approval_wizard(self):
        """Open the approval preview wizard — blockers + band + line summary."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Approve Count — %s") % self.name,
            "res_model": "vivo.count.approval.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_session_id": self.id},
        }

    def action_open_bounce_wizard(self):
        """Open the bounce-sections wizard during review."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Bounce Sections — %s") % self.name,
            "res_model": "vivo.count.bounce.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_session_id": self.id},
        }

    def action_cancel(self):
        for session in self:
            if session.state == "applied":
                raise UserError(_("Applied sessions cannot be cancelled."))
            session.state = "cancelled"
        return True

    def action_reset_to_draft(self):
        for session in self:
            if session.state not in {"cancelled"}:
                raise UserError(_("Only cancelled sessions can be reset to draft."))
            session.section_ids.unlink()
            session.line_ids.unlink()
            session.write({"state": "draft", "start_date": False, "end_date": False})
        return True

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------
    @api.constrains("state", "section_ids")
    def _check_advance_requires_reconciled_sections(self):
        for session in self:
            if session.state in {"counted", "review", "approved", "applied"}:
                outstanding = session.section_ids.filtered(lambda s: s.state != "reconciled")
                if outstanding:
                    raise ValidationError(
                        _(
                            "Session %(name)s cannot be in state %(state)s while "
                            "sections are still unreconciled: %(sections)s"
                        )
                        % {
                            "name": session.name,
                            "state": session.state,
                            "sections": ", ".join(outstanding.mapped("name")),
                        }
                    )
