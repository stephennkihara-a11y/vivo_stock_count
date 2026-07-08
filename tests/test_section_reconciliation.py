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

    def test_reject_sends_back_to_scanning(self):
        """New flow: the second person rejects the scan -> back to scanning,
        rescan_count incremented; then a clean approve+review reconciles."""
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
        self.assertEqual(section.state, "physical_review")
        section.with_user(self.physical).action_reject_scan()
        self.assertEqual(section.state, "scanning")
        self.assertEqual(section.rescan_count, 1)
        self.assertFalse(section.physical_counter_id)
        # Re-scan, approve, review, reconcile.
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_approve_scan()
        section.with_user(self.store_manager).action_confirm_reconcile(
            review_note="recount clean"
        )
        self.assertEqual(section.state, "reconciled")

    def test_segregation_of_duties_on_section(self):
        """AC #3: scanner and physical counter cannot be the same user."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        with self.assertRaises(ValidationError):
            section.physical_counter_id = self.scanner.id

    def test_constraint_blocks_reconciled_without_review_note(self):
        """A direct write to reconciled without a reviewer's note fails — the
        review-note constraint replaces the old scan==physical match."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
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
        """AC #11: a counted line with a variance still needs a per-line reason
        before the reviewer can reconcile (in addition to the review note)."""
        session = self._new_session()
        section = self._approve_section_with_variance(session, counted=9.0, system=10.0)
        self.assertEqual(section.state, "pending_review")
        # Even with a review note, the varied counted line needs a reason.
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile(
                review_note="noted"
            )
        section.line_ids.filtered(lambda l: l.difference != 0).variance_reason = "miscount"
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="noted")
        self.assertEqual(section.state, "reconciled")

    def test_review_note_is_mandatory(self):
        """The reviewer's variance note is mandatory to reconcile."""
        session = self._new_session()
        section = self._reconcile_target(session)
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile()
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="ok")
        self.assertEqual(section.state, "reconciled")

    def test_other_reason_requires_note(self):
        session = self._new_session()
        section = self._approve_section_with_variance(
            session, counted=9.0, system=10.0, reason="other"
        )
        self.assertEqual(section.state, "pending_review")
        # 'Other' needs a per-line free-text note before reconciling.
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile(
                review_note="noted"
            )
        section.line_ids.filtered(lambda l: l.difference != 0).variance_note = "explained"
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="noted")
        self.assertEqual(section.state, "reconciled")

    def test_reject_loop_tracks_rescan_count(self):
        """Repeated rejects increment rescan_count; a final approve reconciles."""
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
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
        # Reject twice.
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_reject_scan()
        self.assertEqual(section.rescan_count, 1)
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_reject_scan()
        self.assertEqual(section.rescan_count, 2)
        # Resolve: approve + review.
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_approve_scan()
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="ok")
        self.assertEqual(section.state, "reconciled")
        self.assertEqual(section.rescan_count, 2)

    # ------------------------------------------------------------------
    # Local helpers for the approve-then-review flow
    # ------------------------------------------------------------------
    def _reconcile_target(self, session):
        """A section scanned (no per-line variance) and approved, sitting in
        pending_review ready for the reviewer."""
        section = self._start_and_get_sections(session)[0]
        return self._approve_section(section, self.scanner, self.physical, 5)

    def _approve_section_with_variance(self, session, counted, system, reason=False):
        section = self._start_and_get_sections(session)[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        vals = {
            "section_id": section.id,
            "product_id": self.product_a.id,
            "system_qty": system,
            "counted_qty": counted,
            "unit_cost": self.product_a.standard_price,
        }
        if reason:
            vals["variance_reason"] = reason
        self.Line.create(vals)
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_approve_scan()
        return section
