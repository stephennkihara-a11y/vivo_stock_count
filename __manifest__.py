{
    "name": "Vivo Stock Count",
    "version": "18.0.1.8.0",
    "summary": "Retail stock count workflow for Vivo Fashion Group — rack-section dual-verification, staged approval, auto-reconciliation",
    "description": """
Vivo Stock Count (SPEC-ODOO-001)
================================

Custom retail-fit stock count workflow that wraps native stock.quant with a
staged, controlled process built around how Vivo stores actually count:
rack by rack, with one person scanning and a second doing an independent
physical count before anything posts to the GL.

Phase 1 scope:
  - Data model (session, section, line, zone, section template, scan event,
    reconciliation, reconciliation line)
  - Session and section state machines
  - Access rights (4 groups) and record-rule store scoping
  - Segregation-of-duties Python constraints
""",
    "author": "Vivo Fashion Group",
    "website": "https://vivofashiongroup.com",
    "category": "Inventory/Inventory",
    "license": "LGPL-3",
    "depends": [
        "stock",
        "barcodes",
        "web",
        "mail",
    ],
    "data": [
        "security/vivo_count_security.xml",
        "security/ir.model.access.csv",
        "data/sequences.xml",
        "data/default_config.xml",
        "data/cron_jobs.xml",
        "wizard/wizard_views.xml",
        "views/count_zone_views.xml",
        "views/count_section_template_views.xml",
        "views/count_session_views.xml",
        "views/count_section_views.xml",
        "views/count_line_views.xml",
        "views/scan_event_views.xml",
        "views/reconciliation_views.xml",
        "views/res_config_settings_views.xml",
        "views/menu_views.xml",
        "views/pwa_menu.xml",
        "reports/reconciliation_report.xml",
        "reports/count_summary_report.xml",
        "reports/section_reconciliation_report.xml",
    ],
    "demo": [
        "demo/demo_data.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "vivo_stock_count/static/src/scss/section_progress_board.scss",
        ],
    },
    "application": True,
    "installable": True,
}
