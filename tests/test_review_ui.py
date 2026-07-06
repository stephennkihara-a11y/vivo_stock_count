"""Phase 2 — review UI: ETA, variance summary, reviewer auto-set,
approval wizard, bounce wizard, view-load smoke.
"""
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import tagged

from .common import VivoCountCommon


@tagged("vivo_count", "phase2")
class TestReviewUI(VivoCountCommon):

    # ------------------------------------------------------------------
    # Reviewer auto-set on Submit-for-Review
    # ------------------------------------------------------------------
    def test_submit_for_review_autoset_reviewer(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        for s in sections:
            self._reconcile_section(s, self.scanner, self.physical, 5)
        session.with_user(self.store_manager).action_submit_for_review()
        self.assertEqual(session.reviewer_id, self.store_manager)
        self.assertEqual(session.state, "review")

    def test_reviewer_id_not_overwritten_when_already_set(self):
        session = self._new_session()
        session.reviewer_id = self.regional.id
        sections = self._start_and_get_sections(session)
        for s in sections:
            self._reconcile_section(s, self.scanner, self.physical, 5)
        session.with_user(self.store_manager).action_submit_for_review()
        self.assertEqual(session.reviewer_id, self.regional)

    # ------------------------------------------------------------------
    # ETA + progress
    # ------------------------------------------------------------------
    def test_progress_pct_and_eta_compute(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        # Backdate start so ETA is computable.
        session.start_date = fields.Datetime.now() - timedelta(minutes=20)
        # Reconcile 1 of 2 sections.
        self._reconcile_section(sections[0], self.scanner, self.physical, 5)
        session.invalidate_recordset()  # force re-compute
        self.assertEqual(session.sections_reconciled, 1)
        self.assertAlmostEqual(session.progress_pct, 50.0, places=1)
        self.assertTrue(session.minutes_per_section > 0)
        self.assertTrue(session.estimated_completion)

    def test_progress_zero_when_nothing_reconciled(self):
        session = self._new_session()
        self._start_and_get_sections(session)
        self.assertEqual(session.sections_reconciled, 0)
        self.assertEqual(session.progress_pct, 0.0)
        self.assertFalse(session.estimated_completion)

    # ------------------------------------------------------------------
    # Variance summary
    # ------------------------------------------------------------------
    def test_variance_summary_counts(self):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        # Section 0 zero-variance; section 1 has 1 reasoned variance line.
        # A section can no longer reconcile while a counted variance lacks a
        # reason (the review gate blocks it), so a reconciled variance is
        # always reasoned -> unreasoned_line_count is 0.
        self._reconcile_section(sections[0], self.scanner, self.physical, 5)
        s = sections[1]
        s.scanner_id = self.scanner.id
        s.with_user(self.scanner).action_start_scanning()
        self.Line.create(
            {
                "section_id": s.id,
                "product_id": self.product_a.id,
                "system_qty": 10.0,
                "counted_qty": 9.0,
                "unit_cost": self.product_a.standard_price,
                "variance_reason": "miscount",
            }
        )
        s.with_user(self.scanner).action_finish_scanning()
        s.physical_counter_id = self.physical.id
        s.with_user(self.physical).action_submit_physical_count(physical_qty=9)
        self._confirm_section_review(s)
        session.invalidate_recordset()
        self.assertEqual(session.variance_line_count, 1)
        self.assertEqual(session.sections_with_variance, 1)
        self.assertEqual(session.unreasoned_line_count, 0)

    # ------------------------------------------------------------------
    # Approval wizard
    # ------------------------------------------------------------------
    def _session_in_review(self, with_variance=False):
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self._reconcile_section(sections[0], self.scanner, self.physical, 5)
        if with_variance:
            s = sections[1]
            s.scanner_id = self.scanner.id
            s.with_user(self.scanner).action_start_scanning()
            self.Line.create(
                {
                    "section_id": s.id,
                    "product_id": self.product_a.id,
                    "system_qty": 10.0,
                    "counted_qty": 9.0,
                    "unit_cost": self.product_a.standard_price,
                    "variance_reason": "miscount",
                }
            )
            s.with_user(self.scanner).action_finish_scanning()
            s.physical_counter_id = self.physical.id
            s.with_user(self.physical).action_submit_physical_count(physical_qty=9)
            # Variance -> pending_review; line has a reason, so confirm it.
            self._confirm_section_review(s)
        else:
            self._reconcile_section(sections[1], self.scanner, self.physical, 5)
        session.action_submit_for_review()
        return session

    def test_approval_wizard_blocks_when_unreasoned_lines(self):
        # The section review gate now requires a reason before a variance can
        # reconcile, so build a reconciled+reasoned variance, then simulate an
        # auditor clearing the reason afterwards. The approval wizard must
        # still catch the resulting unreasoned line as a blocker (defence in
        # depth).
        session = self._session_in_review(with_variance=True)
        varied = session.line_ids.filtered(
            lambda l: l.line_status == "counted" and l.difference != 0.0
        )
        varied.variance_reason = False
        session.invalidate_recordset()
        wiz = (
            self.env["vivo.count.approval.wizard"]
            .with_user(self.store_manager)
            .create({"session_id": session.id})
        )
        self.assertTrue(wiz.is_blocked)
        self.assertIn("no reason", wiz.blocker_messages.lower())

    def test_approval_wizard_blocks_on_band_mismatch(self):
        # Build a session with a big variance so band=cfoo.
        session = self._new_session()
        sections = self._start_and_get_sections(session)
        self._reconcile_section(sections[0], self.scanner, self.physical, 5)
        s = sections[1]
        s.scanner_id = self.scanner.id
        s.with_user(self.scanner).action_start_scanning()
        # 100 units * 500 = 50_000 variance value -> cfoo band.
        self.Line.create(
            {
                "section_id": s.id,
                "product_id": self.product_a.id,
                "system_qty": 100.0,
                "counted_qty": 0.0,
                "unit_cost": self.product_a.standard_price,
                "variance_reason": "theft",
            }
        )
        s.with_user(self.scanner).action_finish_scanning()
        s.physical_counter_id = self.physical.id
        s.with_user(self.physical).action_submit_physical_count(physical_qty=0)
        session.action_submit_for_review()

        wiz = (
            self.env["vivo.count.approval.wizard"]
            .with_user(self.store_manager)
            .create({"session_id": session.id})
        )
        self.assertEqual(session.tolerance_band, "cfoo")
        self.assertTrue(wiz.is_blocked)
        self.assertFalse(wiz.band_authority_ok)

    def test_approval_wizard_sod_block(self):
        # Store manager scanned a section — wizard must block them.
        self.scanner.groups_id = [(4, self.group_store_mgr.id)]
        session = self._session_in_review()
        wiz = (
            self.env["vivo.count.approval.wizard"]
            .with_user(self.scanner)
            .create({"session_id": session.id})
        )
        self.assertTrue(wiz.is_blocked)
        self.assertFalse(wiz.sod_ok)

    def test_approval_wizard_confirm_advances_state(self):
        session = self._session_in_review()
        wiz = (
            self.env["vivo.count.approval.wizard"]
            .with_user(self.store_manager)
            .create({"session_id": session.id})
        )
        self.assertFalse(wiz.is_blocked)
        wiz.with_user(self.store_manager).action_confirm_approve()
        session.invalidate_recordset()
        self.assertEqual(session.state, "approved")

    # ------------------------------------------------------------------
    # Bounce wizard
    # ------------------------------------------------------------------
    def test_bounce_wizard_resets_only_selected(self):
        session = self._session_in_review()
        target = session.section_ids[0]
        other = session.section_ids[1]
        wiz = self.env["vivo.count.bounce.wizard"].create(
            {
                "session_id": session.id,
                "section_ids": [(6, 0, [target.id])],
                "reason": "Counter reported a missed shelf",
            }
        )
        wiz.action_bounce()
        self.assertEqual(session.state, "in_progress")
        self.assertEqual(target.state, "scanning")
        self.assertEqual(other.state, "reconciled")
        self.assertEqual(target.rescan_count, 1)

    def test_bounce_wizard_requires_a_selection(self):
        session = self._session_in_review()
        wiz = self.env["vivo.count.bounce.wizard"].create(
            {"session_id": session.id, "reason": "stub"}
        )
        with self.assertRaises(UserError):
            wiz.action_bounce()

    # ------------------------------------------------------------------
    # View-load smoke
    # ------------------------------------------------------------------
    def test_phase2_views_load(self):
        view_xmlids = [
            "vivo_stock_count.view_vivo_count_session_kanban",
            "vivo_stock_count.view_vivo_count_session_list",
            "vivo_stock_count.view_vivo_count_session_form",
            "vivo_stock_count.view_vivo_count_session_search",
            "vivo_stock_count.view_vivo_count_section_kanban",
            "vivo_stock_count.view_vivo_count_section_list",
            "vivo_stock_count.view_vivo_count_section_form",
            "vivo_stock_count.view_vivo_count_section_search",
            "vivo_stock_count.view_vivo_count_line_pivot",
            "vivo_stock_count.view_vivo_count_line_graph",
            "vivo_stock_count.view_vivo_count_line_search",
            "vivo_stock_count.view_vivo_approval_wizard_form",
            "vivo_stock_count.view_vivo_bounce_wizard_form",
        ]
        for xmlid in view_xmlids:
            view = self.env.ref(xmlid)
            # arch is parsed at load time — just touching it forces validation.
            self.assertTrue(view.arch, "View %s has empty arch" % xmlid)
