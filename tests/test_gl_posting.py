"""Phase 4 — Apply: GL posting and reconciliation generation.

Covers acceptance criteria #1, #9, #15, #16, plus the Risk #4 atomicity
requirement (mid-batch failure rolls back GL writes; session stays at
'approved' for retry).
"""
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase4")
class TestGlPosting(VivoCountCommon):

    def _approved_session_with_count(self, counted_qty=8.0, system_qty=10.0):
        """Build a session that ends approved with a single line:
        counted_qty for product_a, system snapshot of system_qty.
        """
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        s0 = sections[0]
        s0.scanner_id = self.scanner.id
        s0.with_user(self.scanner).action_start_scanning()
        self.Line.create(
            {
                "section_id": s0.id,
                "product_id": self.product_a.id,
                "system_qty": system_qty,
                "counted_qty": counted_qty,
                "unit_cost": self.product_a.standard_price,
                "variance_reason": "miscount" if counted_qty != system_qty else False,
            }
        )
        s0.with_user(self.scanner).action_finish_scanning()
        s0.physical_counter_id = self.physical.id
        s0.with_user(self.physical).action_submit_physical_count(
            physical_qty=counted_qty
        )
        # Reconcile the second section trivially.
        self._reconcile_section(sections[1], self.scanner, self.physical, 0, 0)
        # The store manager reviews (becomes reviewer_id) and approves, so the
        # reconciliation header captures them as reviewer AND approver.
        session.with_user(self.store_manager).action_submit_for_review()
        session.with_user(self.store_manager).action_approve()
        return session

    # ------------------------------------------------------------------
    # AC #15 — reconciliation auto-generated on Apply
    # ------------------------------------------------------------------
    def test_apply_generates_reconciliation_automatically(self):
        session = self._approved_session_with_count(counted_qty=8.0, system_qty=10.0)
        self.assertFalse(session.reconciliation_id)
        session.with_user(self.store_manager).action_apply()
        self.assertEqual(session.state, "applied")
        self.assertTrue(session.reconciliation_id)
        self.assertEqual(session.reconciliation_id.state, "generated")
        self.assertTrue(session.reconciliation_id.generated_at)
        # The session field references the recon; the recon back-refs to session.
        self.assertEqual(session.reconciliation_id.session_id, session)

    # ------------------------------------------------------------------
    # AC #16 — qty_before, qty_after, value_before, value_after, variance
    # ------------------------------------------------------------------
    def test_reconciliation_line_carries_before_after(self):
        session = self._approved_session_with_count(counted_qty=8.0, system_qty=10.0)
        session.with_user(self.store_manager).action_apply()
        recon = session.reconciliation_id
        # One reconciliation line per product. product_a is the only one
        # with a non-zero count.
        recon_line = recon.line_ids.filtered(
            lambda l: l.product_id == self.product_a
        )
        self.assertEqual(len(recon_line), 1)
        self.assertEqual(recon_line.qty_before, 10.0)
        self.assertEqual(recon_line.qty_after, 8.0)
        self.assertEqual(recon_line.qty_variance, -2.0)
        self.assertEqual(recon_line.value_before, 10.0 * 500.0)
        self.assertEqual(recon_line.value_after, 8.0 * 500.0)
        self.assertEqual(recon_line.value_variance, -1000.0)
        self.assertEqual(recon_line.variance_type, "shortage")
        self.assertTrue(recon_line.has_variance)

    def test_reconciliation_overage_classified_correctly(self):
        session = self._approved_session_with_count(counted_qty=12.0, system_qty=10.0)
        session.with_user(self.store_manager).action_apply()
        recon_line = session.reconciliation_id.line_ids.filtered(
            lambda l: l.product_id == self.product_a
        )
        self.assertEqual(recon_line.variance_type, "overage")
        self.assertEqual(recon_line.qty_variance, 2.0)
        self.assertEqual(recon_line.value_variance, 1000.0)

    def test_reconciliation_no_variance_when_match(self):
        session = self._approved_session_with_count(counted_qty=10.0, system_qty=10.0)
        session.with_user(self.store_manager).action_apply()
        recon_line = session.reconciliation_id.line_ids.filtered(
            lambda l: l.product_id == self.product_a
        )
        self.assertEqual(recon_line.variance_type, "none")
        self.assertFalse(recon_line.has_variance)

    def test_reconciliation_header_captures_participants(self):
        session = self._approved_session_with_count()
        session.with_user(self.store_manager).action_apply()
        recon = session.reconciliation_id
        self.assertIn(self.scanner, recon.scanner_ids)
        self.assertIn(self.physical, recon.physical_counter_ids)
        self.assertEqual(recon.reviewer_id, self.store_manager)
        self.assertEqual(recon.approver_id, self.store_manager)
        self.assertEqual(recon.applied_by_id, self.store_manager)

    def test_reconciliation_aggregates_per_product_across_sections(self):
        """A product split across two racks gets ONE recon line aggregating both."""
        session = self._new_session(zone=False)  # whole-store, 3 sections
        sections = self._start_and_get_sections(session)
        s0, s1, s2 = sections[0], sections[1], sections[2]
        # Section 0: snapshot-style line, system=10, counted=0
        s0.scanner_id = self.scanner.id
        s0.with_user(self.scanner).action_start_scanning()
        self.Line.create(
            {
                "section_id": s0.id,
                "product_id": self.product_a.id,
                "system_qty": 10.0,
                "counted_qty": 0.0,
                "unit_cost": 500.0,
            }
        )
        s0.with_user(self.scanner).action_finish_scanning()
        s0.physical_counter_id = self.physical.id
        s0.with_user(self.physical).action_submit_physical_count(physical_qty=0)
        # Sections 1 + 2: counted lines, system=0, counted=4 and 5
        for sec, qty in [(s1, 4.0), (s2, 5.0)]:
            sec.scanner_id = self.scanner.id
            sec.with_user(self.scanner).action_start_scanning()
            self.Line.create(
                {
                    "section_id": sec.id,
                    "product_id": self.product_a.id,
                    "system_qty": 0.0,
                    "counted_qty": qty,
                    "unit_cost": 500.0,
                    "variance_reason": "miscount",
                }
            )
            sec.with_user(self.scanner).action_finish_scanning()
            sec.physical_counter_id = self.physical.id
            sec.with_user(self.physical).action_submit_physical_count(
                physical_qty=qty
            )
        session.action_submit_for_review()
        session.with_user(self.store_manager).action_approve()
        session.with_user(self.store_manager).action_apply()

        recon_lines = session.reconciliation_id.line_ids.filtered(
            lambda l: l.product_id == self.product_a
        )
        self.assertEqual(len(recon_lines), 1, "Expected one rollup line per SKU")
        self.assertEqual(recon_lines.qty_before, 10.0)
        self.assertEqual(recon_lines.qty_after, 9.0)  # 4 + 5
        self.assertEqual(recon_lines.qty_variance, -1.0)
        # Split across two sections -> section_id blank, zone_id blank.
        self.assertFalse(recon_lines.section_id)

    # ------------------------------------------------------------------
    # AC #9 — stock.quant matches counted_qty after Apply
    # ------------------------------------------------------------------
    def test_apply_writes_counted_qty_to_stock_quant(self):
        # Seed an initial quant at the session location.
        self.env["stock.quant"].with_context(inventory_mode=True).create(
            {
                "product_id": self.product_a.id,
                "location_id": self.location.id,
                "inventory_quantity": 10.0,
            }
        ).action_apply_inventory()
        session = self._approved_session_with_count(counted_qty=8.0, system_qty=10.0)
        session.with_user(self.store_manager).action_apply()
        quant = self.env["stock.quant"].search(
            [
                ("product_id", "=", self.product_a.id),
                ("location_id", "=", self.location.id),
            ]
        )
        # quant.quantity should now equal counted_qty.
        self.assertEqual(sum(quant.mapped("quantity")), 8.0)

    def test_apply_creates_stock_move_record(self):
        """The native inventory pipeline produces a stock.move for the
        delta — this is the audit trail the spec relies on for GL posting.
        """
        self.env["stock.quant"].with_context(inventory_mode=True).create(
            {
                "product_id": self.product_a.id,
                "location_id": self.location.id,
                "inventory_quantity": 10.0,
            }
        ).action_apply_inventory()
        moves_before = self.env["stock.move.line"].search_count(
            [("product_id", "=", self.product_a.id)]
        )
        session = self._approved_session_with_count(counted_qty=8.0, system_qty=10.0)
        session.with_user(self.store_manager).action_apply()
        moves_after = self.env["stock.move.line"].search_count(
            [("product_id", "=", self.product_a.id)]
        )
        self.assertGreater(moves_after, moves_before)

    # ------------------------------------------------------------------
    # Atomic rollback (Risk #4)
    # ------------------------------------------------------------------
    def test_apply_rolls_back_on_reconciliation_failure(self):
        """If reconciliation generation raises mid-Apply, stock.quant
        writes are rolled back and the session stays 'approved' so the
        operator can retry without double-posting.
        """
        self.env["stock.quant"].with_context(inventory_mode=True).create(
            {
                "product_id": self.product_a.id,
                "location_id": self.location.id,
                "inventory_quantity": 10.0,
            }
        ).action_apply_inventory()
        session = self._approved_session_with_count(counted_qty=8.0, system_qty=10.0)
        with patch.object(
            self.env.registry["vivo.count.session"],
            "_generate_reconciliation",
            side_effect=RuntimeError("simulated mid-Apply failure"),
        ):
            with self.assertRaises(RuntimeError):
                session.with_user(self.store_manager).action_apply()
        session.invalidate_recordset()
        self.assertEqual(session.state, "approved")
        self.assertFalse(session.reconciliation_id)
        quant_qty = sum(
            self.env["stock.quant"]
            .search(
                [
                    ("product_id", "=", self.product_a.id),
                    ("location_id", "=", self.location.id),
                ]
            )
            .mapped("quantity")
        )
        # Quant stayed at the pre-Apply value.
        self.assertEqual(quant_qty, 10.0)

    # ------------------------------------------------------------------
    # AC #1 reinforced at the Apply layer with the full Phase 4 plumbing
    # ------------------------------------------------------------------
    def test_pure_counter_blocked_from_apply_after_approval(self):
        session = self._approved_session_with_count()
        # A pure counter who never scanned this session.
        bystander = self.env["res.users"].create(
            {
                "name": "Bystander Counter",
                "login": "vivo.bystander.p4@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        # The record-rule scoping would also block read; assert either
        # AccessError or UserError is raised.
        from odoo.exceptions import UserError
        with self.assertRaises((AccessError, UserError)):
            session.with_user(bystander).action_apply()
        # Nothing applied.
        self.assertNotEqual(session.state, "applied")
