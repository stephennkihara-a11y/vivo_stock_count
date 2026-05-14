from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    vivo_count_store_band_kes = fields.Float(
        string="Store Manager band (KES)",
        config_parameter="vivo_count.store_band_kes",
        default=5000.0,
    )
    vivo_count_regional_band_kes = fields.Float(
        string="Regional Manager band (KES)",
        config_parameter="vivo_count.regional_band_kes",
        default=25000.0,
    )
    vivo_count_physical_count_mode = fields.Selection(
        [("per_section", "Per Section"), ("per_sku", "Per SKU")],
        string="Physical count mode",
        config_parameter="vivo_count.physical_count_mode",
        default="per_section",
        help="Phase 1 only implements per_section (matches today's Excel sheet).",
    )
    vivo_count_section_lock_minutes = fields.Integer(
        string="Section idle lock minutes",
        config_parameter="vivo_count.section_lock_minutes",
        default=30,
    )
    vivo_count_trading_deadline = fields.Char(
        string="Store trading deadline",
        config_parameter="vivo_count.trading_deadline",
        default="09:30",
    )
    vivo_count_audit_notify_group_id = fields.Many2one(
        "res.groups",
        string="Audit notification group",
        config_parameter="vivo_count.audit_notify_group_id",
    )
    vivo_count_fx_source = fields.Char(
        string="FX source",
        config_parameter="vivo_count.fx_source",
        default="finance_bulletin",
    )
