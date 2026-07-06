from odoo import _, api, fields, models


class VivoCountSectionReviewWizard(models.TransientModel):
    """Auditor review of a section held in `pending_review`.

    Two cases converge here:
    - scan == physical (a genuine per-line variance): the auditor captures
      variance reasons on the counted lines and confirms.
    - scan != physical (a persistent disagreement escalated after re-scans):
      the counters could not agree, so the auditor sets an authoritative
      physical count and records why before force-reconciling.

    Confirmation runs the section's `action_confirm_reconcile`, which validates
    reasons, transitions the section to `reconciled`, and stamps
    `reconciled_by_id` for the audit trail.
    """

    _name = "vivo.count.section.review.wizard"
    _description = "Vivo Count Section Review Wizard"

    section_id = fields.Many2one(
        "vivo.count.section", required=True, readonly=True, ondelete="cascade"
    )
    section_name = fields.Char(related="section_id.name", readonly=True)
    scan_total_qty = fields.Float(
        related="section_id.scan_total_qty", readonly=True
    )
    physical_total_qty = fields.Float(
        related="section_id.physical_total_qty", readonly=True
    )
    is_mismatch = fields.Boolean(compute="_compute_is_mismatch")
    # Auditor's authoritative headcount when the counters disagree (their
    # number wins). Defaults to the physical counter's figure.
    authoritative_qty = fields.Float(
        string="Authoritative Physical Count",
        compute="_compute_authoritative_qty",
        store=True,
        readonly=False,
    )
    force_reason = fields.Text(
        string="Reason for auditor reconciliation",
        help="Required when the scan and physical counts still disagree.",
    )
    # Editable so the auditor can capture variance reasons/notes on the real
    # lines. The view filters this to the counted subset.
    line_ids = fields.One2many(related="section_id.line_ids", readonly=False)

    @api.depends("section_id.scan_total_qty", "section_id.physical_total_qty")
    def _compute_is_mismatch(self):
        for wiz in self:
            wiz.is_mismatch = bool(wiz.section_id) and (
                wiz.section_id.scan_total_qty != wiz.section_id.physical_total_qty
            )

    @api.depends("section_id.physical_total_qty")
    def _compute_authoritative_qty(self):
        for wiz in self:
            wiz.authoritative_qty = wiz.section_id.physical_total_qty

    def action_confirm(self):
        self.ensure_one()
        # Any reason/note edits made in the wizard are already written to the
        # lines by the save that precedes this button call; confirm validates
        # and reconciles.
        if self.is_mismatch:
            self.section_id.action_confirm_reconcile(
                physical_qty=self.authoritative_qty,
                force_reason=self.force_reason,
            )
        else:
            self.section_id.action_confirm_reconcile()
        return {"type": "ir.actions.act_window_close"}
