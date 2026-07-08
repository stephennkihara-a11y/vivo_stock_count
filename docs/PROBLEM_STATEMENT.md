# Vivo Stock Count — Problem Statement

**Project:** `vivo_stock_count` (Odoo 18 Community module)
**Owner:** Vivo Fashion Group
**Reference spec:** SPEC-ODOO-001
**Status:** problem definition — the "why" behind the module

---

## 1. Purpose of this document

This document describes the **problem** Vivo Fashion Group is trying to solve
with a controlled stock-count workflow — not the solution. It exists so that
everyone (store staff, managers, finance/audit, and developers) shares one
understanding of what is broken today, what a good outcome looks like, and
which constraints are non-negotiable. The implemented features trace back to
the problems stated here.

---

## 2. Context

Vivo runs retail fashion stores that carry a large, fast-moving SKU catalogue
(styles × colours × sizes). Accurate stock is the backbone of the business: it
drives replenishment, prevents overselling, protects margin, and feeds the
general ledger. Stock is counted **rack by rack** on the shop floor and in the
backroom, typically before the store opens for trading, and the counted numbers
must eventually be posted to inventory and the accounts.

A single store count involves:

- **Hundreds to thousands of SKUs** in scope for a location.
- **Multiple physical areas** (display floor, backroom, fitting rooms, transit,
  damaged stock) and many **racks** within them.
- **Several people** counting concurrently under time pressure.
- **Money at stake**: every discrepancy is either shrinkage to investigate or a
  correction that will hit the GL.

The count therefore is not just a data-entry task — it is a **financial control**.

---

## 3. How counting works today (the status quo)

Counting is done with spreadsheets and manual coordination:

- Someone exports or lists the expected SKUs, prints or shares a sheet, and
  people write down what they find rack by rack.
- A second person may re-count, but agreement between the two is informal.
- Totals are reconciled by hand; variances are eyeballed.
- Someone keys the final numbers into the system and adjusts inventory.

This "Excel + goodwill" process is what the module is designed to replace.

---

## 4. Why the status quo fails — the core problems

### 4.1 Shrinkage and errors go undetected or unexplained
Variances (theft, miscount, mis-scan, supplier shorts, damage, in-transit) are
the entire reason the count exists. On a spreadsheet they are easy to miss,
easy to "adjust away," and rarely carry a recorded reason. The business cannot
tell a genuine loss from a counting mistake, and cannot trend shrinkage over
time or across stores.

### 4.2 No enforced control or segregation of duties
Nothing stops one person from scanning, "verifying" their own scan, approving
it, and posting it. There is no independent second count, no separation between
whoever counted and whoever signs off, and no ceiling on who can approve a
large adjustment. A single individual can move stock value on and off the books
unchecked.

### 4.3 Speed vs. the trading deadline
Counts must finish before the store opens (a hard **trading deadline**, e.g.
09:30). With thousands of SKUs and manual coordination, teams cannot tell mid-
count whether they are on track, and a slow or blocked count either delays
trading or gets rushed and inaccurate.

### 4.4 No audit trail or accountability
When a number is questioned weeks later, there is no record of **who** scanned,
**who** counted, **who** approved, **what** the system said before, **what** was
counted, and **why** any difference was accepted. Finance and internal audit
have nothing durable to rely on.

### 4.5 Inventory and GL correctness
The final numbers must post through proper inventory moves and valuation so the
ledger stays correct. Ad-hoc adjustments bypass valuation and create
reconciliation headaches for finance.

### 4.6 Concurrency and coordination
Several people counting the same session at once must not overwrite each other,
double-count a rack, or leave a rack unclaimed. The spreadsheet model has no
notion of "this rack is being counted by someone right now."

---

## 5. Problem statement (concise)

> Vivo needs to count store inventory **quickly, concurrently, and before the
> trading deadline**, while **guaranteeing** that:
> 1. every genuine discrepancy is surfaced, explained, and reviewed by an
>    appropriately authorised person;
> 2. no single person can both count and unilaterally post the result;
> 3. the final quantities post correctly to inventory and the GL; and
> 4. a complete, immutable audit trail records who did what, and why —
>
> replacing today's spreadsheet-and-goodwill process, which does none of these
> reliably.

---

## 6. Stakeholders and what each needs

| Role | Needs |
|------|-------|
| **Counter (scanner / physical counter)** | A fast, forgiving way to count a rack on a phone; to not be blamed for items that live on another rack; to hand disagreements upward instead of being stuck. |
| **Store Manager** | Live view of progress vs. the deadline; a clean queue of only the racks that genuinely need a decision; authority to sign off routine variances. |
| **Regional Manager / CFOO / Internal Audit** | Assurance that large adjustments are escalated to the right authority band; a durable, tamper-evident record; shrinkage visibility across stores. |
| **Finance** | Correct inventory and journal postings; per-SKU before/after with reasons. |

