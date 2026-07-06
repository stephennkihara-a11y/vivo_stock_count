"""Section-level reconcile-before-submit rule and SoD on the section.

Covers acceptance criteria: #2, #3, #5 (re-scan loop), #11 (variance reasons),
#14 (soft-lock visibility).
"""
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase1")
class TestSectionReconciliation(VivoCountCommon):

    def test_section_reconciles_when_totals_match(self):
        """AC #2 happy path."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        self._reconcile_section(section, self.scanner, self.physical, 7)
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.is_reconciled)
        self.assertTrue(section.reconciled_at)

    def test_section_bounces_to_rescan_on_mismatch(self):
        """AC #2 negative + AC #5 re-scan loop."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": 5.0,
                "counted_qty": 5.0,
                "unit_cost": self.product_a.standard_price,
            }
        )
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=4)
        self.assertEqual(section.state, "variance_rescan")
        self.assertEqual(section.rescan_count, 1)
        # Loop: re-scan and reconcile.
        section.with_user(self.scanner).action_start_scanning()
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_submit_physical_count(physical_qty=5)
        self.assertEqual(section.state, "reconciled")

    def test_segregation_of_duties_on_section(self):
        """AC #3: scanner and physical counter cannot be the same user."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        with self.assertRaises(ValidationError):
            section.physical_counter_id = self.scanner.id

    def test_constraint_blocks_reconciled_state_with_mismatch(self):
        """AC #2: direct state write to reconciled with mismatched totals fails."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.physical_total_qty = 99
        with self.assertRaises(ValidationError):
            section.state = "reconciled"

    def test_soft_lock_visibility(self):
        """AC #14: section locked by user A cannot be acquired by user B
        within the idle-lock window."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.with_user(self.scanner).acquire_lock()
        self.assertEqual(section.locked_by_id, self.scanner)
        with self.assertRaises(UserError):
            section.with_user(self.physical).acquire_lock()
        section.with_user(self.scanner).release_lock()
        # Second user can now acquire.
        section.with_user(self.physical).acquire_lock()
        self.assertEqual(section.locked_by_id, self.physical)

    def test_variance_reason_required_for_non_zero_difference(self):
        """AC #11: a counted variance cannot reconcile without a reason.

        The gate now lives at the section review step (pending_review ->
        reconciled), so confirmation is blocked, not approval.
        """
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        # Create a line with a real variance (counted != system).
        self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": 10.0,
                "counted_qty": 9.0,
                "unit_cost": self.product_a.standard_price,
            }
        )
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=9)
        # Match with a genuine variance -> held for auditor review.
        self.assertEqual(section.state, "pending_review")
        with self.assertRaises(ValidationError):
            section.action_confirm_reconcile()

    def test_other_reason_requires_note(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        line = self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": 10.0,
                "counted_qty": 9.0,
                "unit_cost": self.product_a.standard_price,
                "variance_reason": "other",
            }
        )
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=9)
        self.assertEqual(section.state, "pending_review")
        # 'Other' needs a free-text note before the section can reconcile.
        with self.assertRaises(ValidationError):
            section.action_confirm_reconcile()
        line.variance_note = "explained"
        section.action_confirm_reconcile()
        self.assertEqual(section.state, "reconciled")

    def test_rescan_count_tracks_loops(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        # Loop 1
        section.with_user(self.scanner).action_start_scanning()
        self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": 5.0,
                "counted_qty": 5.0,
                "unit_cost": self.product_a.standard_price,
            }
        )
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=3)
        self.assertEqual(section.rescan_count, 1)
        # Loop 2
        section.with_user(self.scanner).action_start_scanning()
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_submit_physical_count(physical_qty=2)
        self.assertEqual(section.rescan_count, 2)
        # Resolve
        section.with_user(self.scanner).action_start_scanning()
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_submit_physical_count(physical_qty=5)
        self.assertEqual(section.state, "reconciled")
        self.assertEqual(section.rescan_count, 2)
