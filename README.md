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
| 3 | Mobile PWA — Scanner + Physical Counter modes, concurrent multi-scanner | **In review** |
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

## Open / deferred

- Sample data fixtures (3 zones, ~12 racks, 50 SKUs, one session per
  state) land in Phase 5 alongside the demo seed.
- `per_sku` physical count mode is a configuration switch but only
  `per_section` is implemented in v1, matching today's Excel sheet.

## Spec

The source-of-truth specification is **SPEC-ODOO-001, v1.0** (May 2026).
All design choices in this module trace back to a section, requirement, or
acceptance criterion in that document.
