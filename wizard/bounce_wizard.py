from odoo import _, api, fields, models
from odoo.exceptions import UserError


class VivoCountBounceWizard(models.TransientModel):
    """Bounce specific reconciled sections back to scanning during review.

    Per A3: rescan_count increments on each bounced section, counted_qty is
    wiped on the section's lines but scan history is preserved on
    vivo.count.scan.event. Other reconciled sections stay green (AC #5).
    """

    _name = "vivo.count.bounce.wizard"
    _description = "Vivo Count Bounce Sections Wizard"

    session_id = fields.Many2one("vivo.count.session", required=True, readonly=True)
    section_ids = fields.Many2many(
        "vivo.count.section",
        domain="[('session_id', '=', session_id), ('state', '=', 'reconciled')]",
        string="Sections to re-count",
    )
    reason = fields.Text(required=True, help="Why these sections are being bounced.")

    def action_bounce(self):
        self.ensure_one()
        if not self.section_ids:
            raise UserError(_("Select at least one section to bounce."))
        self.session_id.action_bounce_sections(self.section_ids.ids)
        self.session_id.message_post(
            body=_(
                "Sections bounced for re-count: %(names)s. Reason: %(reason)s"
            )
            % {
                "names": ", ".join(self.section_ids.mapped("name")),
                "reason": self.reason,
            }
        )
        return {"type": "ir.actions.act_window_close"}
