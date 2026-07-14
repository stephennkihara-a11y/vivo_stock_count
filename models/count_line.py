from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


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
    product_id = fields.Many2one("product.product", string="SKU")
    barcode = fields.Char(related="product_id.barcode", store=True, readonly=True)

    # Unknown-barcode capture: a scan whose barcode matched no product is
    # recorded here as a PRODUCT-LESS line (product_id empty). The raw barcode
    # is preserved so the item can be identified and, in a later phase, linked
    # to a real SKU. Capture only — no product master is ever created here.
    scanned_barcode = fields.Char(
        string="Scanned Barcode",
        index=True,
        help="Raw barcode as scanned. Set on unknown-item lines that carry no "
        "SKU; used to key/aggregate captured unknowns per rack.",
    )
    is_unknown = fields.Boolean(
        string="Unknown / Not in System",
        default=False,
        index=True,
        help="This line was captured from a barcode that matched no product. "
        "It has no SKU and is surfaced as a positive (surplus) variance.",
    )
    product_title = fields.Char(
        string="Product Title",
        compute="_compute_product_title",
        help="Product name for known SKUs; the literal 'Unknown' for captured "
        "barcodes that match no product.",
    )

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

    is_unexpected = fields.Boolean(
        string="Unexpected Item",
        help="Quick Count mode: set when a scanned SKU had zero on-hand at the "
             "location on first scan — a likely overage / off-system item.",
    )

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

    @api.depends("product_id", "product_id.name", "is_unknown")
    def _compute_product_title(self):
        for line in self:
            line.product_title = line.product_id.name if line.product_id else "Unknown"

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
    # Delete guard — a scanned line may only be removed while scanning
    # ------------------------------------------------------------------
    def unlink(self):
        """Hard audit guard shared by EVERY delete path — the desktop trash
        icon, the PWA delete button, and any direct ORM unlink.

        A scanned line may be removed ONLY while its rack is still being
        scanned. Once the rack is submitted (pending_review / reconciled /
        excluded / any non-scanning state) the line is locked, so a variance
        cannot be erased after review. This is server-side and unconditional —
        not just hidden in the UI.
        """
        for line in self:
            section = line.section_id
            if section and section.state != "scanning":
                raise UserError(
                    _(
                        "Lines can only be removed while the rack is still being "
                        "scanned. Rack %(rack)s is now '%(state)s'."
                    )
                    % {"rack": section.name, "state": section.state}
                )
        return super().unlink()

    def action_delete_scan_line(self):
        """Delete a scanned line to fix a mistake (double-scan, or item scanned
        into the wrong rack). The scanning-state rule is enforced in ``unlink``
        above, so the PWA and desktop routes share exactly one guard."""
        self.ensure_one()
        self.unlink()
        return {"ok": True, "deleted": True}

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
        scanned_barcode=None,
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

        Unknown-barcode capture: when ``product_id`` is empty and a
        ``scanned_barcode`` is supplied, the scan is aggregated onto a
        PRODUCT-LESS line keyed by (section, scanned_barcode), flagged
        ``is_unknown``. It rides the SAME idempotency path, so a retried
        unknown scan de-dupes on idempotency_key exactly like a known one —
        it can never create a duplicate unknown line or double the qty.
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
        scan_type = "rescan" if section.state == "variance_rescan" else "initial"
        is_unknown = not product_id
        if is_unknown:
            # No SKU matched the scanned barcode — capture it as a product-less
            # line so the item is never silently dropped. Aggregate per barcode
            # per rack (one line, qty accumulates), mirroring the known flow.
            if not scanned_barcode:
                raise ValidationError(
                    _("A scan with no product must carry a scanned barcode.")
                )
            line = self.search(
                [
                    ("section_id", "=", section.id),
                    ("is_unknown", "=", True),
                    ("scanned_barcode", "=", scanned_barcode),
                ],
                limit=1,
            )
            if not line:
                line = self.create(
                    {
                        "section_id": section.id,
                        "product_id": False,
                        "scanned_barcode": scanned_barcode,
                        "is_unknown": True,
                        "system_qty": 0.0,
                        "counted_qty": 0.0,
                        "unit_cost": 0.0,
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
                "is_unknown": True,
                "scan_event_id": event.id,
                "line_id": line.id,
                "counted_qty": line.counted_qty,
                "scan_count": line.scan_count,
            }
        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            raise ValidationError(_("Product not found."))
        line = self.search(
            [("section_id", "=", section.id), ("product_id", "=", product.id)],
            limit=1,
        )
        if not line:
            # In snapshot mode the SKU already has a snapshot line elsewhere
            # (the catch-all section); a rack line starts at system_qty 0. In
            # quick-count mode there is no snapshot, so freeze system_qty from
            # the current on-hand at first scan and flag off-system overages.
            system_qty = 0.0
            is_unexpected = False
            if section.session_id.count_mode == "scan_to_populate":
                system_qty = section.session_id._product_onhand(product)
                is_unexpected = system_qty <= 0.0
            line = self.create(
                {
                    "section_id": section.id,
                    "product_id": product.id,
                    "system_qty": system_qty,
                    "counted_qty": 0.0,
                    "unit_cost": product.standard_price,
                    "counter_id": self.env.uid,
                    "scanned_at": fields.Datetime.now(),
                    "is_unexpected": is_unexpected,
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
