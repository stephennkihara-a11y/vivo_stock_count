"""Delete-scan-line guard — SELF-CONTAINED.

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestScanDeleteLine --stop-after-init

Rule under test: a scanned line may be deleted only while its rack is in
'scanning'; once submitted the delete is blocked server-side (unlink override),
so a variance cannot be erased after review.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "scan_delete")
class TestScanDeleteLine(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env["vivo.count.session"]
        cls.Section = cls.env["vivo.count.section"]
        cls.Line = cls.env["vivo.count.line"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Del Test Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Del Zone", "location_id": cls.location.id}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Del SKU", "type": "consu"}
        )
        cls.session = cls.Session.create({"location_id": cls.location.id})

    def _section(self, state):
        return self.Section.create(
            {
                "session_id": self.session.id,
                "name": "Rack %s" % state,
                "zone_id": self.zone.id,
                "state": state,
            }
        )

    def _line(self, section):
        return self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product.id,
                "system_qty": 5.0,
                "counted_qty": 2.0,
            }
        )

    def test_delete_allowed_while_scanning(self):
        section = self._section("scanning")
        line = self._line(section)
        res = line.action_delete_scan_line()
        self.assertTrue(res.get("deleted"))
        self.assertFalse(line.exists())

    def test_delete_blocked_once_submitted(self):
        section = self._section("pending_review")
        line = self._line(section)
        # The guarded action is blocked...
        with self.assertRaises(UserError):
            line.action_delete_scan_line()
        self.assertTrue(line.exists())
        # ...and so is a direct ORM unlink (belt and braces — same guard).
        with self.assertRaises(UserError):
            line.unlink()
        self.assertTrue(line.exists())
