"""Populate the counted/uncounted split fields introduced in 18.0.1.1.0.

Odoo materialises the new stored computed columns on upgrade but does not
always trigger a recompute for pre-existing rows in every deployment, so we
force one here. This is idempotent — the computes are pure functions of
existing data.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})

    lines = env["vivo.count.line"].search([])
    if lines:
        lines._compute_line_status()
        lines.flush_recordset(["line_status"])

    sections = env["vivo.count.section"].search([])
    if sections:
        sections._compute_line_counts()
        sections.flush_recordset(["counted_line_count", "not_counted_line_count"])

    sessions = env["vivo.count.session"].search([])
    if sessions:
        sessions._compute_uncounted_rollup()
        sessions.flush_recordset(["uncounted_sku_count", "uncounted_shortage_value"])
