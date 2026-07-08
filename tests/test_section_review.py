"""Approve-then-review section reconciliation.

New flow: the scanner scans and finishes; a DIFFERENT second person approves
the scanned result (or rejects it back to scanning); then a manager/auditor
records a mandatory variance note and reconciles. There is no independent
physical count. The force-reconcile write guards are preserved.
"""
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestSectionReview(VivoCountCommon):

    def _scanned(self, sys, cnt, reason=False):
        """Fresh section scanned by self.scanner, finished, in physical_review."""
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        vals = {
            "section_id": section.id,
            "product_id": self.product_a.id,
            "system_qty": sys,
            "counted_qty": cnt,
            "unit_cost": self.product_a.standard_price,
        }
        if reason:
            vals["variance_reason"] = reason
        self.Line.create(vals)
        section.with_user(self.scanner).action_finish_scanning()
        return section

    def _pending(self, sys=5, cnt=5, reason=False):
        """Section scanned + approved by self.physical, in pending_review."""
        section = self._scanned(sys, cnt, reason=reason)
        section.with_user(self.physical).action_approve_scan()
        return section

    # ------------------------------------------------------------------
    # Scan -> physical_review -> approve/reject
    # ------------------------------------------------------------------
    def test_finish_scanning_goes_to_physical_review(self):
        section = self._scanned(5, 5)
        self.assertEqual(section.state, "physical_review")

    def test_approve_moves_to_pending_review(self):
        section = self._scanned(5, 5)
        section.with_user(self.physical).action_approve_scan()
        self.assertEqual(section.state, "pending_review")
        self.assertEqual(section.physical_counter_id, self.physical)
        # Approval alone does not reconcile.
        self.assertFalse(section.reconciled_at)

    def test_approver_must_differ_from_scanner(self):
        section = self._scanned(5, 5)
        with self.assertRaises(ValidationError):
            section.with_user(self.scanner).action_approve_scan()

    def test_reject_sends_back_to_scanning(self):
        section = self._scanned(5, 5)
        section.with_user(self.physical).action_reject_scan()
        self.assertEqual(section.state, "scanning")
        self.assertEqual(section.rescan_count, 1)
        self.assertFalse(section.physical_counter_id)

    def test_scanner_cannot_reject_own_scan(self):
        section = self._scanned(5, 5)
        with self.assertRaises(ValidationError):
            section.with_user(self.scanner).action_reject_scan()

    # ------------------------------------------------------------------
    # Manager review + mandatory note
    # ------------------------------------------------------------------
    def test_reconcile_without_note_blocked(self):
        section = self._pending(5, 5)
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile()
        self.assertEqual(section.state, "pending_review")

    def test_reconcile_with_note_stamps_reviewer(self):
        section = self._pending(5, 5)
        section.with_user(self.store_manager).action_confirm_reconcile(
            review_note="counted and agreed"
        )
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.is_reconciled)
        self.assertTrue(section.reconciled_at)
        self.assertEqual(section.reconciled_by_id, self.store_manager)
        self.assertTrue(section.review_note)

    def test_non_manager_cannot_reconcile(self):
        section = self._pending(5, 5)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).action_confirm_reconcile(review_note="x")
        self.assertEqual(section.state, "pending_review")

    def test_regional_and_cfoo_can_reconcile(self):
        for reviewer in (self.regional, self.cfoo):
            section = self._pending(5, 5)
            section.with_user(reviewer).action_confirm_reconcile(review_note="ok")
            self.assertEqual(section.state, "reconciled")
            self.assertEqual(section.reconciled_by_id, reviewer)

    def test_counted_variance_line_still_needs_reason(self):
        # Real system baseline that differs -> per-line reason required too.
        section = self._pending(sys=10, cnt=8)
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile(review_note="noted")
        section.line_ids.filtered(lambda l: l.difference != 0).variance_reason = "miscount"
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="noted")
        self.assertEqual(section.state, "reconciled")

    def test_pending_review_blocks_session_advance(self):
        section = self._pending(5, 5)
        session = section.session_id
        other = session.section_ids - section
        self._reconcile_section(other, self.scanner, self.physical, 0, 0)
        with self.assertRaises(UserError):
            session.action_submit_for_review()

    # ------------------------------------------------------------------
    # Review & Reconcile wizard
    # ------------------------------------------------------------------
    def test_wizard_reconciles_with_note(self):
        section = self._pending(5, 5)
        wiz = (
            self.env["vivo.count.section.review.wizard"]
            .with_user(self.store_manager)
            .create({"section_id": section.id, "review_note": "reviewed"})
        )
        wiz.with_user(self.store_manager).action_confirm()
        self.assertEqual(section.state, "reconciled")
        self.assertEqual(section.reconciled_by_id, self.store_manager)

    # ------------------------------------------------------------------
    # Preserved force-reconcile guards (security regression)
    # ------------------------------------------------------------------
    def test_counter_cannot_direct_write_force_reconciled(self):
        section = self._pending(5, 5)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write({"force_reconciled": True})
        self.assertFalse(section.force_reconciled)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write({"force_reconcile_reason": "sneaky"})

    def test_manager_can_direct_write_force_reconciled(self):
        section = self._pending(5, 5)
        section.with_user(self.store_manager).write({"force_reconciled": True})
        self.assertTrue(section.force_reconciled)

    def test_counter_cannot_direct_write_reconciled_state(self):
        """Every reconcile is a manager decision now, so a raw write of the
        reconciled state by a counter is blocked too."""
        section = self._pending(5, 5)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write(
                {"state": "reconciled", "review_note": "sneaky"}
            )
        self.assertEqual(section.state, "pending_review")
