"""Auditor-confirmed section reconciliation (pending_review flow).

A scan-vs-physical match no longer reconciles a section outright when there
is a genuine variance: the section holds at `pending_review` until an auditor
signs off via the Review & Reconcile wizard. Sections with no genuine
variance auto-reconcile when the `vivo_count.auto_close_zero_variance` toggle
is on (the default).
"""
from odoo.exceptions import AccessError, UserError, ValidationError
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

    # ------------------------------------------------------------------
    # Persistent scan-vs-physical mismatch -> auditor escalation
    # ------------------------------------------------------------------
    def _escalate_via_mismatch(self, scan=5, physical=3):
        """Drive a section to pending_review through a persistent mismatch.

        With the default threshold (1) the first mismatch loops to
        variance_rescan; the second escalates to pending_review. The seeded
        line has system_qty == counted_qty, so there is no per-line variance —
        only the section-total disagreement drives the escalation.
        """
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
        # First mismatch -> loop.
        self.assertEqual(section.state, "variance_rescan")
        # Re-scan, still disagree -> escalate.
        section.with_user(self.scanner).action_start_scanning()
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_submit_physical_count(
            physical_qty=physical
        )
        return section

    def test_mismatch_under_threshold_loops(self):
        """First mismatch (rescan_count == threshold) stays in variance_rescan."""
        section = self._submit_section(sys=5, cnt=5, physical=3)
        self.assertEqual(section.state, "variance_rescan")
        self.assertEqual(section.rescan_count, 1)

    def test_mismatch_over_threshold_escalates_to_pending_review(self):
        """A persistent mismatch escalates to the auditor, never auto-reconciles."""
        section = self._escalate_via_mismatch(scan=5, physical=3)
        self.assertEqual(section.state, "pending_review")
        self.assertEqual(section.rescan_count, 2)
        self.assertFalse(section.reconciled_at)

    def test_auditor_forced_reconcile_sets_qty_reason_and_stamps(self):
        section = self._escalate_via_mismatch(scan=5, physical=3)
        # Auditor's authoritative headcount (4) differs from the scan (5).
        section.with_user(self.store_manager).action_confirm_reconcile(
            physical_qty=4.0, force_reason="Recount by auditor; two units mis-scanned."
        )
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.force_reconciled)
        self.assertEqual(section.physical_total_qty, 4.0)
        self.assertEqual(section.reconciled_by_id, self.store_manager)
        self.assertTrue(section.force_reconcile_reason)
        self.assertTrue(section.reconciled_at)

    def test_auditor_force_reconcile_without_reason_blocked(self):
        section = self._escalate_via_mismatch(scan=5, physical=3)
        with self.assertRaises(ValidationError):
            section.with_user(self.store_manager).action_confirm_reconcile(
                physical_qty=4.0
            )
        self.assertEqual(section.state, "pending_review")

    def test_plain_counter_cannot_force_reconcile(self):
        section = self._escalate_via_mismatch(scan=5, physical=3)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).action_confirm_reconcile(
                physical_qty=4.0, force_reason="not allowed"
            )
        self.assertEqual(section.state, "pending_review")

    def test_plain_counter_cannot_confirm_variance_signoff(self):
        # Even the scan==physical sign-off path is manager-gated.
        section = self._submit_section(sys=10, cnt=8, physical=8, reason="miscount")
        self.assertEqual(section.state, "pending_review")
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).action_confirm_reconcile()

    def test_counter_cannot_direct_write_force_reconciled(self):
        """Defense in depth: bypassing the action via a raw ORM write of
        force_reconciled is blocked for a non-manager, even though the counter
        record rule grants section write access during an in-progress count."""
        section = self._escalate_via_mismatch(scan=5, physical=3)
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write({"force_reconciled": True})
        self.assertFalse(section.force_reconciled)
        self.assertEqual(section.state, "pending_review")
        # The audit-reason field is guarded too.
        with self.assertRaises(AccessError):
            section.with_user(self.scanner).write(
                {"force_reconcile_reason": "sneaky"}
            )

    def test_manager_can_direct_write_force_reconciled(self):
        """The guard must not block a legitimate manager/auditor write."""
        section = self._escalate_via_mismatch(scan=5, physical=3)
        section.with_user(self.store_manager).write({"force_reconciled": True})
        self.assertTrue(section.force_reconciled)

    def test_forced_reconcile_via_wizard(self):
        section = self._escalate_via_mismatch(scan=5, physical=3)
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
