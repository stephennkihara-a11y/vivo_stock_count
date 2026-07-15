from odoo import _, api, fields, models


class VivoCountRecountGateWizard(models.TransientModel):
    """Finish-Scanning mismatch gate (desktop).

    Opened by ``vivo.count.section.action_finish_scanning_gate`` when the manual
    Physical Count does not equal the scanned total. It is the red-alert dialog
    that forces an explicit choice instead of finishing silently:

    - Proceed — accept the discrepancy and advance the rack (submit for review),
      exactly as Finish Scanning normally does.
    - Reject & Recount — DELETE every scanned line on the rack and send it back
      to scanning for a full rescan. Destructive; the button carries a native
      confirmation and the section records an audit line.
    """

    _name = "vivo.count.recount.gate.wizard"
    _description = "Vivo Count Recount Gate Wizard"

    section_id = fields.Many2one(
        "vivo.count.section", required=True, readonly=True, ondelete="cascade"
    )
    section_name = fields.Char(related="section_id.name", readonly=True)
    scan_total_qty = fields.Float(
        related="section_id.scan_total_qty",
        string="Scanned Total",
        readonly=True,
        digits="Product Unit of Measure",
    )
    physical_total_qty = fields.Float(
        related="section_id.physical_total_qty",
        string="Physical Count",
        readonly=True,
        digits="Product Unit of Measure",
    )
    line_count = fields.Integer(
        string="Lines to clear", compute="_compute_line_count", readonly=True
    )

    @api.depends("section_id.line_ids")
    def _compute_line_count(self):
        for wiz in self:
            wiz.line_count = len(wiz.section_id.line_ids)

    def action_proceed(self):
        """Accept the discrepancy and advance the rack.

        From the scanner's Finish path the rack is still ``scanning``, so finish
        it (unchanged behaviour). From the supervisor's review path it is
        already ``pending_review`` (awaiting the store reconcile) — accepting
        the discrepancy leaves it exactly there, so there is nothing further to
        advance. The state check keeps the original Finish behaviour byte-for-
        byte while making Proceed reachable from the review-time gate."""
        self.ensure_one()
        if self.section_id.state == "scanning":
            self.section_id.action_finish_scanning()
        return self._reopen_section()

    def action_reject(self):
        """Wipe the rack and send it back to scanning for a full rescan. The
        destructive delete + audit trail live in the section method; the button
        that calls this carries the explicit confirmation."""
        self.ensure_one()
        self.section_id.action_reject_and_recount()
        return self._reopen_section()

    def _reopen_section(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": "vivo.count.section",
            "res_id": self.section_id.id,
            "view_mode": "form",
            "target": "current",
        }
