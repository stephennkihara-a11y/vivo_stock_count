from odoo import fields, models


class VivoCountSectionTemplate(models.Model):
    """Persistent rack template.

    A store's physical layout (racks per zone) is configured once via these
    templates and reused for every count. When a session starts, real
    `vivo.count.section` records are cloned from these templates.
    """

    _name = "vivo.count.section.template"
    _description = "Vivo Count Section Template"
    _order = "zone_id, sequence, name"

    name = fields.Char(string="Rack Name", required=True)
    zone_id = fields.Many2one(
        "vivo.count.zone",
        string="Zone",
        required=True,
        ondelete="cascade",
    )
    location_id = fields.Many2one(
        related="zone_id.location_id",
        store=True,
        readonly=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    note = fields.Text()
