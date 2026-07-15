"""Cost + Total Retail Price on the line and the recon report (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestReconCostRetail --stop-after-init

Two valuations of a variance: variance_value is at COST (difference * unit_cost),
total_retail_price is at RETAIL (difference * lst_price). On the recon report the
per-product cost is taken ONCE (MAX of the snapshotted line unit_cost, never
summed across racks) and total_retail_price = variance * price. Unknown rows carry
no product, so cost and total_retail_price are 0.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "recon_cost_retail")
class TestReconCostRetail(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.Section = cls.env["vivo.count.section"]
        cls.Recon = cls.env["vivo.count.recon.report"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Retail Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Retail Zone", "location_id": cls.location.id}
        )
        # list_price 20 -> lst_price 20; standard_price 8 -> snapshotted unit_cost 8.
        cls.product = cls.env["product.product"].create(
            {
                "name": "Retail SKU",
                "type": "consu",
                "list_price": 20.0,
                "standard_price": 8.0,
            }
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
        )

    def _section(self, name):
        return self.Section.create(
            {
                "session_id": self.session.id,
                "name": name,
                "zone_id": self.zone.id,
                "state": "scanning",
            }
        )

    # 1) line.total_retail_price == difference * lst_price -------------------
    def test_line_total_retail_price(self):
        sec = self._section("R1")
        self.Line.record_scan(
            section_id=sec.id,
            product_id=self.product.id,
            scanned_qty=5,
            idempotency_key="rr-1",
        )
        line = self.Line.search(
            [("section_id", "=", sec.id), ("product_id", "=", self.product.id)]
        )
        line.system_qty = 2.0  # difference = counted 5 - system 2 = 3
        self.assertEqual(line.difference, 3.0)
        self.assertEqual(line.lst_price, 20.0)
        self.assertEqual(line.total_retail_price, 60.0, "3 * 20 retail")
        # The cost-valued variance stays separate and unchanged.
        self.assertEqual(line.unit_cost, 8.0)
        self.assertEqual(line.variance_value, 24.0, "3 * 8 cost")

    # 2) recon: cost taken ONCE (not summed); total_retail = variance * price -
    def test_recon_cost_once_and_total_retail(self):
        r1 = self._section("RR-A")
        r2 = self._section("RR-B")
        self.Line.record_scan(
            section_id=r1.id, product_id=self.product.id, scanned_qty=3,
            idempotency_key="rr-a",
        )
        self.Line.record_scan(
            section_id=r2.id, product_id=self.product.id, scanned_qty=2,
            idempotency_key="rr-b",
        )
        (r1 | r2).action_finish_scanning()  # -> pending_review (submitted)
        self.env.flush_all()

        row = self.Recon.search(
            [
                ("session_id", "=", self.session.id),
                ("product_id", "=", self.product.id),
            ]
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row.counted_qty, 5.0, "3 + 2 merged across racks")
        self.assertEqual(row.system_qty, 0.0)
        self.assertEqual(row.variance, 5.0)
        self.assertEqual(row.price, 20.0)
        # Cost is MAX(unit_cost) over the product's lines — 8, NOT 16 (summed).
        self.assertEqual(row.cost, 8.0, "cost taken once per product, never summed")
        self.assertEqual(row.total_retail_price, 100.0, "variance 5 * price 20")

    # 3) unknown recon row -> cost 0 and total_retail_price 0 ----------------
    def test_recon_unknown_cost_and_retail_zero(self):
        sec = self._section("RR-U")
        self.Line.record_scan(
            section_id=sec.id,
            product_id=None,
            scanned_qty=4,
            idempotency_key="rr-u",
            scanned_barcode="BOGUS-RR",
        )
        sec.action_finish_scanning()
        self.env.flush_all()

        row = self.Recon.search(
            [
                ("session_id", "=", self.session.id),
                ("is_unknown", "=", True),
                ("scanned_barcode", "=", "BOGUS-RR"),
            ]
        )
        self.assertEqual(len(row), 1)
        self.assertEqual(row.cost, 0.0, "unknown row has no known cost")
        self.assertEqual(row.total_retail_price, 0.0, "unknown row has no retail price")
