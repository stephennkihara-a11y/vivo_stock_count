from odoo import _, api, fields, models


class VivoCountSessionReconcileWizard(models.TransientModel):
    """Auditor's store-level reconcile — the clickable path from
    "Scanned — Awaiting Store Reconcile" to "Reconciled".

    The scanner finished every rack (there is no per-rack approver in the
    two-party flow). The auditor opens this from a session in `review`, records
    a mandatory variance/review note, and confirms: every scanned rack is
    reconciled in one transaction (per-line variance-reason gate enforced), any
    rack the scanner never finished is left out and flagged, and the acting
    auditor is stamped as reviewer. Confirmation runs
    ``vivo.count.session.action_reconcile_session``, which enforces the two-party
    guard (auditor != scanner) and the manager/auditor band.
    """

    _name = "vivo.count.session.reconcile.wizard"
    _description = "Vivo Count Store Reconcile Wizard"

    session_id = fields.Many2one(
        "vivo.count.session", required=True, readonly=True, ondelete="cascade"
    )
    session_name = fields.Char(related="session_id.name", readonly=True)
    currency_id = fields.Many2one(related="session_id.currency_id", readonly=True)

    sections_total = fields.Integer(compute="_compute_summary")
    sections_ready = fields.Integer(
        compute="_compute_summary",
        help="Scanned racks that will be reconciled now.",
    )
    sections_unfinished = fields.Integer(
        compute="_compute_summary",
        help="Racks the scanner has not finished — they will be LEFT OUT "
             "(excluded) of this reconcile and flagged.",
    )
    unfinished_names = fields.Char(compute="_compute_summary")
    variance_value = fields.Monetary(
        related="session_id.variance_value",
        currency_field="currency_id",
        readonly=True,
    )
    review_note = fields.Text(
        string="Variance / review note",
        required=True,
        help="Mandatory: record what the counts show and why you are "
             "reconciling. Stamped on every reconciled rack for the audit trail.",
    )

    @api.depends("session_id")
    def _compute_summary(self):
        for wiz in self:
            session = wiz.session_id
            if not session:
                wiz.sections_total = 0
                wiz.sections_ready = 0
                wiz.sections_unfinished = 0
                wiz.unfinished_names = ""
                continue
            _ready_pct, ready, outstanding = session._store_reconcile_readiness()
            wiz.sections_total = len(session.section_ids)
            wiz.sections_ready = len(ready)
            wiz.sections_unfinished = len(outstanding)
            wiz.unfinished_names = ", ".join(outstanding.mapped("name"))

    def action_confirm(self):
        self.ensure_one()
        # Reconciles every scanned rack (auditor != scanner + band + per-line
        # variance-reason gates enforced inside), excludes the rest, and stamps
        # the acting auditor as reviewer.
        self.session_id.action_reconcile_session(review_note=self.review_note)
        return {"type": "ir.actions.act_window_close"}
