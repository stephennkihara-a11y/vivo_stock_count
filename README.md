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
| 1 | Data model, both state machines, access rights, segregation-of-duties constraints | **In review** |
| 2 | Desktop manager review interface, colour-coded section progress board | Pending |
| 3 | Mobile PWA — Scanner + Physical Counter modes, concurrent multi-scanner | Pending |
| 4 | GL posting via `stock.quant._update_available_quantity()`, auto-reconciliation on Apply | Pending |
| 5 | Reporting, audit log, PDF/Excel reconciliation exports, audit auto-notifications | Pending |

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

**Section:** `draft → scanning → physical_count → reconciled`, with a
`variance_rescan → scanning` loop on every scan-vs-physical mismatch.

A session cannot reach `counted` (and therefore `review` / `approved` / `applied`)
while any section is unreconciled — enforced both by state guards on the action
methods and by a `@api.constrains` invariant that prevents direct writes.

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

## Open / deferred

- Sample data fixtures (3 zones, ~12 racks, 50 SKUs, one session per
  state) land in Phase 5 alongside the demo seed.
- `per_sku` physical count mode is a configuration switch but only
  `per_section` is implemented in v1, matching today's Excel sheet.

## Spec

The source-of-truth specification is **SPEC-ODOO-001, v1.0** (May 2026).
All design choices in this module trace back to a section, requirement, or
acceptance criterion in that document.
