from odoo import api, fields, models


class VivoCountZone(models.Model):
    _name = "vivo.count.zone"
    _description = "Vivo Count Zone"
    _order = "location_id, sequence, name"

    name = fields.Char(required=True)
    location_id = fields.Many2one(
        "stock.location",
        string="Store Location",
        required=True,
        domain="[('usage', '=', 'internal')]",
        ondelete="restrict",
    )
    sequence = fields.Integer(default=10)
    section_template_ids = fields.One2many(
        "vivo.count.section.template",
        "zone_id",
        string="Rack Templates",
    )
    section_template_count = fields.Integer(
        compute="_compute_section_template_count",
    )
    active = fields.Boolean(default=True)

    @api.depends("section_template_ids")
    def _compute_section_template_count(self):
        for zone in self:
            zone.section_template_count = len(zone.section_template_ids)
