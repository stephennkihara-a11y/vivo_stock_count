"""Phase 1 — session + section state machine tests.

Covers acceptance criteria: #2, #4, #5 (partial — re-scan loop).
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase1")
class TestSessionStateMachine(VivoCountCommon):

    def test_draft_to_in_progress_creates_sections_from_templates(self):
        session = self._new_session()
        session.action_start()
        self.assertEqual(session.state, "in_progress")
        self.assertEqual(len(session.section_ids), 2)
        self.assertTrue(session.start_date)

    def test_start_fails_without_templates(self):
        empty_zone = self.Zone.create(
            {"name": "Empty Zone", "location_id": self.location.id}
        )
        session = self.Session.create(
            {"location_id": self.location.id, "zone_id": empty_zone.id}
        )
        with self.assertRaises(UserError):
            session.action_start()

    def test_session_whole_store_spans_multiple_zones(self):
        """A1: zone_id empty -> session pulls templates from every zone in the store."""
        session = self.Session.create({"location_id": self.location.id})
        session.action_start()
        zones = session.section_ids.mapped("zone_id")
        self.assertIn(self.zone_floor, zones)
        self.assertIn(self.zone_back, zones)

    def test_cannot_advance_to_review_with_unreconciled_sections(self):
        """AC #4: submit blocked while any section unreconciled."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        # Reconcile only the first section.
        self._reconcile_section(sections[0], self.scanner, self.physical, 10)
        with self.assertRaises(UserError):
            session.action_submit_for_review()

    def test_advance_to_review_when_all_sections_reconciled(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        for s in sections:
            self._reconcile_section(s, self.scanner, self.physical, 5)
        session.action_submit_for_review()
        self.assertEqual(session.state, "review")

    def test_constraint_blocks_review_state_with_unreconciled_section(self):
        """Direct state write should be blocked by @api.constrains."""
        session = self._new_session()
        self._start_and_get_sections(session)
        with self.assertRaises(ValidationError):
            session.state = "review"

    def test_bounce_section_resets_only_that_section(self):
        """AC #5: bounce from review touches only chosen section(s)."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        for s in sections:
            self._reconcile_section(s, self.scanner, self.physical, 5)
        session.action_submit_for_review()

        target = sections[0]
        other = sections[1]
        other_lines_before = other.line_ids.mapped("counted_qty")
        before_rescan = target.rescan_count

        session.action_bounce_sections([target.id])

        self.assertEqual(session.state, "in_progress")
        self.assertEqual(target.state, "scanning")
        self.assertEqual(target.rescan_count, before_rescan + 1)
        self.assertEqual(target.line_ids.mapped("counted_qty"), [0.0])
        # Other section untouched.
        self.assertEqual(other.state, "reconciled")
        self.assertEqual(other.line_ids.mapped("counted_qty"), other_lines_before)

    def test_apply_requires_approval(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        for s in sections:
            self._reconcile_section(s, self.scanner, self.physical, 5)
        session.action_submit_for_review()
        # Cannot apply directly from review.
        with self.assertRaises(UserError):
            session.action_apply()

    def test_cancel_then_reset_to_draft(self):
        session = self._new_session()
        self._start_and_get_sections(session)
        session.action_cancel()
        self.assertEqual(session.state, "cancelled")
        session.action_reset_to_draft()
        self.assertEqual(session.state, "draft")
        self.assertFalse(session.section_ids)
