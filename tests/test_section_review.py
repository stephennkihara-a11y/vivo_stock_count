"""Auditor-confirmed section reconciliation (pending_review flow).

A scan-vs-physical match no longer reconciles a section outright when there
is a genuine variance: the section holds at `pending_review` until an auditor
signs off via the Review & Reconcile wizard. Sections with no genuine
variance auto-reconcile when the `vivo_count.auto_close_zero_variance` toggle
is on (the default).
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestSectionReview(VivoCountCommon):

    def _submit_section(self, sys, cnt, physical, reason=False, note=False):
        """Drive a fresh section to the point just after physical submit."""
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
    # Routing
    # ------------------------------------------------------------------
    def test_match_with_variance_goes_to_pending_review(self):
        """Scan==physical but a counted line differs from system -> review."""
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        self.assertEqual(section.state, "pending_review")
        self.assertFalse(section.reconciled_at)
        self.assertFalse(section.reconciled_by_id)

    def test_mismatch_still_routes_to_variance_rescan(self):
        """Unchanged behaviour: totals disagree -> variance_rescan."""
        section = self._submit_section(sys=5, cnt=5, physical=4)
        self.assertEqual(section.state, "variance_rescan")
        self.assertEqual(section.rescan_count, 1)

    # ------------------------------------------------------------------
    # Wizard confirmation
    # ------------------------------------------------------------------
    def test_wizard_blocks_confirm_without_reason(self):
        section = self._submit_section(sys=10, cnt=8, physical=8)
        self.assertEqual(section.state, "pending_review")
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
        # Adding a note unblocks it.
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
        self.assertTrue(section.is_reconciled)
        self.assertTrue(section.reconciled_at)
        self.assertEqual(section.reconciled_by_id, self.store_manager)

    def test_pending_review_blocks_session_advance(self):
        """A session cannot advance while a section is still pending_review."""
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        session = section.session_id
        # Reconcile the other section so only the pending one is outstanding.
        other = session.section_ids - section
        self._reconcile_section(other, self.scanner, self.physical, 0, 0)
        from odoo.exceptions import UserError

        with self.assertRaises(UserError):
            session.action_submit_for_review()

    # ------------------------------------------------------------------
    # Zero-variance auto-close toggle
    # ------------------------------------------------------------------
    def test_zero_variance_auto_closes_when_toggle_on(self):
        # Default toggle is on.
        section = self._submit_section(sys=7, cnt=7, physical=7)
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.reconciled_at)
        # Auto-close records no auditor.
        self.assertFalse(section.reconciled_by_id)

    def test_zero_variance_goes_to_review_when_toggle_off(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "vivo_count.auto_close_zero_variance", "False"
        )
        section = self._submit_section(sys=7, cnt=7, physical=7)
        self.assertEqual(section.state, "pending_review")
        # No variance -> confirmation needs no reason and reconciles cleanly.
        section.with_user(self.store_manager).action_confirm_reconcile()
        self.assertEqual(section.state, "reconciled")
        self.assertEqual(section.reconciled_by_id, self.store_manager)

    def test_pwa_zero_baseline_scan_auto_closes(self):
        """A PWA-style scan (system_qty 0) is not a genuine section variance,
        so a matched section still auto-reconciles with the toggle on."""
        section = self._submit_section(sys=0, cnt=6, physical=6)
        self.assertEqual(section.state, "reconciled")
