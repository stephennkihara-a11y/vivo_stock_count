# Vivo Stock Count

Custom Odoo 18 (Community) module implementing the Vivo Fashion Group retail
stock-count workflow specified in **SPEC-ODOO-001**.

The module wraps native `stock.quant` with a staged, controlled process built
around the rack-section dual-verification method Vivo stores already use:
one person scans, a second performs an independent physical count, and the
section is only reconciled when both totals agree.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Data model, both state machines, access rights, segregation-of-duties constraints | Complete |
| 2 | Desktop manager review interface, colour-coded section progress board | Complete |
| 3 | Mobile PWA — Scanner + Physical Counter modes, concurrent multi-scanner | Complete |
| 4 | GL posting via `stock.quant` inventory pipeline, auto-reconciliation on Apply | Complete |
| 5 | Reporting, audit log, PDF/Excel reconciliation exports, audit auto-notifications | **In review** |

## Dependencies

- Odoo 18 Community
- Python 3.10+
- PostgreSQL
- Module deps: `stock`, `barcodes`, `web`, `mail`

## Install

1. Drop the `vivo_stock_count/` folder into your Odoo `addons_path`.
2. Restart Odoo and update the apps list.
3. Install **Vivo Stock Count** from the Apps screen.

Post-install setup:

1. Open **Stock Count → Configuration → Zones** and define one zone per
   physical area of each store (display floor, backroom, fitting rooms,
   transit, damaged stock).
2. For each zone, add **Rack Templates** — one row per physical rack. These
   are cloned into real sections every time you start a session, so the
   store map is defined once and reused.
3. Open **Settings → Vivo Stock Count** and tune the approval bands and
   section idle-lock window if the defaults (5 000 / 25 000 KES, 30 min)
   don't match.
4. Map users to one of the four groups under **Settings → Users**:
   - *Counter* — scans / does physical counts
   - *Store Manager* — creates sessions, reviews, approves up to store band
   - *Regional Manager* — approves up to regional band
   - *CFOO / Internal Audit* — approves above regional band, read-only on
     reconciliation reports

## Data model

```
vivo.count.zone ─┬─< vivo.count.section.template       (store map, reused per count)
                 │
                 └─< vivo.count.section >── vivo.count.line ─< vivo.count.scan.event
                          ▲                       (per-SKU on a rack)        (append-only audit)
                          │
                vivo.count.session                                  (the count event)
                          │
                          └── vivo.count.reconciliation             (immutable, auto on Apply)
                                     │
                                     └─< vivo.count.reconciliation.line
```

## State machines

**Session:** `draft → in_progress → counted → review → approved → applied`
(cancellable from any non-applied state).

**Section:** `draft → scanning → physical_count → pending_review → reconciled`,
with a `variance_rescan → scanning` loop on every scan-vs-physical mismatch.

The full path is driven from the desktop section form:

- **Start Scanning** (`draft`/`variance_rescan` → `scanning`)
- **Finish Scanning** (`scanning` → `physical_count`)
- type the physical counter's headcount into **Physical Count**, then
  **Submit Physical Count** — on a match the section moves to
  `pending_review`; on a mismatch it loops to `variance_rescan`
- **Review & Reconcile** (`pending_review` → `reconciled`) opens an auditor
  wizard listing the counted lines. The auditor must give a variance reason
  (and a note for reason *Other*) for every counted line that differs from the
  system before confirming. Confirmation stamps `reconciled_by_id` and
  `reconciled_at` for the audit trail. Confirmation is **manager-gated**
  (`group_vivo_count_store_manager` or higher) — a plain counter can never
  reconcile a section.

**Persistent mismatch → auditor.** A scan-vs-physical mismatch loops through
`variance_rescan` for re-scanning, but it must not loop forever. After
`vivo_count.rescan_review_threshold` failed re-scans (default 1) the section
escalates to `pending_review` for the auditor instead of looping. In the
wizard the auditor sets an **authoritative physical count** (the counters
couldn't agree, so the auditor's number wins) and records a mandatory reason;
the section then reconciles even though the totals differ (`force_reconciled`,
`force_reconcile_reason`, `reconciled_by_id` all captured). This is the only
way a section with `scan_total_qty != physical_total_qty` may become
`reconciled`.

