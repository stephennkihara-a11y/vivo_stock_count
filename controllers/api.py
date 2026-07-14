"""JSON API for the Vivo Stock Count PWA.

All endpoints under /vivo-count/api/* expect a logged-in Odoo session and
return JSON. The frontend (static/pwa/) talks to these endpoints.

Concurrency contract (AC #13): every endpoint is short, transactional, and
operates on row-level data — sections and lines are independent rows, so
three scanners on three different sections never contend at the row level.
The session-level computed totals are read-only computes; they do not block
writers.

Idempotency contract (AC #8): mutating endpoints accept an
`idempotency_key`. When the key matches a previous submission, the
operation is a no-op and the current server state is returned. The PWA
generates a UUIDv4 per scan event so an offline → replay drain produces
exactly-once semantics.
"""
import logging

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _user_role(user):
    if user.has_group("vivo_stock_count.group_vivo_count_cfoo_audit"):
        return "cfoo_audit"
    if user.has_group("vivo_stock_count.group_vivo_count_regional"):
        return "regional"
    if user.has_group("vivo_stock_count.group_vivo_count_store_manager"):
        return "store_manager"
    if user.has_group("vivo_stock_count.group_vivo_count_counter"):
        return "counter"
    return None


class VivoCountAPI(http.Controller):

    # ------------------------------------------------------------------
    # Session / user
    # ------------------------------------------------------------------
    @http.route(
        "/vivo-count/api/me", auth="user", type="json", methods=["POST"]
    )
    def me(self):
        user = request.env.user
        return {
            "id": user.id,
            "name": user.name,
            "login": user.login,
            "role": _user_role(user),
            "company_id": user.company_id.id,
            "company_name": user.company_id.name,
            "currency_id": user.company_id.currency_id.id,
            "currency_name": user.company_id.currency_id.name,
        }

    # ------------------------------------------------------------------
    # Pickers
    # ------------------------------------------------------------------
    @http.route(
        "/vivo-count/api/stores", auth="user", type="json", methods=["POST"]
    )
    def stores(self):
        """Stores the user has at least one open session in."""
        sessions = request.env["vivo.count.session"].search(
            [("state", "in", ["draft", "in_progress", "counted", "review"])]
        )
        locations = sessions.mapped("location_id")
        return [
            {
                "id": loc.id,
                "name": loc.complete_name or loc.display_name,
            }
            for loc in locations
        ]

    @http.route(
        "/vivo-count/api/sessions", auth="user", type="json", methods=["POST"]
    )
    def sessions(self, location_id):
        sessions = request.env["vivo.count.session"].search(
            [
                ("location_id", "=", location_id),
                ("state", "in", ["in_progress", "counted", "review"]),
            ],
            order="scheduled_date desc",
        )
        return [
            {
                "id": s.id,
                "name": s.name,
                "state": s.state,
                "sections_total": s.sections_total,
                "sections_reconciled": s.sections_reconciled,
                "zone_name": s.zone_id.name or "Whole store",
            }
            for s in sessions
        ]

    @http.route(
        "/vivo-count/api/sections", auth="user", type="json", methods=["POST"]
    )
    def sections(self, session_id):
        return request.env["vivo.count.section"].list_for_pwa(session_id)

    # ------------------------------------------------------------------
    # Barcode lookup
    # ------------------------------------------------------------------
    @http.route(
        "/vivo-count/api/lookup_barcode",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def lookup_barcode(self, barcode, session_id=None):
        product = request.env["product.product"].search(
            [("barcode", "=", barcode)], limit=1
        )
        if not product:
            return {"found": False}
        existing_qty = None
        if session_id:
            line = request.env["vivo.count.line"].search(
                [
                    ("session_id", "=", session_id),
                    ("product_id", "=", product.id),
                ],
                limit=1,
            )
            existing_qty = line.system_qty if line else None
        return {
            "found": True,
            "product_id": product.id,
            "name": product.display_name,
            "barcode": product.barcode,
            "system_qty": existing_qty,
        }

    # ------------------------------------------------------------------
    # Scanner mode
    # ------------------------------------------------------------------
    @http.route(
        "/vivo-count/api/section/open",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def section_open(self, section_id):
        section = request.env["vivo.count.section"].browse(section_id)
        try:
            return section.open_for_scanning()
        except UserError as e:
            return {"error": str(e)}

    @http.route(
        "/vivo-count/api/section/finish_scanning",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def section_finish(self, section_id, force=False):
        section = request.env["vivo.count.section"].browse(section_id)
        try:
            return section.finish_scanning_pwa(force=force)
        except UserError as e:
            return {"error": str(e)}

    @http.route(
        "/vivo-count/api/section/reject_recount",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def section_reject_recount(self, section_id):
        """Wipe every scanned line on the rack and send it back to scanning for
        a full rescan. The pre-reconcile guard + audit trail live in the section
        method, so this route cannot bypass them."""
        section = request.env["vivo.count.section"].browse(section_id).exists()
        if not section:
            return {"error": "Section not found."}
        try:
            return section.reject_and_recount_pwa()
        except (UserError, ValidationError) as e:
            return {"error": str(e)}
        except AccessError as e:
            return {"error": str(e), "access_denied": True}

    @http.route(
        "/vivo-count/api/section/release_lock",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def section_release(self, section_id):
        request.env["vivo.count.section"].browse(section_id).release_lock()
        return {"ok": True}

    @http.route(
        "/vivo-count/api/scan", auth="user", type="json", methods=["POST"]
    )
    def scan(
        self,
        section_id,
        product_id=None,
        scanned_qty=1,
        idempotency_key=None,
        device_id=None,
        scanned_barcode=None,
    ):
        try:
            return request.env["vivo.count.line"].record_scan(
                section_id=section_id,
                product_id=product_id,
                scanned_qty=scanned_qty,
                idempotency_key=idempotency_key,
                device_id=device_id,
                scanned_barcode=scanned_barcode,
            )
        except (UserError, ValidationError) as e:
            return {"error": str(e)}
        except AccessError as e:
            return {"error": str(e), "access_denied": True}

    @http.route(
        "/vivo-count/api/section/lines",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def section_lines(self, section_id):
        lines = request.env["vivo.count.line"].search(
            [("section_id", "=", section_id)]
        )
        return [
            {
                "id": l.id,
                "product_id": l.product_id.id,
                "product_name": l.product_id.display_name if l.product_id else "Unknown",
                "product_title": l.product_title,
                "barcode": l.product_id.barcode if l.product_id else False,
                "scanned_barcode": l.scanned_barcode,
                "is_unknown": l.is_unknown,
                "system_qty": l.system_qty,
                "counted_qty": l.counted_qty,
                "scan_count": l.scan_count,
                "no_barcode_flag": l.no_barcode_flag,
            }
            for l in lines
        ]

    @http.route(
        "/vivo-count/api/line/delete",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def line_delete(self, line_id):
        """Remove a scanned line (double-scan / wrong rack). The scanning-state
        guard lives in vivo.count.line.unlink(), so this route cannot bypass it."""
        line = request.env["vivo.count.line"].browse(line_id).exists()
        if not line:
            return {"error": "Line not found."}
        try:
            return line.action_delete_scan_line()
        except (UserError, ValidationError) as e:
            return {"error": str(e)}
        except AccessError as e:
            return {"error": str(e), "access_denied": True}

    # ------------------------------------------------------------------
    # Physical counter mode
    # ------------------------------------------------------------------
    @http.route(
        "/vivo-count/api/section/submit_physical",
        auth="user",
        type="json",
        methods=["POST"],
    )
    def section_submit_physical(
        self, section_id, physical_qty, idempotency_key=None
    ):
        section = request.env["vivo.count.section"].browse(section_id)
        try:
            return section.submit_physical_pwa(
                physical_qty=physical_qty, idempotency_key=idempotency_key
            )
        except (UserError, ValidationError) as e:
            return {"error": str(e)}
