from odoo import fields, models


class VivoCountSectionReviewWizard(models.TransientModel):
    """Manager/auditor review of a section held in `pending_review`.

    Approve-then-review flow: the scanned result has already been approved by a
    second person. The reviewer records a MANDATORY variance note and
    reconciles. Confirmation runs the section's `action_confirm_reconcile`,
    which enforces the manager/auditor band, requires the note, transitions the
    section to `reconciled`, and stamps `reconciled_by_id`.
    """

    _name = "vivo.count.section.review.wizard"
    _description = "Vivo Count Section Review Wizard"

    section_id = fields.Many2one(
        "vivo.count.section", required=True, readonly=True, ondelete="cascade"
    )
    section_name = fields.Char(related="section_id.name", readonly=True)
    scan_total_qty = fields.Float(related="section_id.scan_total_qty", readonly=True)
    review_note = fields.Text(
        string="Variance note",
        required=True,
        help="Mandatory: record what the counts show and why you are "
             "reconciling this section.",
    )
    # Editable so the reviewer can capture per-line variance reasons/notes on
    # the real lines. The view filters this to the counted subset.
    line_ids = fields.One2many(related="section_id.line_ids", readonly=False)

    def action_confirm(self):
        self.ensure_one()
        # Any per-line reason/note edits are written by the save that precedes
        # this button call; confirm validates the band, requires the note, and
        # reconciles.
        self.section_id.action_confirm_reconcile(review_note=self.review_note)
        return {"type": "ir.actions.act_window_close"}
