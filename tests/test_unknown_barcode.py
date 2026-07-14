"""Unknown-barcode capture (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestUnknownBarcode --stop-after-init

A scan whose barcode matches no product must NEVER be silently dropped. It is
captured as a PRODUCT-LESS count line (is_unknown=True) that rides the same
idempotent scan path, aggregates per barcode per rack, and surfaces in the
session recon report as a positive (surplus) variance. Capture only — no
product master is ever created.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "unknown_barcode")
class TestUnknownBarcode(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.Recon = cls.env["vivo.count.recon.report"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Unknown Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Unknown Zone", "location_id": cls.location.id}
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
        )
        cls.section = cls.env["vivo.count.section"].create(
            {
                "session_id": cls.session.id,
                "name": "Rack UNK",
                "zone_id": cls.zone.id,
                "state": "scanning",
            }
        )

    def _scan_unknown(self, barcode, key, qty=1):
        return self.Line.record_scan(
            section_id=self.section.id,
            product_id=None,
            scanned_qty=qty,
            idempotency_key=key,
            scanned_barcode=barcode,
        )

    def _unknown_lines(self, barcode="BOGUS-001"):
        return self.Line.search(
            [
                ("section_id", "=", self.section.id),
                ("is_unknown", "=", True),
                ("scanned_barcode", "=", barcode),
            ]
        )

    # ------------------------------------------------------------------
    def test_unknown_scan_creates_productless_line(self):
        res = self._scan_unknown("BOGUS-001", "u-1")
        self.assertTrue(res.get("is_unknown"))
        self.assertFalse(res.get("idempotent"))

        lines = self._unknown_lines()
        self.assertEqual(len(lines), 1, "exactly one product-less line")
        line = lines
        self.assertFalse(line.product_id, "no SKU is linked")
        self.assertTrue(line.is_unknown)
        self.assertEqual(line.scanned_barcode, "BOGUS-001")
        self.assertEqual(line.product_title, "Unknown")
        self.assertEqual(line.counted_qty, 1.0)
        self.assertEqual(line.system_qty, 0.0)
        # Variance is the full counted qty — a positive surplus.
        self.assertEqual(line.difference, 1.0)

    def test_second_scan_same_barcode_increments_one_line(self):
        self._scan_unknown("BOGUS-001", "u-1")
        self._scan_unknown("BOGUS-001", "u-2")  # distinct client uuid, same barcode
        lines = self._unknown_lines()
        self.assertEqual(len(lines), 1, "aggregate per barcode per rack — one line")
        self.assertEqual(lines.counted_qty, 2.0, "qty accumulates")
        self.assertEqual(lines.scan_count, 2)

    def test_unknown_replay_is_idempotent(self):
        first = self._scan_unknown("BOGUS-001", "u-dup")
        self.assertFalse(first.get("idempotent"))
        replay = self._scan_unknown("BOGUS-001", "u-dup")  # same uuid replayed
        self.assertTrue(replay.get("idempotent"))
        lines = self._unknown_lines()
        self.assertEqual(len(lines), 1, "replay does not duplicate the line")
        self.assertEqual(lines.counted_qty, 1.0, "replay does not double the qty")

    def test_distinct_barcodes_are_distinct_lines(self):
        self._scan_unknown("BOGUS-001", "u-1")
        self._scan_unknown("BOGUS-002", "u-2")
        self.assertEqual(len(self._unknown_lines("BOGUS-001")), 1)
        self.assertEqual(len(self._unknown_lines("BOGUS-002")), 1)

    def test_recon_report_surfaces_unknown_with_rack_breakdown(self):
        self._scan_unknown("BOGUS-001", "u-1")
        self._scan_unknown("BOGUS-001", "u-2")
        # Unknowns surface in the session recon report once the rack is
        # submitted for review (same rule as known counted lines).
        self.section.state = "pending_review"
        self.env.flush_all()

        rows = self.Recon.search(
            [("session_id", "=", self.session.id), ("is_unknown", "=", True)]
        )
        self.assertEqual(len(rows), 1, "one unknown recon row per (session, barcode)")
        row = rows
        self.assertFalse(row.product_id)
        self.assertEqual(row.product_title, "Unknown")
        self.assertEqual(row.scanned_barcode, "BOGUS-001")
        self.assertEqual(row.system_qty, 0.0)
        self.assertEqual(row.counted_qty, 2.0)
        self.assertEqual(row.variance, 2.0, "unknowns read as a positive surplus")
        self.assertIn("Rack UNK", row.rack_breakdown)

    def test_recon_report_unknown_filter(self):
        self._scan_unknown("BOGUS-001", "u-1")
        self.section.state = "pending_review"
        self.env.flush_all()
        filtered = self.Recon.search(
            [("session_id", "=", self.session.id), ("is_unknown", "=", True)]
        )
        self.assertTrue(filtered, "the Unknown filter domain returns the capture")
        self.assertTrue(all(r.is_unknown for r in filtered))
