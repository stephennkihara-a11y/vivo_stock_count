from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class VivoCountReconciliation(models.Model):
    """Immutable Stock Take Reconciliation report.

    Auto-generated server-side the moment a session is applied (Phase 4).
    Phase 1 ships the model + the immutability guard only.
    """

    _name = "vivo.count.reconciliation"
    _description = "Vivo Stock Take Reconciliation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "generated_at desc, id desc"

    name = fields.Char(required=True, copy=False, readonly=True, default=lambda self: _("New"))
    session_id = fields.Many2one(
        "vivo.count.session", required=True, ondelete="restrict", readonly=True
    )
    location_id = fields.Many2one(
        "stock.location", related="session_id.location_id", store=True, readonly=True
    )
    company_id = fields.Many2one(
        "res.company", related="session_id.company_id", store=True, readonly=True
    )
    currency_id = fields.Many2one(
        "res.currency", related="company_id.currency_id", readonly=True
    )
    generated_at = fields.Datetime(readonly=True)

    line_ids = fields.One2many(
        "vivo.count.reconciliation.line", "reconciliation_id", readonly=True
    )

    total_variance_qty = fields.Float(compute="_compute_totals", store=True)
    total_variance_value = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id"
    )
    overage_value = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id"
    )
    shortage_value = fields.Monetary(
        compute="_compute_totals", store=True, currency_field="currency_id"
    )

    variance_band = fields.Selection(
        [("auto", "Store Manager"), ("regional", "Regional Manager"), ("cfoo", "CFOO")],
        readonly=True,
    )

    scanner_ids = fields.Many2many("res.users", "vivo_recon_scanner_rel", "recon_id", "user_id", readonly=True)
    physical_counter_ids = fields.Many2many(
        "res.users", "vivo_recon_phys_rel", "recon_id", "user_id", readonly=True
    )
    reviewer_id = fields.Many2one("res.users", readonly=True)
    approver_id = fields.Many2one("res.users", readonly=True)
    applied_by_id = fields.Many2one("res.users", readonly=True)

    state = fields.Selection(
        [("generated", "Generated")], default="generated", readonly=True, copy=False
    )

    @api.depends("line_ids.qty_variance", "line_ids.value_variance")
    def _compute_totals(self):
        for rec in self:
            rec.total_variance_qty = sum(abs(l.qty_variance) for l in rec.line_ids)
            rec.total_variance_value = sum(abs(l.value_variance) for l in rec.line_ids)
            rec.overage_value = sum(l.value_variance for l in rec.line_ids if l.value_variance > 0)
            rec.shortage_value = sum(l.value_variance for l in rec.line_ids if l.value_variance < 0)

    def write(self, vals):
        """AC #18: immutable for everyone, including CFOO/Audit.

        The only write the system itself performs is during initial
        generation (Phase 4), which uses sudo + create — never write.
        Mail-thread / activity-mixin internal fields are permitted so
        chatter still works.
        """
        allowed = {
            "message_follower_ids",
            "message_ids",
            "message_main_attachment_id",
            "activity_ids",
            "message_attachment_count",
        }
        forbidden = set(vals) - allowed
        if forbidden:
            raise AccessError(
                _(
                    "Reconciliation reports are immutable. Forbidden edit on: %s"
                )
                % ", ".join(sorted(forbidden))
            )
        return super().write(vals)

    def unlink(self):
        raise AccessError(_("Reconciliation reports cannot be deleted."))

    # ------------------------------------------------------------------
    # Phase 5 — audit notification + export actions
    # ------------------------------------------------------------------
    def _audit_notify_group(self):
        """Resolve the audit recipient group from config or default."""
        Param = self.env["ir.config_parameter"].sudo()
        gid = Param.get_param("vivo_count.audit_notify_group_id")
        if gid:
            try:
                return self.env["res.groups"].browse(int(gid))
            except (ValueError, TypeError):
                pass
        return self.env.ref("vivo_stock_count.group_vivo_count_cfoo_audit")

    def notify_audit(self):
        """Post chatter + schedule activities for the audit group (AC #17).

        Called by the session's _generate_reconciliation at Apply time so
        the audit team gets a working link the moment the report exists,
        not at end-of-day.
        """
        self.ensure_one()
        group = self._audit_notify_group()
        if not group:
            return
        partners = group.users.mapped("partner_id")
        body = (
            "<p>Stock Take Reconciliation <strong>%s</strong> generated for "
            "store <strong>%s</strong>.</p>"
            "<p>Total variance value: %s. Band triggered: %s.</p>"
            "<p>Applied by: %s.</p>"
        ) % (
            self.name,
            self.location_id.display_name,
            self.total_variance_value,
            dict(self._fields["variance_band"].selection).get(
                self.variance_band, ""
            ),
            self.applied_by_id.name or "",
        )
        # message_post writes through the immutability guard's allowlist.
        self.message_post(
            body=body,
            partner_ids=partners.ids,
            subject="Reconciliation %s ready for review" % self.name,
            subtype_xmlid="mail.mt_comment",
        )
        try:
            activity_type = self.env.ref("mail.mail_activity_data_todo")
        except ValueError:
            activity_type = False
        if activity_type:
            for user in group.users:
                self.activity_schedule(
                    activity_type_id=activity_type.id,
                    user_id=user.id,
                    summary="Review stock take reconciliation %s" % self.name,
                    note=body,
                )

    def action_print_pdf(self):
        self.ensure_one()
        return self.env.ref(
            "vivo_stock_count.action_report_reconciliation_pdf"
        ).report_action(self)

    def action_export_xlsx(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/vivo_stock_count/reconciliation/%d/xlsx" % self.id,
            "target": "self",
        }


