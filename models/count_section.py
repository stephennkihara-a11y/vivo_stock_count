from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


SECTION_STATES = [
    ("draft", "Draft"),
    ("scanning", "Scanning"),
    ("physical_count", "Physical Count"),
    ("variance_rescan", "Variance Re-scan"),
    ("reconciled", "Reconciled"),
]


class VivoCountSection(models.Model):
    _name = "vivo.count.section"
    _description = "Vivo Count Section (Rack)"
    _order = "session_id, zone_id, sequence, name"

    name = fields.Char(required=True)
    session_id = fields.Many2one(
        "vivo.count.session",
        required=True,
        ondelete="cascade",
        index=True,
    )
    zone_id = fields.Many2one("vivo.count.zone", required=True)
    template_id = fields.Many2one("vivo.count.section.template", ondelete="set null")
    sequence = fields.Integer(default=10)

    state = fields.Selection(SECTION_STATES, default="draft", required=True, copy=False)

    scanner_id = fields.Many2one("res.users", string="Scanner")
    physical_counter_id = fields.Many2one("res.users", string="Physical Counter")

    line_ids = fields.One2many("vivo.count.line", "section_id", string="SKU Lines")

    scan_total_qty = fields.Float(
        compute="_compute_totals", store=True, digits="Product Unit of Measure",
    )
    physical_total_qty = fields.Float(string="Physical Count", digits="Product Unit of Measure")
    is_reconciled = fields.Boolean(compute="_compute_is_reconciled", store=True)

    rescan_count = fields.Integer(default=0, readonly=True, copy=False)
    reconciled_at = fields.Datetime(readonly=True, copy=False)

    # Soft-lock: the scanner currently working this section. Released after
    # `vivo_count.section_lock_minutes` of inactivity.
    locked_by_id = fields.Many2one("res.users", string="In Progress — User", copy=False)
    locked_at = fields.Datetime(copy=False)

    has_unresolved_no_barcode = fields.Boolean(compute="_compute_no_barcode")

    _sql_constraints = [
        (
            "rescan_non_negative",
            "CHECK (rescan_count >= 0)",
            "Rescan count cannot be negative.",
        ),
    ]

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends("line_ids.counted_qty")
    def _compute_totals(self):
        for section in self:
            section.scan_total_qty = sum(section.line_ids.mapped("counted_qty"))

    @api.depends("scan_total_qty", "physical_total_qty", "state")
    def _compute_is_reconciled(self):
        for section in self:
            section.is_reconciled = (
                section.state == "reconciled"
                and section.scan_total_qty == section.physical_total_qty
            )

    @api.depends("line_ids.no_barcode_flag", "line_ids.no_barcode_resolved")
    def _compute_no_barcode(self):
        for section in self:
            section.has_unresolved_no_barcode = any(
                l.no_barcode_flag and not l.no_barcode_resolved
                for l in section.line_ids
            )

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def action_start_scanning(self):
        """draft -> scanning (or variance_rescan -> scanning)."""
        for section in self:
            if section.state not in {"draft", "variance_rescan"}:
                raise UserError(
                    _("Section %s is in state %s; cannot start scanning.")
                    % (section.name, section.state)
                )
            if not section.scanner_id:
                section.scanner_id = self.env.user
            section.write(
                {
                    "state": "scanning",
                    "locked_by_id": self.env.user.id,
                    "locked_at": fields.Datetime.now(),
                }
            )
        return True

    def action_finish_scanning(self):
        """scanning -> physical_count."""
        for section in self:
            if section.state != "scanning":
                raise UserError(_("Section %s is not in scanning state.") % section.name)
            if section.has_unresolved_no_barcode:
                raise UserError(
                    _("Section %s has unresolved 'no barcode' lines.") % section.name
                )
            section.write({"state": "physical_count", "locked_by_id": False, "locked_at": False})
        return True

    def action_submit_physical_count(self, physical_qty=None):
        """physical_count -> reconciled (if match) | variance_rescan (if not).

        Required-different-user enforced by `_check_segregation_of_duties`.
        """
        for section in self:
            if section.state != "physical_count":
                raise UserError(
                    _("Section %s is not awaiting physical count.") % section.name
                )
            if not section.physical_counter_id:
                section.physical_counter_id = self.env.user
            if physical_qty is not None:
                section.physical_total_qty = physical_qty
            if section.scan_total_qty == section.physical_total_qty:
                section.write(
                    {
                        "state": "reconciled",
                        "reconciled_at": fields.Datetime.now(),
                    }
                )
            else:
                section.write({"state": "variance_rescan", "rescan_count": section.rescan_count + 1})
        return True

    def _bounce_from_review(self):
        """Manager-driven bounce from review back to scanning.

        Per A3: rescan_count increments, counted_qty wiped on all lines,
        but scan history is preserved on `vivo.count.scan.event`. Manager
        may reassign scanner/physical_counter afterwards.
        """
        for section in self:
            section.line_ids.write({"counted_qty": 0.0, "variance_reason": False, "variance_note": False})
            section.write(
                {
                    "state": "scanning",
                    "physical_total_qty": 0.0,
                    "rescan_count": section.rescan_count + 1,
                    "reconciled_at": False,
                    "locked_by_id": False,
                    "locked_at": False,
                }
            )
        return True

    # ------------------------------------------------------------------
    # Soft locking (Phase 1 minimum; PWA enforcement in Phase 3)
    # ------------------------------------------------------------------
    def acquire_lock(self):
        """Attempt to soft-lock this section to the current user (AC #14)."""
        self.ensure_one()
        Param = self.env["ir.config_parameter"].sudo()
        lock_minutes = int(Param.get_param("vivo_count.section_lock_minutes", "30"))
        from datetime import timedelta

        now = fields.Datetime.now()
        if self.locked_by_id and self.locked_by_id != self.env.user:
            if self.locked_at and (now - self.locked_at) < timedelta(minutes=lock_minutes):
                raise UserError(
                    _("Section %(name)s is in progress with %(user)s.")
                    % {"name": self.name, "user": self.locked_by_id.name}
                )
        self.write({"locked_by_id": self.env.user.id, "locked_at": now})
        return True

    def release_lock(self):
        self.write({"locked_by_id": False, "locked_at": False})
        return True

    # ------------------------------------------------------------------
    # Constraints — segregation of duties (AC #3)
    # ------------------------------------------------------------------
    @api.constrains("scanner_id", "physical_counter_id")
    def _check_segregation_of_duties(self):
        for section in self:
            if (
                section.scanner_id
                and section.physical_counter_id
                and section.scanner_id == section.physical_counter_id
            ):
                raise ValidationError(
                    _(
                        "Section %s: the scanner and the physical counter must be "
                        "two different users. No one can verify their own scan."
                    )
                    % section.name
                )

    @api.constrains("state", "scan_total_qty", "physical_total_qty")
    def _check_reconcile_match(self):
        """AC #2: cannot land in `reconciled` unless scan == physical."""
        for section in self:
            if section.state == "reconciled" and section.scan_total_qty != section.physical_total_qty:
                raise ValidationError(
                    _(
                        "Section %s cannot be reconciled — scan total %s does not "
                        "match physical count %s."
                    )
                    % (section.name, section.scan_total_qty, section.physical_total_qty)
                )
