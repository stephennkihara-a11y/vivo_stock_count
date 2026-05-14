from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


VARIANCE_REASONS = [
    ("theft", "Theft"),
    ("miscount", "Miscount"),
    ("mis_scan", "Mis-scan"),
    ("system_error", "System Error"),
    ("supplier_short", "Supplier Short"),
    ("in_transit", "In Transit"),
    ("damaged", "Damaged"),
    ("other", "Other"),
]


class VivoCountLine(models.Model):
    _name = "vivo.count.line"
    _description = "Vivo Count Line"
    _order = "session_id, section_id, product_id"

    section_id = fields.Many2one(
        "vivo.count.section",
        required=True,
        ondelete="cascade",
        index=True,
    )
    session_id = fields.Many2one(
        "vivo.count.session",
        related="section_id.session_id",
        store=True,
        index=True,
        readonly=True,
    )
    zone_id = fields.Many2one(
        "vivo.count.zone",
        related="section_id.zone_id",
        store=True,
        readonly=True,
    )
    product_id = fields.Many2one("product.product", string="SKU", required=True)
    barcode = fields.Char(related="product_id.barcode", store=True, readonly=True)

    system_qty = fields.Float(
        digits="Product Unit of Measure",
        help="On-hand per Odoo at session start. Immutable snapshot.",
    )
    counted_qty = fields.Float(digits="Product Unit of Measure")
    difference = fields.Float(
        compute="_compute_difference", store=True, digits="Product Unit of Measure"
    )

    unit_cost = fields.Float(string="Unit Cost", digits="Product Price")
    currency_id = fields.Many2one(
        related="section_id.session_id.currency_id", readonly=True
    )
    variance_value = fields.Monetary(
        compute="_compute_variance_value",
        store=True,
        currency_field="currency_id",
    )

    scan_count = fields.Integer(default=0, readonly=True)
    variance_reason = fields.Selection(VARIANCE_REASONS)
    variance_note = fields.Text()

    counter_id = fields.Many2one("res.users", string="Counter")
    scanned_at = fields.Datetime()

    no_barcode_flag = fields.Boolean(
        help="Set by the scanner when an item has no scannable barcode."
    )
    no_barcode_note = fields.Char()
    no_barcode_resolved = fields.Boolean(
        help="Set when the scanner has linked this line to a real SKU."
    )

    @api.depends("counted_qty", "system_qty")
    def _compute_difference(self):
        for line in self:
            line.difference = (line.counted_qty or 0.0) - (line.system_qty or 0.0)

    @api.depends("difference", "unit_cost")
    def _compute_variance_value(self):
        for line in self:
            line.variance_value = line.difference * (line.unit_cost or 0.0)

    @api.constrains("no_barcode_flag", "no_barcode_resolved", "product_id")
    def _check_no_barcode_resolution(self):
        for line in self:
            if line.no_barcode_resolved and not line.product_id:
                raise ValidationError(
                    _(
                        "Line marked no-barcode-resolved must be linked to a real "
                        "SKU before reconciliation."
                    )
                )