class VivoCountReconciliationLine(models.Model):
    _name = "vivo.count.reconciliation.line"
    _description = "Vivo Stock Take Reconciliation Line"
    _order = "reconciliation_id, zone_id, section_id, product_id"

    reconciliation_id = fields.Many2one(
        "vivo.count.reconciliation", required=True, ondelete="cascade", index=True
    )
    product_id = fields.Many2one("product.product", required=True, readonly=True)
    barcode = fields.Char(readonly=True)
    zone_id = fields.Many2one("vivo.count.zone", readonly=True)
    section_id = fields.Many2one("vivo.count.section", readonly=True)

    qty_before = fields.Float(readonly=True, digits="Product Unit of Measure")
    qty_after = fields.Float(readonly=True, digits="Product Unit of Measure")
    qty_variance = fields.Float(
        compute="_compute_variance", store=True, digits="Product Unit of Measure"
    )

    currency_id = fields.Many2one(
        related="reconciliation_id.currency_id", readonly=True
    )
    value_before = fields.Monetary(readonly=True, currency_field="currency_id")
    value_after = fields.Monetary(readonly=True, currency_field="currency_id")
    value_variance = fields.Monetary(
        compute="_compute_variance", store=True, currency_field="currency_id"
    )

    variance_type = fields.Selection(
        [("none", "None"), ("overage", "Overage"), ("shortage", "Shortage")],
        compute="_compute_variance", store=True,
    )
    has_variance = fields.Boolean(compute="_compute_variance", store=True)
    variance_reason = fields.Char(readonly=True)
    section_rescan_count = fields.Integer(readonly=True)

    @api.depends("qty_before", "qty_after", "value_before", "value_after")
    def _compute_variance(self):
        for line in self:
            line.qty_variance = (line.qty_after or 0.0) - (line.qty_before or 0.0)
            line.value_variance = (line.value_after or 0.0) - (line.value_before or 0.0)
            if line.qty_variance > 0 or line.value_variance > 0:
                line.variance_type = "overage"
            elif line.qty_variance < 0 or line.value_variance < 0:
                line.variance_type = "shortage"
            else:
                line.variance_type = "none"
            line.has_variance = line.variance_type != "none"

    def write(self, vals):
        raise AccessError(_("Reconciliation lines are immutable."))

    def unlink(self):
        raise AccessError(_("Reconciliation lines cannot be deleted."))
