"""Scan-line stepper adjust path (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestScanLineSteppers --stop-after-init

The PWA + / − steppers do NOT use a new endpoint — they reuse ``record_scan``
with a +1 / -1 quantity (idempotent, queue-backed) and, for a decrement that
reaches zero, the existing ``action_delete_scan_line`` unlink path. This test
pins that server behaviour for an UNKNOWN line so a future change (e.g. a
positive-only constraint) can't silently break the steppers.

(The stepper markup / tap handling is front-end and is verified manually on a
device — see the branch notes.)
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "scan_steppers")
class TestScanLineSteppers(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Stepper Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Stepper Zone", "location_id": cls.location.id}
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
        )
        cls.section = cls.env["vivo.count.section"].create(
            {
                "session_id": cls.session.id,
                "name": "Rack STP",
                "zone_id": cls.zone.id,
                "state": "scanning",
            }
        )

    def _scan_unknown(self, key, qty, barcode="STP-001"):
        return self.Line.record_scan(
            section_id=self.section.id,
            product_id=None,
            scanned_qty=qty,
            idempotency_key=key,
            scanned_barcode=barcode,
        )

    def _line(self, barcode="STP-001"):
        return self.Line.search(
            [
                ("section_id", "=", self.section.id),
                ("is_unknown", "=", True),
                ("scanned_barcode", "=", barcode),
            ]
        )

    def test_unknown_increment_decrement_then_zero_removes(self):
        # "+" twice -> qty 2 (each tap is a fresh-uuid scan of +1).
        self._scan_unknown("stp-1", 1)
        self._scan_unknown("stp-2", 1)
        line = self._line()
        self.assertEqual(len(line), 1)
        self.assertEqual(line.counted_qty, 2.0)

        # "−" (not to zero) -> qty 1, via the SAME scan path with -1.
        self._scan_unknown("stp-3", -1)
        line = self._line()
        self.assertEqual(len(line), 1, "still one aggregated unknown line")
        self.assertEqual(line.counted_qty, 1.0)

        # Idempotency holds for adjust too: replaying the -1 uuid is a no-op.
        self._scan_unknown("stp-3", -1)
        self.assertEqual(self._line().counted_qty, 1.0, "replay must not double-adjust")

        # "−" that reaches zero removes the line (the ✕ / unlink path).
        line.action_delete_scan_line()
        self.assertFalse(self._line(), "decrement-to-zero removes the line")
