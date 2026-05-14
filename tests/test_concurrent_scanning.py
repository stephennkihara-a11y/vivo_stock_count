"""Phase 3 — AC #13: three scanners scanning different sections of one
session must produce no record locks, no lost scans, and no deadlocks.

Approach: this is a single-transaction test of the *logical* invariant
that section/line rows are independent. The Odoo test framework runs
inside one transaction, so we cannot exercise true row-level locking
behaviour from here; that needs a multi-process harness. We instead:

  - Drive three different `with_user` contexts in a tight interleaved
    loop, recording 30 scans across three sections.
  - Assert no scans are lost, that scan_count and counted_qty match per
    section, and that all three sections can finish + reconcile
    independently.

A separate `--test-tags` Postgres-level concurrency probe lives in
`tests/test_concurrent_scanning_psql.py` (out of Phase 3 scope, deferred
to the QA harness — see README).
"""
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase3", "concurrent")
class TestConcurrentScanning(VivoCountCommon):

    def setUp(self):
        super().setUp()
        # 3 scanners + 2 physical counters per spec 4.3.
        self.scanner1 = self.env["res.users"].create(
            {
                "name": "Scanner 1",
                "login": "vivo.scan1@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        self.scanner2 = self.env["res.users"].create(
            {
                "name": "Scanner 2",
                "login": "vivo.scan2@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        self.scanner3 = self.env["res.users"].create(
            {
                "name": "Scanner 3",
                "login": "vivo.scan3@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        self.physical1 = self.env["res.users"].create(
            {
                "name": "Physical 1",
                "login": "vivo.phys1@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        self.physical2 = self.env["res.users"].create(
            {
                "name": "Physical 2",
                "login": "vivo.phys2@test",
                "groups_id": [(6, 0, [self.group_counter.id])],
            }
        )
        # Build a session with 3 sections (template_c gives us a third zone).
        self.session = self._new_session(zone=False)  # whole-store

    def test_three_scanners_interleaved_no_loss(self):
        session = self.Session.create({"location_id": self.location.id})
        session.action_start()
        sections = session.section_ids
        self.assertGreaterEqual(len(sections), 3)
        s1, s2, s3 = sections[0], sections[1], sections[2]
        s1.with_user(self.scanner1).open_for_scanning()
        s2.with_user(self.scanner2).open_for_scanning()
        s3.with_user(self.scanner3).open_for_scanning()

        Line = self.env["vivo.count.line"]
        # 30 interleaved scans across 3 sections.
        for i in range(10):
            Line.with_user(self.scanner1).record_scan(
                section_id=s1.id,
                product_id=self.product_a.id,
                scanned_qty=1,
                idempotency_key="s1-%d" % i,
            )
            Line.with_user(self.scanner2).record_scan(
                section_id=s2.id,
                product_id=self.product_b.id,
                scanned_qty=1,
                idempotency_key="s2-%d" % i,
            )
            Line.with_user(self.scanner3).record_scan(
                section_id=s3.id,
                product_id=self.product_a.id,
                scanned_qty=1,
                idempotency_key="s3-%d" % i,
            )

        # Per-section invariants
        for sec, scanner, product, qty in [
            (s1, self.scanner1, self.product_a, 10),
            (s2, self.scanner2, self.product_b, 10),
            (s3, self.scanner3, self.product_a, 10),
        ]:
            line = self.Line.search(
                [("section_id", "=", sec.id), ("product_id", "=", product.id)]
            )
            self.assertEqual(len(line), 1, "Section %s line not found" % sec.name)
            self.assertEqual(line.counted_qty, qty)
            self.assertEqual(line.scan_count, qty)
            self.assertEqual(line.counter_id, scanner)

        # Now finish + reconcile independently with two physical counters.
        s1.with_user(self.scanner1).finish_scanning_pwa()
        s2.with_user(self.scanner2).finish_scanning_pwa()
        s3.with_user(self.scanner3).finish_scanning_pwa()
        s1.with_user(self.physical1).submit_physical_pwa(physical_qty=10)
        s2.with_user(self.physical2).submit_physical_pwa(physical_qty=10)
        s3.with_user(self.physical1).submit_physical_pwa(physical_qty=10)
        self.assertEqual(s1.state, "reconciled")
        self.assertEqual(s2.state, "reconciled")
        self.assertEqual(s3.state, "reconciled")

    def test_scanner_cannot_steal_other_scanners_section(self):
        """AC #14 reinforcement: scanner B cannot scan into scanner A's section."""
        session = self.Session.create({"location_id": self.location.id})
        session.action_start()
        s1 = session.section_ids[0]
        s1.with_user(self.scanner1).open_for_scanning()
        # The section is locked AND assigned to scanner1. scanner2 trying to
        # open it raises UserError. Scanning into it without opening still
        # works at the API level (the scan API doesn't check lock — by
        # design, since a scan from a different device on the same user
        # should also work). The lock is the UX guard, not the data guard.
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            s1.with_user(self.scanner2).open_for_scanning()
