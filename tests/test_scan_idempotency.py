"""Scan idempotency — the piece that MUST be correct (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestScanIdempotency --stop-after-init

The PWA local-first queue replays a scan after any ambiguous failure, so the
server endpoint MUST de-dupe on the client uuid (idempotency_key): reposting the
same uuid creates exactly ONE line and never doubles the quantity. Getting this
wrong inflates the count, which is worse than losing a scan.

(The local-first / offline-queue / per-line-status behaviour is front-end and is
verified manually on a device — see the branch notes.)
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "scan_resilience")
class TestScanIdempotency(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Idem Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Idem Zone", "location_id": cls.location.id}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Idem SKU", "type": "consu"}
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
        )
        cls.section = cls.env["vivo.count.section"].create(
            {
                "session_id": cls.session.id,
                "name": "Rack Idem",
                "zone_id": cls.zone.id,
                "state": "scanning",
            }
        )

    def _scan(self, key, qty=1):
        return self.Line.record_scan(
            section_id=self.section.id,
            product_id=self.product.id,
            scanned_qty=qty,
            idempotency_key=key,
        )

    def _lines(self):
        return self.Line.search(
            [
                ("section_id", "=", self.section.id),
                ("product_id", "=", self.product.id),
            ]
        )

    def test_same_uuid_twice_is_one_line_no_double(self):
        first = self._scan("uuid-A", qty=1)
        self.assertFalse(first.get("idempotent"))
        replay = self._scan("uuid-A", qty=1)  # exact same client uuid
        self.assertTrue(replay.get("idempotent"))

        lines = self._lines()
        self.assertEqual(len(lines), 1, "one line only")
        self.assertEqual(lines.counted_qty, 1.0, "qty must NOT double on replay")

    def test_distinct_uuids_accumulate(self):
        self._scan("uuid-1", qty=1)
        self._scan("uuid-2", qty=1)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.counted_qty, 2.0)
