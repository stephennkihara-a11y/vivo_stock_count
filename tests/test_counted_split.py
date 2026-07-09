"""Option 2 — counted vs. uncounted split, and desktop reconcile path.

A section loads every store SKU, but only the items physically scanned on
the rack are "counted". Items that live on other racks stay "not counted
here": they must not read as variances, must not demand a reason, and must
not block reconcile or approval — yet an SKU counted on NO rack of the
session is still surfaced as a genuine shortage.
"""
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestCountedSplit(VivoCountCommon):

    def _line(self, section, product, system_qty, counted_qty):
        return self.Line.create(
            {
                "section_id": section.id,
                "product_id": product.id,
                "system_qty": system_qty,
                "counted_qty": counted_qty,
                "unit_cost": product.standard_price,
            }
        )

    def test_line_status_computed(self):
        """counted_qty > 0 => counted; == 0 => not_counted."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        counted = self._line(section, self.product_a, 5.0, 5.0)
        uncounted = self._line(section, self.product_b, 8.0, 0.0)
        self.assertEqual(counted.line_status, "counted")
        self.assertEqual(uncounted.line_status, "not_counted")
        # Section split fields reflect the two buckets.
        self.assertEqual(section.counted_line_count, 1)
        self.assertEqual(section.not_counted_line_count, 1)
        self.assertEqual(section.not_counted_line_ids, uncounted)
        # A "not counted here" line does not contribute to the scan total.
        self.assertEqual(section.scan_total_qty, 5.0)

    def test_uncounted_line_does_not_block_approval(self):
        """The headline fix: an uncounted snapshot line (negative difference,
        no reason) must NOT demand a variance reason nor block approval."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        # One item actually on the rack (no variance) + one system SKU that
        # lives on another rack (counted 0 here). Keep the shortage value in
        # the store band so a store manager can approve.
        self._line(section, self.product_a, 5.0, 5.0)
        self._line(section, self.product_b, 2.0, 0.0)
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=5)
        # Submit routes to pending_review; the auditor confirms (no auto-close).
        self.assertEqual(section.state, "pending_review")
        section.action_confirm_reconcile()
        self.assertEqual(section.state, "reconciled")

        # Reconcile the remaining section so the session can advance.
        self._reconcile_section(sections[1], self.scanner, self.physical, 0, 0)

        session.action_submit_for_review()
        # No line counted on a rack has an unreasoned variance, so approval
        # goes through even though product_b is short on this rack.
        self.assertEqual(session.unreasoned_line_count, 0)
        session.with_user(self.store_manager).action_approve()
        self.assertEqual(session.state, "approved")

    def test_uncounted_rollup_flags_genuine_shortage(self):
        """An SKU counted on no rack of the session is flagged as a shortage."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        self._line(section, self.product_a, 5.0, 5.0)   # counted somewhere
        self._line(section, self.product_b, 2.0, 0.0)   # counted nowhere
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=5)

        self.assertEqual(session.uncounted_sku_count, 1)
        self.assertEqual(
            session.uncounted_shortage_value, 2.0 * self.product_b.standard_price
        )
        uncounted_ids = session._uncounted_line_ids()
        self.assertEqual(len(uncounted_ids), 1)
        self.assertEqual(
            self.Line.browse(uncounted_ids).product_id, self.product_b
        )

    def test_uncounted_cleared_when_counted_on_another_rack(self):
        """If the SKU is later counted on another rack, it is no longer a shortage."""
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        # Rack A: product_b is a snapshot line, not scanned here.
        rack_a, rack_b = sections[0], sections[1]
        rack_a.scanner_id = self.scanner.id
        rack_a.with_user(self.scanner).action_start_scanning()
        self._line(rack_a, self.product_b, 3.0, 0.0)
        rack_a.with_user(self.scanner).action_finish_scanning()
        rack_a.physical_counter_id = self.physical.id
        rack_a.with_user(self.physical).action_submit_physical_count(physical_qty=0)
        self.assertEqual(session.uncounted_sku_count, 1)
        # Rack B: product_b is actually scanned here.
        rack_b.scanner_id = self.scanner.id
        rack_b.with_user(self.scanner).action_start_scanning()
        self._line(rack_b, self.product_b, 0.0, 3.0)
        rack_b.with_user(self.scanner).action_finish_scanning()
        rack_b.physical_counter_id = self.physical.id
        rack_b.with_user(self.physical).action_submit_physical_count(physical_qty=3)
        self.assertEqual(session.uncounted_sku_count, 0)

    def test_desktop_reconcile_without_pwa(self):
        """Scanning -> Physical Count -> Reconciled entirely on desktop.

        The desktop 'Submit Physical Count' button calls the action with no
        argument, relying on the physical_total_qty field typed on the form.
        """
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        self.assertEqual(section.state, "scanning")
        self._line(section, self.product_a, 6.0, 6.0)
        section.with_user(self.scanner).action_finish_scanning()
        self.assertEqual(section.state, "physical_count")
        # Physical counter types the headcount into the desktop field...
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).physical_total_qty = 6.0
        # ...then clicks Submit Physical Count (no qty passed).
        section.with_user(self.physical).action_submit_physical_count()
        # Submit routes to pending_review; the auditor then reconciles.
        self.assertEqual(section.state, "pending_review")
        section.action_confirm_reconcile()
        self.assertEqual(section.state, "reconciled")
        self.assertTrue(section.is_reconciled)

    def test_counted_line_with_variance_still_needs_reason(self):
        """Regression guard: a line actually counted on the rack that differs
        from the system is a genuine variance — it routes to review and cannot
        reconcile without a reason."""
        from odoo.exceptions import ValidationError

        session = self._new_session()
        sections = self._start_and_get_sections(session)
        section = sections[0]
        section.scanner_id = self.scanner.id
        section.with_user(self.scanner).action_start_scanning()
        # Counted 4 but system says 5 — a real rack-level shortfall.
        self._line(section, self.product_a, 5.0, 4.0)
        section.with_user(self.scanner).action_finish_scanning()
        section.physical_counter_id = self.physical.id
        section.with_user(self.physical).action_submit_physical_count(physical_qty=4)
        self.assertEqual(section.state, "pending_review")
        with self.assertRaises(ValidationError):
            section.action_confirm_reconcile()
