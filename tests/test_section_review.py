"""Auditor-confirmed section reconciliation (pending_review flow).

Submitting the physical count always routes a section to `pending_review`
(match or mismatch) — there is no Variance Re-scan loop and no auto-close. The
auditor reviews any variance and reconciles via the Review & Reconcile wizard;
reconciliation is manager/auditor-gated. Where the scan total and the auditor's
authoritative figure still differ, the force-reconcile path (and its write
guards) applies.
"""
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestSectionReview(VivoCountCommon):

    def _submit_section(self, sys, cnt, physical, reason=False, note=False):
        """Scan one line, finish, submit physical -> pending_review."""
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
        if note:
            vals["variance_note"] = note
        self.Line.create(vals)
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(
            physical_qty=physical
        )
        return section

    # ------------------------------------------------------------------
    # Routing: submit always -> pending_review
    # ------------------------------------------------------------------
    def test_match_goes_to_pending_review(self):
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        self.assertEqual(section.state, "pending_review")
        self.assertFalse(section.reconciled_at)

    def test_mismatch_goes_to_pending_review(self):
        """A scan-vs-physical mismatch goes straight to review, no loop."""
        section = self._submit_section(sys=5, cnt=5, physical=4)
        self.assertEqual(section.state, "pending_review")
        self.assertEqual(section.rescan_count, 0)

    def test_zero_variance_still_goes_to_pending_review(self):
        """No auto-close: even a clean, zero-variance section is reviewed."""
        section = self._submit_section(sys=7, cnt=7, physical=7)
        self.assertEqual(section.state, "pending_review")
        section.with_user(self.store_manager).action_confirm_reconcile()
        self.assertEqual(section.state, "reconciled")
        self.assertEqual(section.reconciled_by_id, self.store_manager)

    # ------------------------------------------------------------------
    # Wizard confirmation + per-line reasons
    # ------------------------------------------------------------------
    def test_wizard_blocks_confirm_without_reason(self):
        section = self._submit_section(sys=10, cnt=8, physical=8)
        wiz = self.env["vivo.count.section.review.wizard"].create(
            {"section_id": section.id}
        )
        with self.assertRaises(ValidationError):
            wiz.action_confirm()
        self.assertEqual(section.state, "pending_review")

    def test_wizard_blocks_when_other_reason_missing_note(self):
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="other")
        wiz = self.env["vivo.count.section.review.wizard"].create(
            {"section_id": section.id}
        )
        with self.assertRaises(ValidationError):
            wiz.action_confirm()
        section.line_ids.filtered(lambda l: l.difference != 0).variance_note = "found in transit"
        wiz.action_confirm()
        self.assertEqual(section.state, "reconciled")

    def test_confirm_reconciles_and_stamps_auditor(self):
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        wiz = (
            self.env["vivo.count.section.review.wizard"]
            .with_user(self.store_manager)
            .create({"section_id": section.id})
        )
        wiz.with_user(self.store_manager).action_confirm()
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.reconciled_at)
        self.assertEqual(section.reconciled_by_id, self.store_manager)

    def test_pending_review_blocks_session_advance(self):
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        session = section.session_id
        other = session.section_ids - section
        self._reconcile_section(other, self.scanner, self.physical, 0, 0)
        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            session.action_submit_for_review()

    def test_plain_counter_cannot_confirm(self):
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).action_confirm_reconcile()

    # ------------------------------------------------------------------
    # Mismatch at review: auditor force-reconcile (+ preserved guards)
    # ------------------------------------------------------------------
    def _pending_mismatch(self, scan=5, physical=3):
        """Section in pending_review with scan_total (scan) != physical (physical)."""
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": scan,
                "counted_qty": scan,
                "unit_cost": self.product_a.standard_price,
            }
        )
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(
            physical_qty=physical
        )
        self.assertEqual(section.state, "pending_review")
        return section

    def test_auditor_forced_reconcile_sets_qty_reason_and_stamps(self):
        section = self._pending_mismatch(scan=5, physical=3)
        section.with_user(self.store_manager).action_confirm_reconcile(
            physical_qty=4.0, force_reason="Recount by auditor; two units mis-scanned."
        )
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.force_reconciled)
        self.assertEqual(section.physical_total_qty, 4.0)
        self.assertEqual(section.reconciled_by_id, self.store_manager)
        self.assertTrue(section.force_reconcile_reason)

    def test_auditor_force_reconcile_without_reason_blocked(self):
        section = self._pending_mismatch(scan=5, physical=3)
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile(
                physical_qty=4.0
            )
        self.assertEqual(section.state, "pending_review")

    def test_plain_counter_cannot_force_reconcile(self):
        section = self._pending_mismatch(scan=5, physical=3)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).action_confirm_reconcile(
                physical_qty=4.0, force_reason="not allowed"
            )
        self.assertEqual(section.state, "pending_review")

    def test_counter_cannot_direct_write_force_reconciled(self):
        section = self._pending_mismatch(scan=5, physical=3)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write({"force_reconciled": True})
        self.assertFalse(section.force_reconciled)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write({"force_reconcile_reason": "sneaky"})

    def test_manager_can_direct_write_force_reconciled(self):
        section = self._pending_mismatch(scan=5, physical=3)
        section.with_user(self.store_manager).write({"force_reconciled": True})
        self.assertTrue(section.force_reconciled)

    def test_forced_reconcile_via_wizard(self):
        section = self._pending_mismatch(scan=5, physical=3)
        wiz = (
            self.env["vivo.count.section.review.wizard"]
            .with_user(self.store_manager)
            .create({"section_id": section.id})
        )
        self.assertTrue(wiz.is_mismatch)
        wiz.authoritative_qty = 5.0
        wiz.force_reason = "Auditor accepts the scanned count."
        wiz.with_user(self.store_manager).action_confirm()
        self.assertEqual(section.state, "reconciled")
        self.assertEqual(section.reconciled_by_id, self.store_manager)
