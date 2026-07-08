"""Dual counting mode: snapshot (full inventory) vs scan_to_populate (quick).

Snapshot mode pre-loads every expected SKU at start (shortage detection).
Quick Count starts empty and builds lines as scans arrive, freezing system_qty
from live on-hand on first scan and flagging off-system overages. The
scan/physical mismatch -> auditor path is mode-independent.
"""
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestCountMode(VivoCountCommon):

    def _seed_onhand(self, product, qty):
        self.env["stock.quant"].with_context(inventory_mode=True).create(
            {
                "product_id": product.id,
                "location_id": self.location.id,
                "inventory_quantity": qty,
            }
        ).action_apply_inventory()

    def _quick_session_started(self):
        session = self._new_session()
        session.count_mode = "scan_to_populate"
        session.action_start()
        return session

    def _open_section(self, session):
        section = session.section_ids[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        return section

    def _scan(self, section, product, qty, key):
        return self.env["vivo.count.line"].with_user(self.scanner).record_scan(
            section_id=section.id,
            product_id=product.id,
            scanned_qty=qty,
            idempotency_key=key,
        )

    # ------------------------------------------------------------------
    # Snapshot mode unchanged
    # ------------------------------------------------------------------
    def test_snapshot_mode_preloads_all_skus(self):
        self._seed_onhand(self.product_a, 10)
        self._seed_onhand(self.product_b, 4)
        session = self._new_session()
        self.assertEqual(session.count_mode, "snapshot")
        session.action_start()
        lines = session.line_ids
        self.assertEqual(len(lines), 2)
        pa = lines.filtered(lambda l: l.product_id == self.product_a)
        self.assertEqual(pa.system_qty, 10.0)

    # ------------------------------------------------------------------
    # Quick Count (scan_to_populate)
    # ------------------------------------------------------------------
    def test_scan_to_populate_starts_empty(self):
        self._seed_onhand(self.product_a, 10)
        session = self._quick_session_started()
        self.assertEqual(len(session.line_ids), 0)

    def test_first_scan_creates_line_with_frozen_onhand(self):
        self._seed_onhand(self.product_a, 10)
        session = self._quick_session_started()
        section = self._open_section(session)
        self._scan(section, self.product_a, 3, "k1")
        lines = section.line_ids
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.counted_qty, 3.0)
        self.assertEqual(lines.system_qty, 10.0)
        self.assertFalse(lines.is_unexpected)

    def test_second_scan_increments_same_line(self):
        self._seed_onhand(self.product_a, 10)
        session = self._quick_session_started()
        section = self._open_section(session)
        self._scan(section, self.product_a, 3, "k1")
        self._scan(section, self.product_a, 2, "k2")
        self.assertEqual(len(section.line_ids), 1)
        self.assertEqual(section.line_ids.counted_qty, 5.0)

    def test_unexpected_item_zero_onhand_is_flagged(self):
        # product_b has no on-hand at the location.
        session = self._quick_session_started()
        section = self._open_section(session)
        self._scan(section, self.product_b, 4, "k1")
        line = section.line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.counted_qty, 4.0)
        self.assertEqual(line.system_qty, 0.0)
        self.assertTrue(line.is_unexpected)

    def test_approve_reject_flow_is_mode_independent(self):
        """The approve-then-review flow works the same in quick-count mode."""
        self._seed_onhand(self.product_a, 5)
        session = self._quick_session_started()
        section = self._open_section(session)
        self._scan(section, self.product_a, 5, "k1")
        section.with_user(self.scanner).action_finish_scanning()
        self.assertEqual(section.state, "physical_review")
        # Second person rejects -> back to scanning.
        section.with_user(self.physical).action_reject_scan()
        self.assertEqual(section.state, "scanning")
        self.assertEqual(section.rescan_count, 1)
        # Retry: finish, approve, review.
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_approve_scan()
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="ok")
        self.assertEqual(section.state, "reconciled")

    def test_uncounted_rollup_is_na_in_quick_mode(self):
        self._seed_onhand(self.product_a, 10)
        session = self._quick_session_started()
        section = self._open_section(session)
        self._scan(section, self.product_a, 3, "k1")
        # No full snapshot -> no shortage rollup, and no false shortages.
        self.assertEqual(session.uncounted_sku_count, 0)
        self.assertEqual(session.uncounted_shortage_value, 0.0)

    # ------------------------------------------------------------------
    # Mode is frozen once counting starts
    # ------------------------------------------------------------------
    def test_count_mode_locked_after_start(self):
        session = self._new_session()
        session.action_start()
        self.assertEqual(session.state, "in_progress")
        with self.assertRaises(UserError):
            session.count_mode = "scan_to_populate"

    def test_count_mode_editable_while_draft(self):
        session = self._new_session()
        session.count_mode = "scan_to_populate"
        self.assertEqual(session.count_mode, "scan_to_populate")
        # Setting to the same value after start is a no-op, not an error.
        session.action_start()
        session.count_mode = "scan_to_populate"
