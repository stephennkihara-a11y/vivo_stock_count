from odoo import _, api, fields, models
from odoo.exceptions import UserError


class VivoCountApprovalWizard(models.TransientModel):
    """Approval preview — shows the operator everything that affects the
    approval decision before they click through. Mirrors the spec's
    'Approval wizard at session level: previews journal impact, shows
    tolerance band, surfaces blocking issues before Apply is enabled.'
    """

    _name = "vivo.count.approval.wizard"
    _description = "Vivo Count Approval Wizard"

    session_id = fields.Many2one(
        "vivo.count.session", required=True, readonly=True
    )
    state = fields.Selection(related="session_id.state", readonly=True)
    location_id = fields.Many2one(
        related="session_id.location_id", readonly=True
    )
    tolerance_band = fields.Selection(
        related="session_id.tolerance_band", readonly=True
    )
    variance_value = fields.Monetary(
        related="session_id.variance_value", readonly=True
    )
    currency_id = fields.Many2one(
        related="session_id.currency_id", readonly=True
    )

    sections_total = fields.Integer(
        related="session_id.sections_total", readonly=True
    )
    sections_reconciled = fields.Integer(
        related="session_id.sections_reconciled", readonly=True
    )
    sections_outstanding = fields.Integer(
        related="session_id.sections_outstanding", readonly=True
    )
    variance_line_count = fields.Integer(
        related="session_id.variance_line_count", readonly=True
    )
    sections_with_variance = fields.Integer(
        related="session_id.sections_with_variance", readonly=True
    )
    unreasoned_line_count = fields.Integer(
        related="session_id.unreasoned_line_count", readonly=True
    )

    blocker_messages = fields.Text(compute="_compute_blockers")
    is_blocked = fields.Boolean(compute="_compute_blockers")
    band_authority_ok = fields.Boolean(compute="_compute_blockers")
    sod_ok = fields.Boolean(compute="_compute_blockers")

    @api.depends(
        "session_id.state",
        "session_id.sections_outstanding",
        "session_id.unreasoned_line_count",
        "session_id.tolerance_band",
    )
    def _compute_blockers(self):
        for wiz in self:
            messages = []
            session = wiz.session_id
            user = self.env.user

            if session.state != "review":
                messages.append(
                    _("Session must be in Review (currently: %s).") % session.state
                )
            if session.sections_outstanding:
                messages.append(
                    _("%d section(s) still unreconciled.")
                    % session.sections_outstanding
                )
            # Per-line variance reasons are optional and do NOT block approval.

            # Band authority
            band = session.tolerance_band
            band_ok = True
            if band == "auto" and not user.has_group(
                "vivo_stock_count.group_vivo_count_store_manager"
            ):
                band_ok = False
                messages.append(_("You do not have Store Manager rights."))
            elif band == "regional" and not user.has_group(
                "vivo_stock_count.group_vivo_count_regional"
            ):
                band_ok = False
                messages.append(
                    _(
                        "Variance %.2f is in the Regional band — "
                        "Regional Manager (or higher) approval required."
                    )
                    % session.variance_value
                )
            elif band == "cfoo" and not user.has_group(
                "vivo_stock_count.group_vivo_count_cfoo_audit"
            ):
                band_ok = False
                messages.append(
                    _(
                        "Variance %.2f is in the CFOO band — "
                        "CFOO / Audit approval required."
                    )
                    % session.variance_value
                )
            wiz.band_authority_ok = band_ok

            # Two-party SoD: the auditor approving must not be the scanner.
            scanners = session.section_ids.mapped("scanner_id")
            sod_ok = user not in scanners
            if not sod_ok:
                messages.append(
                    _("You scanned on this session — a different auditor must approve it.")
                )
            wiz.sod_ok = sod_ok

            wiz.blocker_messages = "\n".join(messages) if messages else ""
            wiz.is_blocked = bool(messages)

    def action_confirm_approve(self):
        self.ensure_one()
        if self.is_blocked:
            raise UserError(_("Cannot approve while blockers exist:\n%s") % self.blocker_messages)
        self.session_id.action_approve()
        return {"type": "ir.actions.act_window_close"}
