from odoo.tests.common import TransactionCase


class VivoCountCommon(TransactionCase):
    """Shared fixture: a store, a zone, two rack templates, two users."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env["vivo.count.session"]
        cls.Section = cls.env["vivo.count.section"]
        cls.Line = cls.env["vivo.count.line"]
        cls.Zone = cls.env["vivo.count.zone"]
        cls.Template = cls.env["vivo.count.section.template"]

        cls.location = cls.env["stock.location"].create(
            {
                "name": "Test Store",
                "usage": "internal",
            }
        )

        cls.zone_floor = cls.Zone.create(
            {"name": "Display Floor", "location_id": cls.location.id, "sequence": 1}
        )
        cls.zone_back = cls.Zone.create(
            {"name": "Backroom", "location_id": cls.location.id, "sequence": 2}
        )
        cls.template_a = cls.Template.create(
            {"name": "Rack A1", "zone_id": cls.zone_floor.id, "sequence": 1}
        )
        cls.template_b = cls.Template.create(
            {"name": "Rack A2", "zone_id": cls.zone_floor.id, "sequence": 2}
        )
        cls.template_c = cls.Template.create(
            {"name": "Rack B1", "zone_id": cls.zone_back.id, "sequence": 1}
        )

        cls.group_counter = cls.env.ref("vivo_stock_count.group_vivo_count_counter")
        cls.group_store_mgr = cls.env.ref(
            "vivo_stock_count.group_vivo_count_store_manager"
        )
        cls.group_regional = cls.env.ref(
            "vivo_stock_count.group_vivo_count_regional"
        )
        cls.group_cfoo = cls.env.ref(
            "vivo_stock_count.group_vivo_count_cfoo_audit"
        )

        cls.scanner = cls.env["res.users"].create(
            {
                "name": "Test Scanner",
                "login": "vivo.scanner@test",
                "groups_id": [(6, 0, [cls.group_counter.id])],
            }
        )
        cls.physical = cls.env["res.users"].create(
            {
                "name": "Test Physical Counter",
                "login": "vivo.physical@test",
                "groups_id": [(6, 0, [cls.group_counter.id])],
            }
        )
        cls.store_manager = cls.env["res.users"].create(
            {
                "name": "Test Store Manager",
                "login": "vivo.mgr@test",
                "groups_id": [(6, 0, [cls.group_store_mgr.id])],
            }
        )
        cls.regional = cls.env["res.users"].create(
            {
                "name": "Test Regional",
                "login": "vivo.regional@test",
                "groups_id": [(6, 0, [cls.group_regional.id])],
            }
        )
        cls.cfoo = cls.env["res.users"].create(
            {
                "name": "Test CFOO",
                "login": "vivo.cfoo@test",
                "groups_id": [(6, 0, [cls.group_cfoo.id])],
            }
        )

        cls.product_a = cls.env["product.product"].create(
            {"name": "T-Shirt Red M", "type": "consu", "standard_price": 500.0}
        )
        cls.product_b = cls.env["product.product"].create(
            {"name": "Dress Blue S", "type": "consu", "standard_price": 1500.0}
        )

    def _new_session(self, zone=None):
        return self.Session.create(
            {
                "location_id": self.location.id,
                "zone_id": (zone or self.zone_floor).id if zone is not False else False,
            }
        )

    def _start_and_get_sections(self, session):
        """Start the session and skip the stock.quant snapshot (no real quants in test)."""
        session.action_start()
        return session.section_ids

    def _reconcile_section(self, section, scanner, physical, scan_qty, physical_qty=None):
        if physical_qty is None:
            physical_qty = scan_qty
        section.scanner_id = scanner.id
        section.with_user(scanner).action_start_scanning()
        # Seed a single line directly (mobile-app behaviour comes in Phase 3).
        self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": scan_qty,
                "counted_qty": scan_qty,
                "unit_cost": self.product_a.standard_price,
            }
        )
        section.with_user(scanner).action_finish_scanning()
        section.physical_counter_id = physical.id
        section.with_user(physical).action_submit_physical_count(physical_qty=physical_qty)
        return section
