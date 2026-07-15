from odoo import _, api, fields, models
from odoo.exceptions import UserError


class VivoCountSessionReconcileGateWizard(models.TransientModel):
    """Store-level mismatch gate — a SECOND safety net at the store reconcile.

    Even if a rack was clicked past its own review-time gate, a physical-vs-scan
    disagreement must not silently reach the committed store count. When the
    auditor opens the store reconcile, every rack about to be reconciled
    (``pending_review``) is scanned:

    - MISMATCH racks (physical entered AND != scanned) are listed one per line,
      each with a tick box, so the auditor can send back ONLY the racks they
      choose — there is deliberately no blanket "reject all".
    - UNVERIFIED racks (no physical count entered) are shown separately as a
      softer advisory; they are NOT mismatches and are not tickable here.

    Proceed continues the reconcile exactly as today; Reject Selected wipes only
    the ticked racks via the EXISTING ``action_reject_and_recount`` (guarded,
    audited) and does NOT reconcile the session. If nothing is flagged the gate
    never opens (see ``vivo.count.session._reconcile_gate_action``).
    """

    _name = "vivo.count.session.reconcile.gate.wizard"
    _description = "Vivo Count Store Reconcile Gate Wizard"

    session_id = fields.Many2one(
        "vivo.count.session", required=True, readonly=True, ondelete="cascade"
    )
    session_name = fields.Char(related="session_id.name", readonly=True)
    mismatch_line_ids = fields.One2many(
        "vivo.count.session.reconcile.gate.line",
        "wizard_id",
        string="Mismatch racks",
    )
    has_mismatch = fields.Boolean(compute="_compute_flags")
    unverified_names = fields.Char(readonly=True)
    unverified_count = fields.Integer(readonly=True)
    reject_warning = fields.Char(compute="_compute_reject_warning")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        session_id = res.get("session_id") or self.env.context.get(
            "default_session_id"
        )
        if not session_id:
            return res
        session = self.env["vivo.count.session"].browse(session_id)
        _pct, ready, _out = session._store_reconcile_readiness()
        # MISMATCH: reuse the shared rack helper so the rule is identical.
        mismatch = ready.filtered(lambda s: s._mismatch_gate_triggered())
        # UNVERIFIED: no physical count was ever entered.
        unverified = ready.filtered(lambda s: (s.physical_total_qty or 0.0) <= 0)
        res["mismatch_line_ids"] = [
            (
                0,
                0,
                {
                    "section_id": s.id,
                    "physical_qty": s.physical_total_qty,
                    "scanned_qty": s.scan_total_qty,
                    "line_count": len(s.line_ids),
                },
            )
            for s in mismatch
        ]
        res["unverified_names"] = ", ".join(unverified.mapped("name"))
        res["unverified_count"] = len(unverified)
        return res

    @api.depends("mismatch_line_ids")
    def _compute_flags(self):
        for wiz in self:
            wiz.has_mismatch = bool(wiz.mismatch_line_ids)

    @api.depends(
        "mismatch_line_ids.reject",
        "mismatch_line_ids.line_count",
        "mismatch_line_ids.section_name",
    )
    def _compute_reject_warning(self):
        for wiz in self:
            ticked = wiz.mismatch_line_ids.filtered("reject")
            if not ticked:
                wiz.reject_warning = ""
                continue
            parts = [
                "%s (%d lines)" % (line.section_name or "?", line.line_count)
                for line in ticked
            ]
            wiz.reject_warning = _(
                "This will DELETE all scans in %s and require a full rescan. "
                "This cannot be undone."
            ) % " and ".join(parts)

    def action_proceed(self):
        """Accept and continue the reconcile exactly as today — open the store
        reconcile note wizard (bypassing the gate, which we have just cleared)."""
        self.ensure_one()
        return self.session_id._open_reconcile_note_wizard()

    def action_reject_selected(self):
        """Send ONLY the ticked mismatch racks back for a full rescan, reusing
        the existing per-section wipe. The session is NOT reconciled — the
        auditor reconciles later once the rejected racks are rescanned."""
        self.ensure_one()
        to_reject = self.mismatch_line_ids.filtered("reject").mapped("section_id")
        if not to_reject:
            raise UserError(
                _("Tick at least one rack to reject, or click Proceed to continue.")
            )
        for section in to_reject:
            # Reuse the guarded, audited wipe — a reconciled rack raises here.
            section.action_reject_and_recount()
        return {"type": "ir.actions.act_window_close"}


class VivoCountSessionReconcileGateLine(models.TransientModel):
    _name = "vivo.count.session.reconcile.gate.line"
    _description = "Vivo Count Store Reconcile Gate Line"

    wizard_id = fields.Many2one(
        "vivo.count.session.reconcile.gate.wizard",
        required=True,
        ondelete="cascade",
    )
    section_id = fields.Many2one(
        "vivo.count.section", required=True, readonly=True
    )
    section_name = fields.Char(related="section_id.name", readonly=True)
    physical_qty = fields.Float(
        string="Physical", readonly=True, digits="Product Unit of Measure"
    )
    scanned_qty = fields.Float(
        string="Scanned", readonly=True, digits="Product Unit of Measure"
    )
    variance = fields.Float(
        compute="_compute_variance", readonly=True, digits="Product Unit of Measure"
    )
    line_count = fields.Integer(string="Lines", readonly=True)
    reject = fields.Boolean(string="Reject & recount")

    @api.depends("physical_qty", "scanned_qty")
    def _compute_variance(self):
        for line in self:
            line.variance = (line.physical_qty or 0.0) - (line.scanned_qty or 0.0)