**Auto-close.** A section with no *genuine* variance skips `pending_review` and
reconciles automatically on match (no `reconciled_by_id`), controlled by the
**Auto-close zero-variance sections** setting
(`vivo_count.auto_close_zero_variance`, default on). A "genuine variance" is a
counted line with a real system baseline (`system_qty` set) that differs from
the count. Pure rack scans from the mobile PWA carry `system_qty = 0` (the
snapshot lives in the catch-all section), so they are not section-level
variances — their reconciliation against the system happens per-product at
Apply. Turn the toggle off to route **every** matched section through auditor
review.

The same transitions are exposed to the mobile PWA.

A session cannot reach `counted` (and therefore `review` / `approved` / `applied`)
while any section is unreconciled — `pending_review` counts as unreconciled,
so a section awaiting sign-off blocks the session from advancing. Enforced both
by state guards on the action methods and by a `@api.constrains` invariant that
prevents direct writes.

## Counting modes

Each session picks a `count_mode` at creation (frozen once it leaves `draft` —
you can't switch engines mid-count):

- **Full Inventory Count** (`snapshot`, default) — `action_start` pre-loads a
  line for **every** expected SKU in scope with a frozen `system_qty`. This is
  what powers the counted-vs-not-counted split and the uncounted-SKU shortage
  rollup below: an SKU that is never found anywhere is a genuine shortage.
- **Quick Count** (`scan_to_populate`) — `action_start` creates **no** lines.
  Lines are built as scans arrive: the first scan of an SKU creates its line,
  freezes `system_qty` from the current `stock.quant` on-hand at the session's
  location, and sets `counted_qty`; subsequent scans of the same SKU increment
  the existing line. An SKU scanned with **zero on-hand** is still counted but
  flagged `is_unexpected` (a likely overage / off-system item) — the scan is
  never blocked. Because there is no full snapshot, "never counted" shortage
  detection does not apply, so the uncounted-SKU rollup is reported as zero (not
  a false shortage) in this mode.

Everything downstream of the count is **mode-independent**: the scan-vs-physical
match/mismatch logic, the `variance_rescan → pending_review` auditor escalation,
the review wizard, and the force-reconcile band check all work the same in both
modes (they compare per-section scan and physical totals, not the snapshot).

## Counted vs. "not counted here" (Option 2)

Each section loads **every** SKU in scope for the store location, but a scanner
only scans the items physically present on their assigned rack. To stop the
other racks' SKUs from reading as shrinkage, every line carries a computed
`line_status`:

- **Counted** — `counted_qty > 0`. The item was scanned on this rack. These are
  the lines the counter reconciles and, where they differ from the system, must
  give a variance reason for.
- **Not counted here** — `counted_qty == 0`. A system SKU that wasn't found on
  this rack. It is treated as *pending on another rack*, **not** a variance: it
  needs no reason, does not appear in the Variance Lines tab, and never blocks
  reconcile or approval.

The section form splits these into two tabs (*Counted in this rack* /
*Not counted here*), and the scan total (hence the reconcile check) counts only
the counted subset — which is automatic, since uncounted lines contribute zero.

The discrepancy the count exists to catch is preserved at the **session** level:
an SKU counted on *no* rack of the entire session (positive system snapshot,
zero counted everywhere) is rolled up into `uncounted_sku_count` /
`uncounted_shortage_value` and listed behind the **Uncounted SKUs** button on
the session form. So a per-rack screen stays clean (≈62 counted lines, not
1 000+), while a genuine store-wide shortage is still flagged before Apply.

Line search views ship filters for **Counted**, **Not Counted Here**,
**Has Variance Reason**, and **Missing Variance Reason**.

## Segregation of duties

Two enforced rules:

1. On a section, `scanner_id != physical_counter_id`. No one verifies their
   own scan. Enforced via `_check_segregation_of_duties` on
   `vivo.count.section`.
2. A user who scanned or physically counted on any section of a session
   cannot approve that session. Enforced via `_check_counter_not_approver`
   on `vivo.count.session`.

Plus group-level: counters never reach the `action_apply` path; variance
band routing locks each approval level to the matching group.

## Mobile PWA

The counting app is an installable Progressive Web App served from Odoo
at `/vivo-count/pwa`. Counters and store managers reach it from the
**Stock Count → Mobile App** menu entry, or by typing the URL directly
into a phone browser.

To install on a counter's device:

1. Sign into Odoo on the phone (HTTPS only).
2. Open `/vivo-count/pwa` in Chrome (Android) or Safari (iOS).
3. Use the browser's "Add to Home Screen" option. The app icon installs
   as a standalone, chromeless launcher.
4. The service worker caches the shell + static assets on first load. The
   app then runs offline for up to 60 minutes of scanning, queueing all
   scans in IndexedDB with idempotency keys for deterministic replay
   when the network returns (AC #8).

Inputs supported:

- **Bluetooth HID scanner** (the most common kit on store floors). The
  barcode input is auto-focused so the scanner can fire scans without
  any extra taps.
- **Camera barcode scan** via the `BarcodeDetector` API (Chrome / Edge /
  modern Android). On unsupported browsers the camera button is
  disabled and the hardware-scanner / typed input remain available.

Concurrent multi-scanner: every section open in the PWA acquires a
soft-lock visible to other scanners on the section list as `🔒 [name]`.
A section in another user's lock window cannot be opened (AC #14).
Three scanners working three different sections of the same session
operate against independent rows, so AC #13 holds.

## Running tests

```bash
odoo -d test_db -i vivo_stock_count --test-enable --test-tags vivo_count --stop-after-init
```

Phase 1 tests cover acceptance criteria #1 (counter cannot apply), #2 (no
reconcile on mismatch), #3 (scanner ≠ physical counter), #4 (no advance
with unreconciled sections), #5 (re-scan loop and bounce-from-review
isolation), #7 (variance band routing), #10 (scan-event immutability),
#11 (variance reason required), #14 (soft-lock visibility), and #18
(reconciliation immutability).