---

## 7. Specific problems this surfaced (and had to be solved)

As the workflow was built against real store behaviour, several concrete
problems became explicit. They are recorded here because each one is a facet of
the core problem above.

### 7.1 "Everything looks like a loss"
A section loads *all* the store's SKUs, but a counter only scans the items
physically on **their** rack. Treating every un-scanned SKU as a negative
variance floods the screen with thousands of false shortages and demands a
reason for each — burying the handful of real discrepancies. **The count must
distinguish "not on this rack" (pending elsewhere) from "genuinely missing
everywhere" (a real shortage).**

### 7.2 Reconciliation must be a judgement, not an accident
A section that simply auto-closes the instant two totals happen to match treats
a real variance the same as a clean count. **A genuine variance must be held for
an authorised person to review and accept, with a recorded reason** — while
clean, zero-variance racks should not waste anyone's time.

### 7.3 A persistent disagreement had no way out
When the scanner's total and the independent physical count keep disagreeing,
re-scanning forever is not a resolution. **A persistent two-counter
disagreement must escalate to an auditor** who can set an authoritative figure
and record why — the process must always terminate, never loop indefinitely and
never silently auto-resolve.

### 7.4 Overriding the two-counter check is a privilege, not a default
The ability to reconcile a section where the counts still differ is powerful —
it overrides the core two-counter integrity guarantee. **That override must be
restricted to manager/auditor authority and impossible for a plain counter to
reach**, through the UI or by any back-door data write.

### 7.5 One counting method does not fit every situation
A full, controlled inventory count (pre-load every expected SKU, detect
shortages) is right for a periodic wall-to-wall count. But a fast **quick count**
— walk up, scan what's there, build the list as you go — is what staff want for
spot checks. **The system must support both, chosen per session**, without one
mode's assumptions breaking the other.

---

## 8. Requirements any solution must satisfy

These are the non-negotiables the problem imposes:

- **R1 — Real variances only.** Surface genuine discrepancies; never present
  "not counted here yet" as shrinkage, and never lose a truly-missing SKU.
- **R2 — Independent dual verification.** One person scans; a second performs an
  independent physical count. A section is not settled until they agree *or* an
  authorised person adjudicates.
- **R3 — Segregation of duties.** The scanner and the physical counter must be
  different people; anyone who counted a session cannot approve it; counters can
  never post to the GL.
- **R4 — Authority-banded sign-off.** The larger the variance value, the higher
  the approval authority required (store → regional → CFOO/audit thresholds in
  KES).
- **R5 — Always terminates.** No state loops forever; persistent disagreement
  escalates to a human decision.
- **R6 — Complete, immutable audit trail.** Who scanned, who counted, who
  reviewed/approved/posted, system-before vs counted-after per SKU, and the
  reason for every accepted variance — preserved and tamper-evident.
- **R7 — Correct posting.** Final quantities post through native inventory
  moves and valuation so the ledger stays correct.
- **R8 — Fast and concurrent.** Multiple counters work the same session at once
  without collisions; live progress shows whether the trading deadline is at
  risk; usable on a phone, resilient to flaky connectivity.
- **R9 — Fits real store workflow.** Support both a full controlled count and a
  quick scan-to-build count, chosen per session.

---

## 9. Out of scope / non-goals

- Replacing Odoo's native inventory valuation or accounting — the count *feeds*
  them, it does not reinvent them.
- Continuous/perpetual cycle-count scheduling automation (a session is an event
  a manager initiates).
- Demand forecasting or replenishment decisions downstream of the count.
- Hardware procurement; the solution targets standard phones and Bluetooth/
  camera barcode input.

---

## 10. Success criteria

The problem is solved when a store can, before the trading deadline:

1. Count every rack with several people at once on their phones, with live
   assurance they will finish on time.
2. See only the racks that genuinely need a decision — real variances, with a
   recorded reason — and nothing drowned out by "not on this rack" noise.
3. Guarantee that no discrepancy is accepted without an independent second count
   or an appropriately authorised adjudication, and that no counter can post or
   override on their own.
4. Escalate any persistent disagreement to the right authority and always reach
   a definitive outcome.
5. Post accurate quantities to inventory and the GL, leaving an immutable record
   that finance and internal audit can trust and trend across stores.

---

*This is a living document. As the workflow evolves, new problems are added to
Section 7 and reconciled against the requirements in Section 8.*
