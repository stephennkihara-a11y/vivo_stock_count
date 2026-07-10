"""Recon report aggregation — SELF-CONTAINED.

Deliberately does NOT use tests/common.py (VivoCountCommon), whose helpers call
methods removed on main (action_confirm_reconcile / action_approve_scan). This
builds its own fixtures so it can run green on the current base:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestReconReport --stop-after-init

The non-negotiable rule under test: counted_qty SUMS across racks while
system_qty and price are taken ONCE per product, and variance = merged counted
minus the single system_qty.
"""
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "recon_report")
class TestReconReport(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env["vivo.count.session"]
        cls.Section = cls.env["vivo.count.section"]
        cls.Line = cls.env["vivo.count.line"]
        cls.Report = cls.env["vivo.count.recon.report"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Recon Test Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Recon Zone", "location_id": cls.location.id}
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Recon Test SKU",
                "type": "consu",
                "list_price": 42.0,
                "barcode": "RECON-TEST-1",
            }
        )

    def _session(self):
        return self.Session.create({"location_id": self.location.id})

    def _section(self, session, name, state="pending_review"):
        # `pending_review` = submitted; no reviewer-note constraint (unlike
        # `reconciled`), so fixtures stay minimal.
        return self.Section.create(
            {
                "session_id": session.id,
                "name": name,
                "zone_id": self.zone.id,
                "state": state,
            }
        )

    def _line(self, section, system, counted):
        return self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product.id,
                "system_qty": system,
                "counted_qty": counted,
            }
        )

    def _row(self, session):
        self.env.flush_all()
        return self.Report.search(
            [("session_id", "=", session.id), ("product_id", "=", self.product.id)]
        )

    def test_counted_sums_but_system_and_price_taken_once(self):
        """Same SKU counted in two submitted racks: counted sums (2+3=5),
        system_qty stays 10 (NOT 20), variance = 5-10 = -5, price once."""
        session = self._session()
        rack_a = self._section(session, "Rack A")
        rack_b = self._section(session, "Rack B")
        self._line(rack_a, system=10.0, counted=2.0)
        self._line(rack_b, system=10.0, counted=3.0)

        row = self._row(session)
        self.assertEqual(len(row), 1, "one merged row per (session, product)")
        self.assertEqual(row.counted_qty, 5.0)      # SUM across racks
        self.assertEqual(row.system_qty, 10.0)      # ONCE — not doubled to 20
        self.assertEqual(row.variance, -5.0)        # 5 - 10, not SUM(difference)
        self.assertEqual(row.price, 42.0)           # once
        self.assertTrue(row.counted)
        self.assertEqual(row.rack_count, 2)
        # Per-rack drill-down reveals both racks.
        self.assertEqual(
            set(row.line_ids.mapped("section_id.name")), {"Rack A", "Rack B"}
        )

    def test_system_qty_uses_full_session_baseline_not_just_submitted(self):
        """THE FIX: baseline lives on an UNSUBMITTED catch-all rack; a submitted
        rack counted the SKU with system_qty 0 on its own line. System Qty must
        be the full-session baseline (10), not 0 — otherwise a counted item reads
        as surplus (variance +4 instead of -6)."""
        session = self._session()
        # Catch-all holds the frozen baseline but is NOT submitted.
        catchall = self._section(session, "Catch-all", state="draft")
        self._line(catchall, system=10.0, counted=0.0)
        # Submitted rack scanned the SKU; its own line carries system_qty 0.
        rack_b = self._section(session, "Rack B", state="pending_review")
        self._line(rack_b, system=0.0, counted=4.0)

        row = self._row(session)
        self.assertEqual(len(row), 1)
        self.assertEqual(row.system_qty, 10.0)   # full-session baseline, NOT 0
        self.assertEqual(row.counted_qty, 4.0)   # submitted sections only
        self.assertEqual(row.variance, -6.0)     # 4 - 10, NOT +4
        self.assertTrue(row.counted)
        self.assertEqual(row.rack_count, 1)

    def test_baseline_only_sku_surfaces_as_not_counted(self):
        """A SKU whose only line is the (unsubmitted) baseline, never counted in
        any submitted rack, still appears — as a Not Counted shortage candidate
        with its true system_qty."""
        session = self._session()
        catchall = self._section(session, "Catch-all", state="draft")
        self._line(catchall, system=8.0, counted=0.0)
        # A submitted rack exists but never counted this SKU.
        self._section(session, "Rack B", state="pending_review")

        row = self._row(session)
        self.assertEqual(len(row), 1)
        self.assertEqual(row.system_qty, 8.0)
        self.assertEqual(row.counted_qty, 0.0)
        self.assertFalse(row.counted)
        self.assertEqual(row.variance, -8.0)
        self.assertEqual(row.rack_count, 0)

    def test_unsubmitted_racks_are_excluded(self):
        """Draft / scanning / excluded racks are not merged."""
        session = self._session()
        submitted = self._section(session, "Rack A", state="pending_review")
        draft = self._section(session, "Rack D", state="draft")
        scanning = self._section(session, "Rack S", state="scanning")
        self._line(submitted, system=10.0, counted=4.0)
        self._line(draft, system=10.0, counted=99.0)      # must be ignored
        self._line(scanning, system=10.0, counted=88.0)   # must be ignored

        row = self._row(session)
        self.assertEqual(row.counted_qty, 4.0)
        self.assertEqual(row.rack_count, 1)

    def test_not_counted_detection(self):
        """A SKU in the baseline of a submitted rack but counted 0 everywhere is
        flagged not-counted (shortage candidate)."""
        session = self._session()
        rack_a = self._section(session, "Rack A")
        self._line(rack_a, system=7.0, counted=0.0)

        row = self._row(session)
        self.assertEqual(row.counted_qty, 0.0)
        self.assertFalse(row.counted)
        self.assertEqual(row.variance, -7.0)
        self.assertEqual(row.rack_count, 0)