Phase 2 tests (`test_review_ui.py`) cover reviewer auto-set, ETA + progress
computes, variance summary, approval-wizard blocker logic, bounce wizard,
and view-load smoke.

Phase 3 tests cover #6 (scan-once-type-qty), #8 (50-scan idempotent
replay), #13 (three scanners interleaved with no scan loss), and #14
reinforced at the PWA-API layer.

A separate Postgres-level concurrency probe that exercises real
multi-process row-locking is out of Phase 3 scope and lives in the QA
harness — Odoo's `TransactionCase` runs inside one transaction and cannot
exercise inter-transaction locking from within a single test process.

Phase 4 tests (`test_gl_posting.py`) cover #1 (counter still blocked
after full plumbing), #9 (stock.quant + stock.move records reflect
counted_qty), #15 (auto-reconciliation), #16 (qty/value before/after
+ variance flags including multi-section aggregation), and Risk #4
(mid-batch failure rolls back atomically).

Phase 5 tests (`test_reports.py`) cover #17 (audit notification posts
chatter + schedules activity on Apply) and #19 (PDF templates compile +
bind data; xlsxwriter produces valid output).

## Acceptance criteria coverage

| AC | Description | Covered in |
|---|---|---|
| 1 | Counter cannot post to GL | Phase 1, 4 |
| 2 | No reconcile on scan-vs-physical mismatch | Phase 1 |
| 3 | Scanner ≠ physical counter | Phase 1 |
| 4 | No session advance with unreconciled sections | Phase 1 |
| 5 | Re-scan loop isolated to one section; bounce-from-review isolation | Phase 1, 2 |
| 6 | Scan-once-then-type-qty | Phase 3 |
| 7 | Variance-band approval routing (store / regional / CFOO) | Phase 1, 2 |
| 8 | 60-min offline + deterministic sync without duplicates | Phase 3 |
| 9 | `stock.quant` + journal on Apply | Phase 4 |
| 10 | Immutable audit log of every state transition + scan | Phase 1, 3, 4 |
| 11 | Variance reasons mandatory before approval | Phase 1, 2 |
| 12 | Reports populate (Count Summary, Section Reconciliation, Variance Trend, Audit Trail) | Phase 5 |
| 13 | 3 concurrent scanners, no locks / lost scans | Phase 3 |
| 14 | Section soft-lock visible to other scanners | Phase 1, 3 |
| 15 | Reconciliation auto-generated on Apply | Phase 4, 5 |
| 16 | qty/value before/after + variance flags per barcode | Phase 4 |
| 17 | Internal Audit auto-notified on Apply | Phase 5 |
| 18 | Reconciliation immutable | Phase 1 |
| 19 | PDF + Excel exports preserve variance highlighting | Phase 5 |

## Open / deferred

- Sample data fixtures (3 zones, ~12 racks, 50 SKUs, one session per
  state) land in Phase 5 alongside the demo seed.
- `per_sku` physical count mode is a configuration switch but only
  `per_section` is implemented in v1, matching today's Excel sheet.

## Spec

The source-of-truth specification is **SPEC-ODOO-001, v1.0** (May 2026).
All design choices in this module trace back to a section, requirement, or
acceptance criterion in that document.
