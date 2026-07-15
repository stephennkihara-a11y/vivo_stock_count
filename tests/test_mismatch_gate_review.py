"""Mismatch gate fires at the supervisor's physical-count submit (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestMismatchGateReview --stop-after-init

The gate used to be wired only to Finish Scanning (scanning state), where no
physical count exists yet, so it never fired. The real firing point is the
supervisor entering physical_total_qty on a pending_review rack and submitting.
``action_submit_physical_review`` runs the SAME shared helper and opens the
EXISTING recount gate wizard on a genuine mismatch.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "mismatch_gate_review")
class TestMismatchGateReview(TransactionCase):

    WIZARD = "vivo.count.recount.gate.wizard"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.Section = cls.env["vivo.count.section"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Review Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Review Zone", "location_id": cls.location.id}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Review SKU", "type": "consu"}
        )
        cls.session = cls.env["vivo.count.session"].create(
            {"location_id": cls.location.id}
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

    def _pending_section(self, name="Rack REV"):
        sec = self.Section.create(
            {
                "session_id": self.session.id,
                "name": name,
                "zone_id": self.zone.id,
                "state": "scanning",
            }
        )
        return sec

    # (a) entered physical != scanned total -> gate fires (opens the wizard) ---
    def test_review_mismatch_opens_recount_wizard(self):
        sec = self._pending_section()
        self._scan_known(sec, "rev-a", qty=3)  # scanned total = 3
        sec.action_finish_scanning()
        self.assertEqual(sec.state, "pending_review")

        sec.physical_total_qty = 5.0  # supervisor enters a differing count
        action = sec.action_submit_physical_review()
        self.assertIsInstance(action, dict)
        self.assertEqual(action.get("res_model"), self.WIZARD, "the recount gate opens")
        self.assertEqual(sec.state, "pending_review", "no state change on opening the gate")

    # (b) entered physical == scanned total -> no gate, advances normally ------
    def test_review_match_no_gate(self):
        sec = self._pending_section()
        self._scan_known(sec, "rev-b", qty=3)
        sec.action_finish_scanning()

        sec.physical_total_qty = 3.0  # matches
        action = sec.action_submit_physical_review()
        self.assertNotEqual(
            action.get("res_model"), self.WIZARD, "a match must NOT open the gate"
        )
        self.assertEqual(sec.state, "pending_review", "stays ready for store reconcile")

    # (c) blank/0 physical count -> no gate, advances normally ----------------
    def test_review_blank_physical_no_gate(self):
        sec = self._pending_section()
        self._scan_known(sec, "rev-c", qty=3)
        sec.action_finish_scanning()

        self.assertEqual(sec.physical_total_qty, 0.0)  # nothing entered
        action = sec.action_submit_physical_review()
        self.assertNotEqual(action.get("res_model"), self.WIZARD)
        self.assertEqual(sec.state, "pending_review")

    # (d) Reject from this path wipes lines (incl. unknown) -> scanning; and a
    #     reconciled section stays blocked --------------------------------------
    def test_review_reject_wipes_and_resets_and_reconciled_blocked(self):
        sec = self._pending_section()
        self._scan_known(sec, "rev-d1", qty=3)
        self._scan_unknown(sec, "BOGUS-REV", "rev-d2", qty=2)
        sec.action_finish_scanning()
        sec.physical_total_qty = 99.0  # mismatch

        action = sec.action_submit_physical_review()
        self.assertEqual(action.get("res_model"), self.WIZARD)

        # Reject via the wizard opened by the review submit.
        wiz = self.env[self.WIZARD].create({"section_id": sec.id})
        wiz.action_reject()
        self.assertEqual(sec.state, "scanning", "rejected rack goes back to scanning")
        self.assertEqual(len(sec.line_ids), 0, "all lines wiped, incl. the unknown")
        self.assertTrue(sec.recount_log, "the destructive wipe is audited")

        # A reconciled section is still blocked from reject.
        other = self._pending_section(name="Rack REV2")
        self._scan_known(other, "rev-d3", qty=1)
        other.action_finish_scanning()
        other.write({"review_note": "closed", "state": "reconciled"})
        with self.assertRaises(UserError):
            other.action_reject_and_recount()

    # Proceed from the review path leaves the rack in pending_review (no crash).
    def test_review_proceed_leaves_pending_review(self):
        sec = self._pending_section()
        self._scan_known(sec, "rev-e", qty=3)
        sec.action_finish_scanning()
        sec.physical_total_qty = 7.0  # mismatch -> wizard would open

        wiz = self.env[self.WIZARD].create({"section_id": sec.id})
        wiz.action_proceed()
        self.assertEqual(
            sec.state, "pending_review", "Proceed accepts the discrepancy in place"
        )
        self.assertEqual(len(sec.line_ids), 1, "Proceed keeps the scanned lines")
