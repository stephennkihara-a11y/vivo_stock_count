from odoo import _, fields, models


class VivoCountSectionReviewWizard(models.TransientModel):
    """Auditor review of a section held in `pending_review`.

    Shows the counted subset of the section's lines so the auditor can fill
    in variance reasons before confirming. Confirmation runs the section's
    `action_confirm_reconcile`, which validates reasons, transitions the
    section to `reconciled`, and stamps `reconciled_by_id` for the audit
    trail.
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
    # Editable so the auditor can capture variance reasons/notes on the real
    # lines. The view filters this to the counted subset.
    line_ids = fields.One2many(related="section_id.line_ids", readonly=False)

    def action_confirm(self):
        self.ensure_one()
        # Any reason/note edits made in the wizard are already written to the
        # lines by the save that precedes this button call; confirm validates
        # and reconciles.
        self.section_id.action_confirm_reconcile()
        return {"type": "ir.actions.act_window_close"}
