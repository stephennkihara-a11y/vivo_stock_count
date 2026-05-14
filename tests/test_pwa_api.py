"""Phase 3 — PWA model API: idempotent scan replay, scan-once-type-qty,
section open/finish/physical submit, lock contention.

Covers acceptance criteria #6, #8, #13, #14.
"""
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase3")
class TestPwaApi(VivoCountCommon):

    def _open_section_as(self, section, user):
        section.scanner_id = user.id
        return section.with_user(user).open_for_scanning()

    # ------------------------------------------------------------------
    # AC #6 — scan-once-then-type-qty
    # ------------------------------------------------------------------
    def test_scan_once_type_qty_increments_scan_count_by_one(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        self._open_section_as(section, self.scanner)

        r = self.env["vivo.count.line"].with_user(self.scanner).record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=6.0,
            idempotency_key="k-1",
        )
        self.assertFalse(r.get("idempotent"))
        self.assertEqual(r["counted_qty"], 6.0)
        self.assertEqual(r["scan_count"], 1)

    def test_multiple_scans_accumulate(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        self._open_section_as(section, self.scanner)
        Line = self.env["vivo.count.line"].with_user(self.scanner)
        Line.record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=3,
            idempotency_key="k-a",
        )
        r = Line.record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=4,
            idempotency_key="k-b",
        )
        self.assertEqual(r["counted_qty"], 7)
        self.assertEqual(r["scan_count"], 2)

    # ------------------------------------------------------------------
    # AC #8 — idempotent replay
    # ------------------------------------------------------------------
    def test_replay_with_same_key_is_no_op(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        self._open_section_as(section, self.scanner)
        Line = self.env["vivo.count.line"].with_user(self.scanner)
        first = Line.record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=5,
            idempotency_key="dup-1",
        )
        replay = Line.record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=5,
            idempotency_key="dup-1",
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["scan_event_id"], first["scan_event_id"])
        self.assertEqual(replay["counted_qty"], 5)
        self.assertEqual(replay["scan_count"], 1)

    def test_offline_drain_50_scans_deterministic(self):
        """AC #8: 50 queued scans replayed end up as 50 events, no duplicates."""
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        self._open_section_as(section, self.scanner)
        Line = self.env["vivo.count.line"].with_user(self.scanner)
        for i in range(50):
            Line.record_scan(
                section_id=section.id,
                product_id=self.product_a.id,
                scanned_qty=1,
                idempotency_key="drain-%d" % i,
            )
        # Re-submit the entire batch (simulating a paranoid replay).
        for i in range(50):
            Line.record_scan(
                section_id=section.id,
                product_id=self.product_a.id,
                scanned_qty=1,
                idempotency_key="drain-%d" % i,
            )
        line = self.env["vivo.count.line"].search(
            [("section_id", "=", section.id), ("product_id", "=", self.product_a.id)]
        )
        self.assertEqual(len(line), 1)
        self.assertEqual(line.counted_qty, 50)
        events = self.env["vivo.count.scan.event"].search(
            [("section_id", "=", section.id)]
        )
        self.assertEqual(len(events), 50)

    def test_scan_rejected_when_section_not_open(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        # Section is still in 'draft' state — not scanning.
        with self.assertRaises(ValidationError):
            self.env["vivo.count.line"].record_scan(
                section_id=section.id,
                product_id=self.product_a.id,
                scanned_qty=1,
                idempotency_key="r-1",
            )

    # ------------------------------------------------------------------
    # AC #14 — soft lock contention
    # ------------------------------------------------------------------
    def test_open_section_blocks_second_user(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        section.with_user(self.scanner).open_for_scanning()
        with self.assertRaises(UserError):
            section.with_user(self.physical).open_for_scanning()

    def test_list_for_pwa_marks_lock_owner(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        section.with_user(self.scanner).open_for_scanning()
        rows = (
            self.env["vivo.count.section"]
            .with_user(self.physical)
            .list_for_pwa(session.id)
        )
        target = next(r for r in rows if r["id"] == section.id)
        self.assertEqual(target["locked_by_id"], self.scanner.id)
        self.assertFalse(target["is_mine"])
        # The original scanner sees their own lock.
        rows = (
            self.env["vivo.count.section"]
            .with_user(self.scanner)
            .list_for_pwa(session.id)
        )
        target = next(r for r in rows if r["id"] == section.id)
        self.assertTrue(target["is_mine"])

    # ------------------------------------------------------------------
    # Physical submit + replay
    # ------------------------------------------------------------------
    def test_physical_submit_replay_is_no_op(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        section.with_user(self.scanner).open_for_scanning()
        self.env["vivo.count.line"].with_user(self.scanner).record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=5,
            idempotency_key="p-scan",
        )
        section.with_user(self.scanner).finish_scanning_pwa()

        first = (
            section.with_user(self.physical)
            .submit_physical_pwa(physical_qty=5, idempotency_key="phys-1")
        )
        self.assertEqual(first["state"], "reconciled")
        replay = (
            section.with_user(self.physical)
            .submit_physical_pwa(physical_qty=5, idempotency_key="phys-1")
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["state"], "reconciled")
        # rescan_count not bumped by replay
        self.assertEqual(section.rescan_count, 0)

    def test_physical_submit_mismatch_routes_to_rescan(self):
        session = self._new_session()
        section = self._start_and_get_sections(session)[0]
        section.with_user(self.scanner).open_for_scanning()
        self.env["vivo.count.line"].with_user(self.scanner).record_scan(
            section_id=section.id,
            product_id=self.product_a.id,
            scanned_qty=5,
            idempotency_key="m-scan",
        )
        section.with_user(self.scanner).finish_scanning_pwa()
        r = (
            section.with_user(self.physical)
            .submit_physical_pwa(physical_qty=4, idempotency_key="phys-mm")
        )
        self.assertEqual(r["state"], "variance_rescan")
        self.assertEqual(section.rescan_count, 1)
