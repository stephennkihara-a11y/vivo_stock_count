"""Access rights, SoD (counter ≠ approver), reconciliation immutability,
variance band routing.

Covers acceptance criteria: #1 (counter cannot apply), #7 (variance routing),
#10 (scan-event immutability), #18 (reconciliation immutability).
"""
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase1")
class TestAccessRights(VivoCountCommon):

    def _reconciled_session(self, variance_value=0.0):
        """Build a session reconciled with the given total absolute variance."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self._reconcile_section(sections[0], self.scanner, self.physical, 5)
        # Second section carries the optional variance.
        section = sections[1]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        # Variance comes from system_qty 10 -> counted 10 + |variance_value|/unit_cost
        if variance_value:
            delta_units = variance_value / self.product_a.standard_price
        else:
            delta_units = 0.0
        counted = 10.0 - delta_units
        self.Line.create(
            {
                "section_id": section.id,
                "product_id": self.product_a.id,
                "system_qty": 10.0,
                "counted_qty": counted,
                "unit_cost": self.product_a.standard_price,
                "variance_reason": "miscount" if variance_value else False,
            }
        )
        section.with_user(self.scanner).action_finish_scanning()
        section.with_user(self.physical).action_approve_scan()
        section.with_user(self.store_manager).action_confirm_reconcile(review_note="reviewed")
        session.action_submit_for_review()
        return session

    # ------------------------------------------------------------------
    # AC #1
    # ------------------------------------------------------------------
    def test_counter_cannot_apply(self):
        session = self._reconciled_session()
        session.with_user(self.store_manager).action_approve()
        # The scanner from the count cannot approve their own session (SoD),
        # so create a third counter user who never scanned.
        bystander_counter = self.env["res.users"].create(
            {
                "name": "Bystander",
                "login": "vivo.bystander@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        with self.assertRaises((AccessError, UserError)):
            session.with_user(bystander_counter).action_apply()

    # ------------------------------------------------------------------
    # AC #7 — variance routing
    # ------------------------------------------------------------------
    def test_store_band_approval(self):
        session = self._reconciled_session(variance_value=500.0)  # under 5k
        self.assertEqual(session.tolerance_band, "auto")
        session.with_user(self.store_manager).action_approve()
        self.assertEqual(session.state, "approved")

    def test_regional_band_blocks_store_manager(self):
        session = self._reconciled_session(variance_value=10000.0)  # 5k-25k
        self.assertEqual(session.tolerance_band, "regional")
        with self.assertRaises(AccessError):
            session.with_user(self.store_manager).action_approve()
        # Regional can.
        session.with_user(self.regional).action_approve()
        self.assertEqual(session.state, "approved")

    def test_cfoo_band_blocks_regional(self):
        session = self._reconciled_session(variance_value=50000.0)  # >25k
        self.assertEqual(session.tolerance_band, "cfoo")
        with self.assertRaises(AccessError):
            session.with_user(self.regional).action_approve()
        session.with_user(self.cfoo).action_approve()
        self.assertEqual(session.state, "approved")

    def test_counter_who_scanned_cannot_approve(self):
        """SoD: scanner of a section in this session cannot approve it."""
        # Promote the scanner to store manager rank so the band gate passes;
        # the counter-not-approver gate should still block.
        self.scanner.groups_id = [(4, self.group_store_mgr.id)]
        session = self._reconciled_session()
        with self.assertRaises(UserError):
            session.with_user(self.scanner).action_approve()

    # ------------------------------------------------------------------
    # AC #10 + #18 — immutability
    # ------------------------------------------------------------------
    def test_scan_event_is_immutable(self):
        event = self.env["vivo.count.scan.event"].create(
            {
                "counter_id": self.scanner.id,
                "scanned_qty": 1.0,
                "scan_type": "initial",
            }
        )
        with self.assertRaises(AccessError):
            event.scanned_qty = 99
        with self.assertRaises(AccessError):
            event.unlink()

    def test_reconciliation_is_immutable(self):
        recon = self.env["vivo.count.reconciliation"].sudo().create(
            {
                "name": "RECON/TEST/0001",
                "session_id": self._new_session().id,
            }
        )
        with self.assertRaises(AccessError):
            recon.name = "tampered"
        with self.assertRaises(AccessError):
            recon.unlink()

    def test_reconciliation_line_is_immutable(self):
        session = self._new_session()
        recon = self.env["vivo.count.reconciliation"].sudo().create(
            {"name": "RECON/TEST/0002", "session_id": session.id}
        )
        line = self.env["vivo.count.reconciliation.line"].sudo().create(
            {
                "reconciliation_id": recon.id,
                "product_id": self.product_a.id,
                "qty_before": 10,
                "qty_after": 9,
            }
        )
        with self.assertRaises(AccessError):
            line.qty_after = 99
        with self.assertRaises(AccessError):
            line.unlink()
