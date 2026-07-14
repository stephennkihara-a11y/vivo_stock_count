"""Rack recount gate on Finish Scanning (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestRackRecountGate --stop-after-init

When a rack is finished and the manual Physical Count does not equal the scanned
total, the counter must choose: PROCEED (accept the discrepancy and advance the
rack) or REJECT & RECOUNT (wipe every scanned line and rescan). Reject is
destructive and only permitted before the rack is reconciled.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "recount_gate")
class TestRackRecountGate(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.Section = cls.env["vivo.count.section"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Recount Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Recount Zone", "location_id": cls.location.id}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Recount SKU", "type": "consu"}
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
        )

    def _fresh_section(self, name="Rack RC"):
        return self.Section.create(
            {
                "session_id": self.session.id,
                "name": name,
                "zone_id": self.zone.id,
                "state": "scanning",
            }
        )

    def _scan_known(self, section, key, qty=1):
        return self.Line.record_scan(
            section_id=section.id,
            product_id=self.product.id,
            scanned_qty=qty,
            idempotency_key=key,
        )

    def _scan_unknown(self, section, barcode, key, qty=1):
        return self.Line.record_scan(
            section_id=section.id,
            product_id=None,
            scanned_qty=qty,
            idempotency_key=key,
            scanned_barcode=barcode,
        )

    # (a) match -> finish proceeds, no alert path -----------------------------
    def test_match_finishes_without_alert(self):
        sec = self._fresh_section()
        self._scan_known(sec, "rc-a", qty=3)
        sec.physical_total_qty = 3.0  # equals the scanned total
        result = sec.action_finish_scanning_gate()
        self.assertTrue(result is True, "match advances directly, no wizard action")
        self.assertEqual(sec.state, "pending_review")

    # (b) mismatch -> gate shows the wizard; Proceed advances, lines intact ----
    def test_mismatch_opens_gate_then_proceed_advances(self):
        sec = self._fresh_section()
        self._scan_known(sec, "rc-b", qty=3)
        sec.physical_total_qty = 5.0  # disagrees with scanned total (3)

        action = sec.action_finish_scanning_gate()
        self.assertIsInstance(action, dict, "mismatch returns a wizard action")
        self.assertEqual(action.get("res_model"), "vivo.count.recount.gate.wizard")
        self.assertEqual(sec.state, "scanning", "the rack is NOT advanced yet")
        self.assertEqual(len(sec.line_ids), 1, "lines are untouched by the gate")

        # Proceed = accept the discrepancy and finish as normal.
        sec.action_finish_scanning()
        self.assertEqual(sec.state, "pending_review")
        self.assertEqual(len(sec.line_ids), 1, "Proceed keeps the scanned lines")

    def test_mismatch_pwa_gate_payload_and_force(self):
        sec = self._fresh_section()
        self._scan_known(sec, "rc-bp", qty=2)
        sec.physical_total_qty = 4.0

        payload = sec.finish_scanning_pwa()
        self.assertTrue(payload.get("mismatch"))
        self.assertEqual(payload.get("scan_total_qty"), 2.0)
        self.assertEqual(payload.get("physical_total_qty"), 4.0)
        self.assertEqual(payload.get("line_count"), 1)
        self.assertEqual(sec.state, "scanning", "no advance without force")

        forced = sec.finish_scanning_pwa(force=True)
        self.assertFalse(forced.get("mismatch"))
        self.assertEqual(sec.state, "pending_review")

    # (c) mismatch -> Reject clears ALL lines (incl unknown), back to scanning -
    def test_reject_clears_all_lines_including_unknown(self):
        sec = self._fresh_section()
        self._scan_known(sec, "rc-c1", qty=3)
        self._scan_unknown(sec, "BOGUS-RC", "rc-c2", qty=2)
        sec.physical_total_qty = 10.0  # mismatch
        self.assertEqual(len(sec.line_ids), 2, "one known + one unknown line")

        cleared = sec.action_reject_and_recount()
        self.assertEqual(cleared, 2)
        self.assertEqual(len(sec.line_ids), 0, "every line wiped, incl. the unknown")
        self.assertEqual(sec.state, "scanning", "rack is back to scanning")
        self.assertEqual(sec.scan_total_qty, 0.0)
        self.assertEqual(sec.physical_total_qty, 0.0, "stale physical count cleared")
        self.assertTrue(sec.recount_log, "the destructive wipe is audited")
        self.assertIn("2 line(s) cleared", sec.recount_log)

    def test_reject_allowed_from_pending_review(self):
        sec = self._fresh_section()
        self._scan_known(sec, "rc-pr", qty=1)
        sec.physical_total_qty = 1.0
        sec.action_finish_scanning()  # -> pending_review (the just-finished state)
        self.assertEqual(sec.state, "pending_review")

        sec.action_reject_and_recount()
        self.assertEqual(sec.state, "scanning")
        self.assertEqual(len(sec.line_ids), 0)

    # (d) Reject on a reconciled section is BLOCKED --------------------------
    def test_reject_blocked_after_reconciled(self):
        sec = self._fresh_section()
        self._scan_known(sec, "rc-d", qty=1)
        # Move to reconciled (review_note satisfies the reconcile-note gate).
        sec.write({"review_note": "counted and closed", "state": "reconciled"})
        self.assertEqual(sec.state, "reconciled")

        with self.assertRaises(UserError):
            sec.action_reject_and_recount()
        self.assertEqual(sec.state, "reconciled", "state unchanged after the block")
