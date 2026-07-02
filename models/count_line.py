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

LINE_STATUSES = [
    ("counted", "Counted"),
    ("not_counted", "Not Counted Here"),
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
    line_status = fields.Selection(
        LINE_STATUSES,
        compute="_compute_line_status",
        store=True,
        index=True,
        help=(
            "Counted = this SKU was scanned on this rack (counted qty > 0).\n"
            "Not Counted Here = a system SKU that was not found on this rack. "
            "It is pending on another rack, not a shrinkage variance, so it "
            "needs no variance reason. An SKU left uncounted on every rack of "
            "the session is flagged as a genuine shortage at session level."
        ),
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

    @api.depends("counted_qty")
    def _compute_line_status(self):
        for line in self:
            line.line_status = (
                "counted" if (line.counted_qty or 0.0) > 0 else "not_counted"
            )

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

    # ------------------------------------------------------------------
    # PWA API (Phase 3)
    # ------------------------------------------------------------------
    @api.model
    def record_scan(
        self,
        section_id,
        product_id,
        scanned_qty,
        idempotency_key,
        device_id=None,
    ):
        """Idempotent scan-and-increment endpoint for the mobile PWA.

        Contract (AC #6, AC #8):
        - Each scan event carries a client-generated idempotency_key.
        - If the key was already submitted, the existing event is returned
          and no quantity is added — replay is a no-op.
        - Otherwise a new scan_event is logged AND the line's counted_qty
          is incremented by `scanned_qty` (scan-once-then-type-qty: one scan
          event with scanned_qty=6 → counted_qty +=6, scan_count +=1).
        - On the first scan of an SKU in a section, the line is created.
        """
        if not idempotency_key:
            raise ValidationError(_("idempotency_key is required."))
        Section = self.env["vivo.count.section"]
        section = Section.browse(section_id).exists()
        if not section:
            raise ValidationError(_("Section not found."))
        if section.state not in {"scanning", "variance_rescan"}:
            raise ValidationError(
                _("Section %s is not open for scanning (state: %s).")
                % (section.name, section.state)
            )
        ScanEvent = self.env["vivo.count.scan.event"]
        existing = ScanEvent.search(
            [("idempotency_key", "=", idempotency_key)], limit=1
        )
        if existing:
            return {
                "idempotent": True,
                "scan_event_id": existing.id,
                "line_id": existing.line_id.id,
                "counted_qty": existing.line_id.counted_qty,
                "scan_count": existing.line_id.scan_count,
            }
        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            raise ValidationError(_("Product not found."))
        line = self.search(
            [("section_id", "=", section.id), ("product_id", "=", product.id)],
            limit=1,
        )
        scan_type = "rescan" if section.state == "variance_rescan" else "initial"
        if not line:
            # Pull the system snapshot if it was captured at session start; if
            # the SKU wasn't in scope (new arrival), system_qty stays 0.
            line = self.create(
                {
                    "section_id": section.id,
                    "product_id": product.id,
                    "system_qty": 0.0,
                    "counted_qty": 0.0,
                    "unit_cost": product.standard_price,
                    "counter_id": self.env.uid,
                    "scanned_at": fields.Datetime.now(),
                }
            )
        line.write(
            {
                "counted_qty": (line.counted_qty or 0.0) + scanned_qty,
                "scan_count": (line.scan_count or 0) + 1,
                "counter_id": self.env.uid,
                "scanned_at": fields.Datetime.now(),
            }
        )
        event = ScanEvent.sudo().create(
            {
                "line_id": line.id,
                "counter_id": self.env.uid,
                "scanned_qty": scanned_qty,
                "scan_type": scan_type,
                "idempotency_key": idempotency_key,
                "device_id": device_id or "",
            }
        )
        return {
            "idempotent": False,
            "scan_event_id": event.id,
            "line_id": line.id,
            "counted_qty": line.counted_qty,
            "scan_count": line.scan_count,
        }
