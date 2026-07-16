"""Recon report column grand totals (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestReconColumnTotals --stop-after-init

The list footer sum= only aggregates the records the list has LOADED. With a
default page size (e.g. 40) a 1000-row recon report would total ONE page and
present it as the whole report. The list therefore sets limit="5000" so a
single-session recon (one row per product, ~1000 rows) always loads in one page
and the footer sums EVERY row.

This test pins the data-level grand totals over ALL rows (what a full-page footer
must show): system_qty, counted_qty, variance, total_retail_price and
total_cost_price summed across every row of the session, including an unknown row
which contributes 0 to the money totals.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "recon_column_totals")
class TestReconColumnTotals(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.Section = cls.env["vivo.count.section"]
        cls.Recon = cls.env["vivo.count.recon.report"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Totals Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Totals Zone", "location_id": cls.location.id}
        )
        cls.pa = cls.env["product.product"].create(
            {"name": "SKU A", "type": "consu", "list_price": 10.0, "standard_price": 4.0}
        )
        cls.pb = cls.env["product.product"].create(
            {"name": "SKU B", "type": "consu", "list_price": 20.0, "standard_price": 8.0}
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
        )

    def _rack(self, name):
        return self.Section.create(
            {
                "session_id": self.session.id,
                "name": name,
                "zone_id": self.zone.id,
                "state": "scanning",
            }
        )

    def _scan(self, sec, product, qty, key, barcode=None):
        return self.Line.record_scan(
            section_id=sec.id,
            product_id=product,
            scanned_qty=qty,
            idempotency_key=key,
            scanned_barcode=barcode,
        )

    def test_footer_totals_are_grand_totals_over_all_rows(self):
        # SKU A counted across TWO racks (5 total); SKU B in one; one unknown.
        a1 = self._rack("A1")
        a2 = self._rack("A2")
        b1 = self._rack("B1")
        u1 = self._rack("U1")
        self._scan(a1, self.pa.id, 3, "t-a1")
        self._scan(a2, self.pa.id, 2, "t-a2")
        self._scan(b1, self.pb.id, 1, "t-b1")
        self._scan(u1, None, 4, "t-u1", barcode="BOGUS-TOT")
        (a1 | a2 | b1 | u1).action_finish_scanning()
        self.env.flush_all()

        rows = self.Recon.search([("session_id", "=", self.session.id)])
        # 3 rows: SKU A (merged over 2 racks), SKU B, one unknown.
        self.assertEqual(len(rows), 3)

        # Grand totals over ALL rows — what the single-page footer must show.
        self.assertEqual(sum(rows.mapped("counted_qty")), 10.0)   # 5 + 1 + 4
        self.assertEqual(sum(rows.mapped("variance")), 10.0)      # 5 + 1 + 4
        self.assertEqual(sum(rows.mapped("system_qty")), 0.0)
        # Retail: A 5*10 + B 1*20 + unknown 0 = 70.
        self.assertEqual(sum(rows.mapped("total_retail_price")), 70.0)
        # Cost: A 5*4 + B 1*8 + unknown 0 = 28.
        self.assertEqual(sum(rows.mapped("total_cost_price")), 28.0)

        # The unknown row itself carries 0 money totals but real qty.
        unknown = rows.filtered("is_unknown")
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown.total_retail_price, 0.0)
        self.assertEqual(unknown.total_cost_price, 0.0)
        self.assertEqual(unknown.counted_qty, 4.0)

        # Cost is taken ONCE per product (MAX unit_cost), never summed per rack:
        # SKU A appears in two racks but its recon cost stays 4, not 8.
        sku_a = rows.filtered(lambda r: r.product_id == self.pa)
        self.assertEqual(sku_a.cost, 4.0)
        self.assertEqual(sku_a.total_cost_price, 20.0)  # variance 5 * cost 4
