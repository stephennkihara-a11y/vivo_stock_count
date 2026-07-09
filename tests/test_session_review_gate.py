"""Item 2 — session-level gate on the auditor's Review & Reconcile action.

The physical counter only ever *submits* a section for review. The auditor's
full Review & Reconcile action becomes available only once EVERY section in the
session has been submitted (none left in draft / scanning / physical_count).
The gate is surfaced by ``session.all_submitted_for_review`` and enforced at the
wizard entry point ``section.action_open_section_review_wizard`` (mirrored by the
form button's ``invisible`` condition).
"""
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestSessionReviewGate(VivoCountCommon):

    def test_all_submitted_flag_reflects_section_progress(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self.assertEqual(len(sections), 2)
        self.assertFalse(session.all_submitted_for_review)
        self.assertEqual(session.sections_submitted, 0)

        self._submit_section_for_review(sections[0], self.scanner, self.physical, 5)
        session.invalidate_recordset()
        self.assertEqual(session.sections_submitted, 1)
        self.assertFalse(session.all_submitted_for_review)

        self._submit_section_for_review(sections[1], self.scanner, self.physical, 5)
        session.invalidate_recordset()
        self.assertEqual(session.sections_submitted, 2)
        self.assertTrue(session.all_submitted_for_review)

    def test_wizard_open_blocked_until_whole_session_submitted(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self._submit_section_for_review(sections[0], self.scanner, self.physical, 5)
        self.assertEqual(sections[0].state, "pending_review")

        # A sibling still in draft -> the auditor cannot open Review & Reconcile.
        with self.assertRaises(UserError):
            sections[0].with_user(
                self.store_manager
            ).action_open_section_review_wizard()

        # Once every section is submitted, the wizard opens.
        self._submit_section_for_review(sections[1], self.scanner, self.physical, 5)
        action = sections[0].with_user(
            self.store_manager
        ).action_open_section_review_wizard()
        self.assertEqual(action["res_model"], "vivo.count.section.review.wizard")
        self.assertEqual(action["context"]["default_section_id"], sections[0].id)

    def test_reconciled_sibling_counts_toward_gate(self):
        """A section already reconciled still satisfies the gate for its peers."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self._submit_section_for_review(sections[0], self.scanner, self.physical, 5)
        self._submit_section_for_review(sections[1], self.scanner, self.physical, 5)
        # Reconcile one; the flag must stay True (reconciled still counts).
        sections[0].action_confirm_reconcile()
        session.invalidate_recordset()
        self.assertTrue(session.all_submitted_for_review)
        action = sections[1].with_user(
            self.store_manager
        ).action_open_section_review_wizard()
        self.assertEqual(action["res_model"], "vivo.count.section.review.wizard")

    def test_empty_session_is_not_submitted(self):
        """A session with no started sections is not 'all submitted'."""
        session = self._new_session()
        self.assertFalse(session.all_submitted_for_review)
        self.assertEqual(session.sections_submitted, 0)

    def test_related_flag_visible_on_section(self):
        """The section exposes the session flag for its form button."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self.assertFalse(sections[0].session_all_submitted)
        for s in sections:
            self._submit_section_for_review(s, self.scanner, self.physical, 5)
        sections.invalidate_recordset()
        self.assertTrue(sections[0].session_all_submitted)
