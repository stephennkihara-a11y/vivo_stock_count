from odoo import _, api, fields, models
from odoo.exceptions import AccessError


SCAN_TYPES = [
    ("initial", "Initial"),
    ("rescan", "Re-scan"),
    ("correction", "Correction"),
]


class VivoCountScanEvent(models.Model):
    """Append-only scan log.

    Per A6: `initial` = first scan of the SKU in this section in its current
    state; `rescan` = scan logged after the section was bounced from review
    (rescan_count > 0); `correction` = scanner manually amends qty on an
    existing line before finishing scanning.

    These records are never deleted and never edited. They are the audit
    trail referenced by Section 11 AC #10.
    """

    _name = "vivo.count.scan.event"
    _description = "Vivo Count Scan Event"
    _order = "scanned_at desc, id desc"

    line_id = fields.Many2one("vivo.count.line", ondelete="cascade", index=True)
    section_id = fields.Many2one(
        "vivo.count.section",
        related="line_id.section_id",
        store=True,
        index=True,
        readonly=True,
    )
    session_id = fields.Many2one(
        "vivo.count.session",
        related="line_id.session_id",
        store=True,
        index=True,
        readonly=True,
    )
    product_id = fields.Many2one(
        "product.product", related="line_id.product_id", store=True, readonly=True
    )
    counter_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user
    )
    scanned_qty = fields.Float(required=True, digits="Product Unit of Measure")
    scan_type = fields.Selection(SCAN_TYPES, default="initial", required=True)
    scanned_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    device_id = fields.Char(string="Device Fingerprint")
    idempotency_key = fields.Char(
        index=True,
        help="Client-generated key for offline replay de-dup (PWA, Phase 3).",
    )

    _sql_constraints = [
        (
            "idempotency_unique",
            "UNIQUE(idempotency_key)",
            "Duplicate scan event — idempotency key already used.",
        ),
    ]

    def write(self, vals):
        raise AccessError(_("Scan events are immutable — write disabled."))

    def unlink(self):
        raise AccessError(_("Scan events are immutable — delete disabled."))
