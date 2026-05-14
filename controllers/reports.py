"""Phase 5 — Excel export endpoint for the Stock Take Reconciliation.

Uses xlsxwriter (bundled with Odoo). Variance highlighting per AC #19
survives the export: shortage rows fill red, overage rows fill green.
"""
import io

import xlsxwriter

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request


class VivoCountReports(http.Controller):

    @http.route(
        "/vivo_stock_count/reconciliation/<int:recon_id>/xlsx",
        auth="user",
        type="http",
        csrf=False,
    )
    def export_reconciliation_xlsx(self, recon_id, **kw):
        recon = request.env["vivo.count.reconciliation"].browse(recon_id)
        # Force a read-permission check.
        recon.check_access("read")

        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})

        # Formats
        f_title = wb.add_format(
            {"bold": True, "font_size": 14, "bottom": 1}
        )
        f_header = wb.add_format(
            {"bold": True, "bg_color": "#0066cc", "font_color": "#fff", "border": 1}
        )
        f_label = wb.add_format({"bold": True})
        f_money = wb.add_format({"num_format": "#,##0.00"})
        f_int = wb.add_format({"num_format": "#,##0"})
        f_overage = wb.add_format(
            {"bg_color": "#e8f5e9", "border": 1, "num_format": "#,##0.00"}
        )
        f_shortage = wb.add_format(
            {"bg_color": "#fdecea", "border": 1, "num_format": "#,##0.00"}
        )
        f_normal = wb.add_format({"border": 1, "num_format": "#,##0.00"})
        f_text_cell = wb.add_format({"border": 1})

        # ---- Sheet 1: Summary ----
        ws = wb.add_worksheet("Summary")
        ws.set_column("A:A", 32)
        ws.set_column("B:B", 32)
        ws.write("A1", "Stock Take Reconciliation", f_title)
        ws.write("A2", "Reference", f_label)
        ws.write("B2", recon.name)
        ws.write("A3", "Store", f_label)
        ws.write("B3", recon.location_id.display_name or "")
        ws.write("A4", "Generated", f_label)
        ws.write(
            "B4",
            recon.generated_at.strftime("%Y-%m-%d %H:%M") if recon.generated_at else "",
        )
        ws.write("A5", "Variance band", f_label)
        ws.write("B5", recon.variance_band or "")
        ws.write("A6", "Reviewer", f_label)
        ws.write("B6", recon.reviewer_id.name or "")
        ws.write("A7", "Approver", f_label)
        ws.write("B7", recon.approver_id.name or "")
        ws.write("A8", "Applied by", f_label)
        ws.write("B8", recon.applied_by_id.name or "")
        ws.write("A9", "Scanners", f_label)
        ws.write("B9", ", ".join(recon.scanner_ids.mapped("name")))
        ws.write("A10", "Physical counters", f_label)
        ws.write("B10", ", ".join(recon.physical_counter_ids.mapped("name")))

        ws.write("A12", "Total absolute variance qty", f_label)
        ws.write("B12", recon.total_variance_qty, f_int)
        ws.write("A13", "Total absolute variance value", f_label)
        ws.write("B13", recon.total_variance_value, f_money)
        ws.write("A14", "Overage value", f_label)
        ws.write("B14", recon.overage_value, f_money)
        ws.write("A15", "Shortage value", f_label)
        ws.write("B15", recon.shortage_value, f_money)

        # ---- Sheet 2: Lines ----
        ws2 = wb.add_worksheet("Variance Lines")
        headers = [
            "Zone",
            "Section",
            "Barcode",
            "Product",
            "Qty Before",
            "Qty After",
            "Qty Variance",
            "Value Before",
            "Value After",
            "Value Variance",
            "Variance Type",
            "Reason",
            "Section Rescans",
        ]
        widths = [22, 22, 18, 36, 12, 12, 14, 14, 14, 14, 14, 22, 10]
        for col, (h, w) in enumerate(zip(headers, widths)):
            ws2.write(0, col, h, f_header)
            ws2.set_column(col, col, w)

        row = 1
        for line in recon.line_ids:
            fmt = (
                f_overage
                if line.variance_type == "overage"
                else f_shortage
                if line.variance_type == "shortage"
                else f_normal
            )
            ws2.write(row, 0, line.zone_id.name or "Multiple", f_text_cell)
            ws2.write(row, 1, line.section_id.name or "Multiple", f_text_cell)
            ws2.write(row, 2, line.barcode or "", f_text_cell)
            ws2.write(row, 3, line.product_id.display_name or "", f_text_cell)
            ws2.write(row, 4, line.qty_before or 0.0, fmt)
            ws2.write(row, 5, line.qty_after or 0.0, fmt)
            ws2.write(row, 6, line.qty_variance or 0.0, fmt)
            ws2.write(row, 7, line.value_before or 0.0, fmt)
            ws2.write(row, 8, line.value_after or 0.0, fmt)
            ws2.write(row, 9, line.value_variance or 0.0, fmt)
            ws2.write(row, 10, line.variance_type or "", f_text_cell)
            ws2.write(row, 11, line.variance_reason or "", f_text_cell)
            ws2.write(row, 12, line.section_rescan_count or 0, f_text_cell)
            row += 1

        ws2.autofilter(0, 0, max(row - 1, 1), len(headers) - 1)
        ws2.freeze_panes(1, 0)

        wb.close()
        data = buf.getvalue()
        buf.close()
        filename = "%s.xlsx" % (recon.name or "reconciliation").replace("/", "_")
        return request.make_response(
            data,
            headers=[
                (
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                ("Content-Disposition", 'attachment; filename="%s"' % filename),
            ],
        )
