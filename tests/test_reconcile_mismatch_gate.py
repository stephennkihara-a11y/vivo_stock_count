"""Store-level reconcile mismatch gate (SELF-CONTAINED).

Does NOT use tests/common.py (its helpers call methods removed on main). Run:

    docker compose run --rm odoo odoo -d vivo_test -u vivo_stock_count \
      --test-enable --test-tags /vivo_stock_count:TestReconcileMismatchGate --stop-after-init

A SECOND gate at the store reconcile: before reconciling, every rack about to be
reconciled (pending_review) is scanned. Physically-counted racks whose count !=
scan are listed as tickable mismatches; racks with no physical count are a softer
"unverified" advisory. Reject Selected wipes ONLY the ticked racks (reusing the
existing per-section wipe); Proceed continues the reconcile as today. A clean
session never opens the gate.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged("post_install", "-at_install", "vivo_count", "reconcile_mismatch_gate")
class TestReconcileMismatchGate(TransactionCase):

    GATE = "vivo.count.session.reconcile.gate.wizard"
    NOTE = "vivo.count.session.reconcile.wizard"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Line = cls.env["vivo.count.line"]
        cls.Section = cls.env["vivo.count.section"]
        cls.location = cls.env["stock.location"].create(
            {
                "name": "Recon Gate Store",
                "usage": "internal",
                "location_id": cls.env.ref("stock.stock_location_stock").id,
            }
        )
        cls.zone = cls.env["vivo.count.zone"].create(
            {"name": "Recon Gate Zone", "location_id": cls.location.id}
        )
        cls.product = cls.env["product.product"].create(
            {"name": "Recon Gate SKU", "type": "consu"}
        )

    def _rack(self, session, name, scan_qty, physical, with_unknown=False, key=""):
        """A rack scanned, finished (pending_review), with a physical count set."""
        sec = self.Section.create(
            {
                "session_id": session.id,
                "name": name,
                "zone_id": self.zone.id,
                "state": "scanning",
            }
        )
        self.Line.record_scan(
            section_id=sec.id,
            product_id=self.product.id,
            scanned_qty=scan_qty,
            idempotency_key="k-%s" % (key or name),
        )
        if with_unknown:
            self.Line.record_scan(
                section_id=sec.id,
                product_id=None,
                scanned_qty=1,
                idempotency_key="u-%s" % (key or name),
                scanned_barcode="BOGUS-%s" % name,
            )
        sec.action_finish_scanning()
        sec.physical_total_qty = physical
        return sec

    def _gate(self, session):
        return self.env[self.GATE].with_context(
            default_session_id=session.id
        ).create({})

    # (a) all racks match -> reconcile proceeds, NO gate ----------------------
    def test_all_match_no_gate(self):
        session = new_session(self)
        self._rack(session, "A1", scan_qty=3, physical=3)
        self._rack(session, "A2", scan_qty=2, physical=2)
        action = session.action_open_reconcile_wizard()
        self.assertEqual(
            action.get("res_model"), self.NOTE, "clean session goes straight to reconcile"
        )

    # (b) one mismatch rack -> gate opens listing it with the right numbers ----
    def test_one_mismatch_opens_gate(self):
        session = new_session(self)
        self._rack(session, "B1", scan_qty=3, physical=5)  # mismatch (+2)
        self._rack(session, "B2", scan_qty=4, physical=4)  # match
        action = session.action_open_reconcile_wizard()
        self.assertEqual(action.get("res_model"), self.GATE, "mismatch opens the gate")

        wiz = self._gate(session)
        self.assertEqual(len(wiz.mismatch_line_ids), 1, "only the mismatch rack is listed")
        line = wiz.mismatch_line_ids
        self.assertEqual(line.section_name, "B1")
        self.assertEqual(line.physical_qty, 5.0)
        self.assertEqual(line.scanned_qty, 3.0)
        self.assertEqual(line.variance, 2.0)
        self.assertEqual(wiz.unverified_count, 0)

    # (c) blank-physical racks -> listed as UNVERIFIED, not mismatches --------
    def test_blank_physical_listed_unverified(self):
        session = new_session(self)
        self._rack(session, "C1", scan_qty=3, physical=0)  # never verified
        self._rack(session, "C2", scan_qty=2, physical=0)
        action = session.action_open_reconcile_wizard()
        self.assertEqual(action.get("res_model"), self.GATE)

        wiz = self._gate(session)
        self.assertFalse(wiz.mismatch_line_ids, "blank physical is NOT a mismatch")
        self.assertEqual(wiz.unverified_count, 2)
        self.assertIn("C1", wiz.unverified_names)
        self.assertIn("C2", wiz.unverified_names)

    # (d) Reject SELECTED wipes only the ticked rack; others untouched --------
    def test_reject_selected_only_ticked(self):
        session = new_session(self)
        r1 = self._rack(session, "D1", scan_qty=3, physical=5, with_unknown=True)
        r2 = self._rack(session, "D2", scan_qty=4, physical=9)  # also a mismatch
        self.assertEqual(len(r1.line_ids), 2)  # known + unknown

        wiz = self._gate(session)
        self.assertEqual(len(wiz.mismatch_line_ids), 2)
        # Tick ONLY D1.
        for line in wiz.mismatch_line_ids:
            line.reject = line.section_id == r1
        wiz.action_reject_selected()

        self.assertEqual(r1.state, "scanning", "ticked rack sent back to scanning")
        self.assertEqual(len(r1.line_ids), 0, "ticked rack wiped, incl. unknown line")
        self.assertTrue(r1.recount_log, "the wipe is audited")
        self.assertEqual(r2.state, "pending_review", "unticked rack untouched")
        self.assertEqual(len(r2.line_ids), 1, "unticked rack keeps its lines")

    # (e) a reconciled rack cannot be rejected via this path (raises) ---------
    def test_reconciled_rack_reject_blocked(self):
        session = new_session(self)
        done = self._rack(session, "E1", scan_qty=3, physical=5)
        done.write({"review_note": "closed", "state": "reconciled"})

        wiz = self.env[self.GATE].with_context(
            default_session_id=session.id
        ).create({})
        # Force a ticked line targeting the reconciled rack (it would not appear
        # normally) and confirm the guard still blocks the wipe.
        wiz.mismatch_line_ids = [
            (
                0,
                0,
                {
                    "section_id": done.id,
                    "physical_qty": 5.0,
                    "scanned_qty": 3.0,
                    "line_count": 1,
                    "reject": True,
                },
            )
        ]
        with self.assertRaises(UserError):
            wiz.action_reject_selected()

    # (f) Proceed continues the reconcile as today (opens the note wizard) ----
    def test_proceed_continues_reconcile(self):
        session = new_session(self)
        r1 = self._rack(session, "F1", scan_qty=3, physical=5)  # mismatch
        wiz = self._gate(session)
        action = wiz.action_proceed()
        self.assertEqual(
            action.get("res_model"), self.NOTE, "Proceed goes to the reconcile note wizard"
        )
        # Proceed does NOT wipe anything.
        self.assertEqual(r1.state, "pending_review")
        self.assertEqual(len(r1.line_ids), 1)

    def test_reject_nothing_ticked_raises(self):
        session = new_session(self)
        self._rack(session, "G1", scan_qty=3, physical=5)
        wiz = self._gate(session)
        with self.assertRaises(UserError):
            wiz.action_reject_selected()  # nothing ticked


def new_session(case):
    return case.env["vivo.count.session"].create({"location_id": case.location.id})
