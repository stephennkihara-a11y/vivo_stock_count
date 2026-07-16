from odoo import api, fields, models, tools


# Sections whose data counts as "submitted for review". Draft/scanning and
# 'excluded' racks are NOT merged; legacy 'physical_review' is treated as
# submitted (same bucket as pending_review).
SUBMITTED_STATES = ("pending_review", "reconciled", "physical_review")


class VivoCountReconReport(models.Model):
    """Read-only session-level reconciliation report.

    Grain = (session x product). CRITICAL aggregation rule — counted depends on
    submission, the expected baseline does NOT:

    - counted_qty  = SUM(counted_qty) over SUBMITTED sections only
                     (pending_review / reconciled / legacy physical_review) —
                     the merged physical/scanned total.
    - system_qty   = MAX(system_qty) over ALL of the session's sections (every
                     state) — the product's frozen baseline, TAKEN ONCE. It must
                     reflect true expected stock regardless of which racks have
                     been submitted; sourcing it from submitted racks only would
                     read 0 when the baseline (catch-all) rack isn't submitted,
                     making counted items look like surplus. Never summed (a SKU
                     counted in N racks would otherwise show N x its true stock).
    - variance     = (submitted counted_qty) - (full-session system_qty).
    - price        = product.list_price — once per product.

    Rows are driven by the full baseline (all sections), so a SKU in the baseline
    that was never counted in any submitted rack surfaces as Not Counted
    (counted_qty 0) — a genuine session-level shortage candidate.

    Backed by a PostgreSQL view (``_auto = False``); strictly read-only.
    """

    _name = "vivo.count.recon.report"
    _description = "Vivo Count Recon Report (session x product)"
    _auto = False
    _order = "session_id, product_id"

    session_id = fields.Many2one("vivo.count.session", string="Session", readonly=True)
    product_id = fields.Many2one("product.product", string="SKU", readonly=True)
    product_title = fields.Char(
        string="Product Title",
        compute="_compute_product_title",
        search="_search_product_title",
        readonly=True,
    )
    is_unknown = fields.Boolean(string="Unknown / Not in System", readonly=True)
    scanned_barcode = fields.Char(string="Scanned Barcode", readonly=True)
    barcode = fields.Char(string="Barcode", readonly=True)
    system_qty = fields.Float(
        string="System Qty", readonly=True, digits="Product Unit of Measure"
    )
    counted_qty = fields.Float(
        string="Counted Qty", readonly=True, digits="Product Unit of Measure"
    )
    variance = fields.Float(
        string="Variance", readonly=True, digits="Product Unit of Measure"
    )
    price = fields.Float(string="Price", readonly=True, digits="Product Price")
    cost = fields.Float(string="Cost", readonly=True, digits="Product Price")
    total_retail_price = fields.Float(
        string="Total Retail Price", readonly=True, digits="Product Price"
    )
    total_cost_price = fields.Float(
        string="Total Cost Price", readonly=True, digits="Product Price"
    )
    counted = fields.Boolean(string="Counted", readonly=True)
    rack_count = fields.Integer(string="Racks Counted", readonly=True)

    # Per-rack drill-down: the underlying counted lines for this (session,
    # product) across submitted racks. Computed, non-stored, read-only.
    line_ids = fields.One2many(
        "vivo.count.line",
        compute="_compute_line_ids",
        string="Per-Rack Breakdown",
    )
    # Same information as a one-line summary, e.g. "Rack A: 2, Rack B: 4".
    rack_breakdown = fields.Char(
        string="Per-Rack Breakdown",
        compute="_compute_rack_breakdown",
    )

    def _compute_product_title(self):
        for rec in self:
            rec.product_title = rec.product_id.name if rec.product_id else "Unknown"

    def _search_product_title(self, operator, value):
        # Search delegates to the real product name; the literal "Unknown"
        # rows have no product and are reached via the dedicated filter.
        return [("product_id.name", operator, value)]

    def _compute_line_ids(self):
        Line = self.env["vivo.count.line"]
        for rec in self:
            if not rec.session_id:
                rec.line_ids = Line.browse()
                continue
            if rec.is_unknown:
                # Unknown rows aggregate product-less lines by scanned barcode.
                if not rec.scanned_barcode:
                    rec.line_ids = Line.browse()
                    continue
                rec.line_ids = Line.search(
                    [
                        ("section_id.session_id", "=", rec.session_id.id),
                        ("is_unknown", "=", True),
                        ("scanned_barcode", "=", rec.scanned_barcode),
                        ("section_id.state", "in", list(SUBMITTED_STATES)),
                        ("counted_qty", ">", 0),
                    ]
                )
                continue
            if not rec.product_id:
                rec.line_ids = Line.browse()
                continue
            rec.line_ids = Line.search(
                [
                    ("section_id.session_id", "=", rec.session_id.id),
                    ("product_id", "=", rec.product_id.id),
                    ("section_id.state", "in", list(SUBMITTED_STATES)),
                    ("counted_qty", ">", 0),
                ]
            )

    @api.depends("line_ids", "line_ids.counted_qty", "line_ids.section_id.name")
    def _compute_rack_breakdown(self):
        for rec in self:
            lines = rec.line_ids.filtered(lambda l: (l.counted_qty or 0.0) > 0)
            # Stable order: section sequence, then name.
            lines = lines.sorted(
                key=lambda l: (l.section_id.sequence, l.section_id.name or "")
            )
            parts = []
            for line in lines:
                qty = line.counted_qty or 0.0
                qty_str = str(int(qty)) if float(qty).is_integer() else "%.1f" % qty
                parts.append("%s: %s" % (line.section_id.name or "?", qty_str))
            rec.rack_breakdown = ", ".join(parts) or "-"

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            """
            CREATE VIEW %s AS (
                WITH base AS (
                    -- Baseline over ALL of the session's sections (every state):
                    -- the expected qty must not depend on which racks are
                    -- submitted. One row per (session, product). Unknown
                    -- (product-less) lines are excluded here and handled by the
                    -- dedicated UNION branch below.
                    SELECT
                        sec.session_id       AS session_id,
                        l.product_id         AS product_id,
                        MAX(l.system_qty)    AS system_qty,
                        MAX(l.unit_cost)     AS cost,
                        MIN(l.id)            AS id
                    FROM vivo_count_line l
                    JOIN vivo_count_section sec ON sec.id = l.section_id
                    WHERE l.product_id IS NOT NULL
                    GROUP BY sec.session_id, l.product_id
                ),
                counted AS (
                    -- Merged physical count over SUBMITTED sections only.
                    SELECT
                        sec.session_id                             AS session_id,
                        l.product_id                               AS product_id,
                        SUM(l.counted_qty)                         AS counted_qty,
                        bool_or(l.counted_qty > 0)                 AS counted,
                        COUNT(*) FILTER (WHERE l.counted_qty > 0)  AS rack_count
                    FROM vivo_count_line l
                    JOIN vivo_count_section sec ON sec.id = l.section_id
                    WHERE l.product_id IS NOT NULL
                      AND sec.state IN ('pending_review', 'reconciled', 'physical_review')
                    GROUP BY sec.session_id, l.product_id
                )
                SELECT
                    b.id                                            AS id,
                    b.session_id                                    AS session_id,
                    b.product_id                                    AS product_id,
                    false                                           AS is_unknown,
                    NULL::varchar                                   AS scanned_barcode,
                    pp.barcode                                      AS barcode,
                    b.system_qty                                    AS system_qty,
                    COALESCE(c.counted_qty, 0.0)                    AS counted_qty,
                    COALESCE(c.counted_qty, 0.0) - b.system_qty     AS variance,
                    pt.list_price                                   AS price,
                    b.cost                                          AS cost,
                    (COALESCE(c.counted_qty, 0.0) - b.system_qty) * pt.list_price
                                                                    AS total_retail_price,
                    (COALESCE(c.counted_qty, 0.0) - b.system_qty) * b.cost
                                                                    AS total_cost_price,
                    COALESCE(c.counted, false)                      AS counted,
                    COALESCE(c.rack_count, 0)                       AS rack_count
                FROM base b
                JOIN product_product pp ON pp.id = b.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                LEFT JOIN counted c
                    ON c.session_id = b.session_id
                    AND c.product_id = b.product_id

                UNION ALL

                -- Unknown captures: product-less lines grouped by
                -- (session, scanned_barcode) over SUBMITTED sections only.
                -- system_qty is 0 (never expected), so the whole counted qty
                -- reads as a positive surplus variance. MIN(l.id) is taken over
                -- a disjoint set of lines (all product-less), so it can never
                -- collide with a known-branch id.
                SELECT
                    MIN(l.id)                                       AS id,
                    sec.session_id                                  AS session_id,
                    NULL::integer                                   AS product_id,
                    true                                            AS is_unknown,
                    l.scanned_barcode                               AS scanned_barcode,
                    l.scanned_barcode                               AS barcode,
                    0.0                                             AS system_qty,
                    SUM(l.counted_qty)                              AS counted_qty,
                    SUM(l.counted_qty)                              AS variance,
                    0.0                                             AS price,
                    0.0                                             AS cost,
                    0.0                                             AS total_retail_price,
                    0.0                                             AS total_cost_price,
                    bool_or(l.counted_qty > 0)                      AS counted,
                    COUNT(*) FILTER (WHERE l.counted_qty > 0)       AS rack_count
                FROM vivo_count_line l
                JOIN vivo_count_section sec ON sec.id = l.section_id
                WHERE l.is_unknown = true
                  AND l.scanned_barcode IS NOT NULL
                  AND sec.state IN ('pending_review', 'reconciled', 'physical_review')
                GROUP BY sec.session_id, l.scanned_barcode
            )
            """
            % self._table
        )
