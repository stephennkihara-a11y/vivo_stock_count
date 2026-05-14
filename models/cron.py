"""Phase 5 — monthly cross-store shrinkage roll-up.

Aggregates total variance value per store for the previous calendar
month and emails the result to the audit group. Feeds the existing BI
alerting pipeline per spec Section 4.8.
"""
from datetime import timedelta

from odoo import _, api, fields, models


class VivoCountReconciliation(models.Model):
    _inherit = "vivo.count.reconciliation"

    @api.model
    def cron_monthly_shrinkage_rollup(self):
        today = fields.Date.today()
        first_of_this_month = today.replace(day=1)
        first_of_prev = (first_of_this_month - timedelta(days=1)).replace(day=1)

        recons = self.search(
            [
                ("generated_at", ">=", fields.Datetime.to_string(first_of_prev)),
                ("generated_at", "<", fields.Datetime.to_string(first_of_this_month)),
            ]
        )
        if not recons:
            return

        by_store = {}
        for r in recons:
            d = by_store.setdefault(
                r.location_id,
                {
                    "total_variance": 0.0,
                    "overage": 0.0,
                    "shortage": 0.0,
                    "session_count": 0,
                    "currency": r.currency_id,
                },
            )
            d["total_variance"] += r.total_variance_value or 0.0
            d["overage"] += r.overage_value or 0.0
            d["shortage"] += r.shortage_value or 0.0
            d["session_count"] += 1

        rows = []
        for loc, d in by_store.items():
            rows.append(
                "<tr><td>%s</td><td style='text-align:right'>%d</td>"
                "<td style='text-align:right'>%.2f</td>"
                "<td style='text-align:right;color:#198754'>+%.2f</td>"
                "<td style='text-align:right;color:#dc3545'>%.2f</td></tr>"
                % (
                    loc.display_name,
                    d["session_count"],
                    d["total_variance"],
                    d["overage"],
                    d["shortage"],
                )
            )
        body = (
            "<h3>Monthly Stock Take Roll-up — %s</h3>"
            "<table border='1' cellpadding='6' style='border-collapse:collapse'>"
            "<thead><tr><th>Store</th><th>Sessions</th><th>Total |variance|</th>"
            "<th>Overage</th><th>Shortage</th></tr></thead>"
            "<tbody>%s</tbody></table>"
        ) % (first_of_prev.strftime("%B %Y"), "".join(rows))

        audit_group = self.env.ref(
            "vivo_stock_count.group_vivo_count_cfoo_audit"
        )
        partners = audit_group.users.mapped("partner_id")
        if partners:
            self.env["mail.mail"].sudo().create(
                {
                    "subject": "Monthly Stock Take Roll-up — %s"
                    % first_of_prev.strftime("%B %Y"),
                    "body_html": body,
                    "recipient_ids": [(6, 0, partners.ids)],
                }
            ).send()
        return body
