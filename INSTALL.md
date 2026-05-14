# Vivo Stock Count — Pilot Install Runbook

A step-by-step procedure for installing the `vivo_stock_count` module on an
**Odoo 18 Community** test environment. Aimed at a single-store pilot
(spec Section 14 risks: "pilot store first, suggest Garden City; train
champions before wider rollout").

If anything in this runbook surprises you, **stop and ask**. Don't power
through unexpected errors on a production-adjacent database.

---

## 0 — Before you start

You need:

- An **Odoo 18 Community** test instance you control (not production).
- A **PostgreSQL** database for that instance.
- Shell access to the Odoo server (or the container running it).
- `git` on the Odoo server (or a way to copy files onto it).
- A **fresh database** is strongly recommended for the first pilot run.
  Cloning your real DB to a `pilot_<date>` database is fine.

The module lives at `odoo/vivo_stock_count/` in this repository, on
branch `claude/build-vivo-stock-count-YaYk8`.

## 1 — Get the code onto the Odoo server

On the Odoo server, in your addons folder (whatever path is in
`addons_path` in your `odoo.conf`):

```bash
cd /path/to/your/addons   # whatever maps to addons_path
git clone --branch claude/build-vivo-stock-count-YaYk8 \
    <repo-url> line_balancing
```

That clones the whole repo. You only need the `odoo/vivo_stock_count/`
folder inside it — but the rest is harmless.

Either:

- **Option A — symlink:**
  ```bash
  ln -s /path/to/your/addons/line_balancing/odoo/vivo_stock_count \
        /path/to/your/addons/vivo_stock_count
  ```
- **Option B — copy:**
  ```bash
  cp -r /path/to/your/addons/line_balancing/odoo/vivo_stock_count \
        /path/to/your/addons/
  ```

Either way you should end up with `/path/to/your/addons/vivo_stock_count/`
visible directly inside an `addons_path` entry.

## 2 — Verify Python dependencies

The module's only non-Odoo Python dep is **`xlsxwriter`**, which is
bundled with every Odoo 18 install. No `pip install` should be needed.

If you're running Odoo via Docker, the official `odoo:18` image ships
`xlsxwriter` already. If you've built your own image, run:

```bash
docker exec -it <odoo-container> python -c "import xlsxwriter; print(xlsxwriter.__version__)"
```

Anything 3.x is fine.

## 3 — Restart Odoo and update the apps list

```bash
sudo systemctl restart odoo        # or `docker restart <container>`
```

In the Odoo UI:

1. Log in as an admin user.
2. Go to **Apps**.
3. Click **Update Apps List** (you may need to enable developer mode
   first: Settings → Activate the developer mode).

## 4 — Install the module

In the Apps screen, search for **"Vivo Stock Count"**. Click **Install**.

If you'd rather install from the command line (recommended for the
pilot DB — gives you full install logs in real time):

```bash
./odoo-bin -d pilot_vivo \
    -i vivo_stock_count \
    --stop-after-init \
    --log-level=info
```

Watch the log for any traceback. The install creates:

- 8 new database tables (`vivo_count_session`, `vivo_count_section`, etc.)
- 4 security groups (Counter, Store Manager, Regional Manager, CFOO/Audit)
- 1 cron job (monthly cross-store roll-up, disabled until you toggle it)
- Sequence prefixes `VFG/yyyy/mm/` and `RECON/yyyy/mm/`

Expected duration: under 30 seconds on a small DB.

## 5 — Run the test suite once (recommended)

Before any user touches the module on this DB, prove the install is
sound:

```bash
./odoo-bin -d pilot_vivo \
    -i vivo_stock_count \
    --test-enable \
    --test-tags vivo_count \
    --stop-after-init \
    --log-level=test
```

Expected: **65 tests pass**. Anything red — stop and read the trace
before doing anything else. Most likely culprits are environment
issues (Postgres locale, missing `xlsxwriter`, valuation account
config) rather than module bugs at this point.

## 6 — (Optional) Install demo data

For a hands-on QA walkthrough, install with the `--with-demo` flag on
a fresh DB:

```bash
./odoo-bin -d pilot_vivo_demo --without-demo=False \
    -i vivo_stock_count \
    --stop-after-init
```

This seeds: 1 store location, 3 zones (Display Floor, Backroom, Fitting
Rooms), 12 rack templates, 5 sample SKUs, and 1 draft session. Enough
to drive the full happy path end-to-end without typing anything.

> If you're installing onto a database that already has production data,
> **skip this step**. Don't mix demo and real data.

## 7 — Configure the pilot store

After install, in the Odoo UI go to **Stock Count → Configuration**:

1. **Zones** — create one zone per physical area of the pilot store
   (Display Floor, Backroom, Fitting Rooms, Transit, Damaged Stock).
   Each zone references the store's `stock.location`.
2. **Rack Templates** — for each zone, add one row per physical rack
   (e.g. "Rack A1 — Dresses"). These are cloned into per-session
   sections every time a session starts. Configure once, reuse forever.
3. **Settings → Vivo Stock Count** — tune approval bands and the
   section idle-lock window if the spec defaults (5 000 / 25 000 KES,
   30 min idle) don't fit. Set the **Audit notification group** if
   you want notifications routed to a custom group instead of the
   default CFOO/Audit.

## 8 — Map users to groups

In **Settings → Users & Companies → Users**, for each pilot user:

- **Floor staff scanning items** → *Counter*
- **Store Manager who reviews and approves** → *Store Manager*
- **Regional Manager** → *Regional Manager*
- **CFOO / Internal Audit** → *CFOO / Internal Audit*

Higher groups imply the lower ones (Store Manager has Counter
permissions automatically), so don't double-tick.

## 9 — Mobile PWA install on a phone

On the counter's phone, with HTTPS Odoo access:

1. Open `https://<your-odoo-host>/vivo-count/pwa` in Chrome (Android)
   or Safari (iOS).
2. Sign in.
3. Tap the browser menu → **Add to Home Screen**. The app installs as a
   standalone launcher with the Vivo "V" icon.
4. Open from the home screen. It runs chromeless.

On first load the service worker caches the shell, so the app then
works offline for up to 60 minutes per session.

## 10 — First test count (sanity check)

Do this end-to-end with a small section before doing a real count:

1. As **Store Manager**: **Stock Count → Count Sessions → New**.
   Pick the pilot store, hit **Start Count**.
2. As **Counter A** on the PWA: open the session → pick a rack → scan
   3–5 items → tap **Finish Scanning**.
3. As **Counter B** on the PWA: open the same session → pick the same
   rack → enter the independent physical headcount → tap **Submit**.
4. Confirm the side-by-side reveal shows reconciled (or bounces to
   re-scan if mismatched).
5. Repeat for any other racks you want in scope.
6. As **Store Manager**: **Submit for Review** → **Review & Approve**
   on the wizard → **Post to Inventory**.
7. Open the generated **Stock Take Reconciliation** record. Click
   **Print PDF** and **Export Excel** — both should download cleanly.
8. As **CFOO/Audit user**: confirm the reconciliation appears in your
   **Activities** panel with a working link.

That covers all 19 acceptance criteria in a single 10-minute drill.

## 11 — Rollback plan

If something goes wrong and you need to back out:

```bash
./odoo-bin -d pilot_vivo --without-demo=all shell <<'PY'
self.env['ir.module.module'].search([('name','=','vivo_stock_count')]).button_uninstall()
self.env.cr.commit()
PY
```

Or in the UI: **Apps → Vivo Stock Count → Uninstall**.

Uninstalling **deletes all `vivo.count.*` records**, including
reconciliations. If you've already applied any sessions, the
`stock.quant` and `account.move` records they created remain (those
belong to Odoo, not the module) — you do **not** lose posted
adjustments by uninstalling. But the audit trail of who counted what
will be gone, so don't uninstall after a real count without exporting
the reconciliation PDFs first.

## 12 — Known limitations to brief the pilot team on

- **Real-time desktop refresh** from concurrent PWA scanners is not yet
  pushed via `bus.bus`. The section progress board reflects whichever
  state is persisted; manual refresh (or a route nav) re-fetches. Per-
  scanner UX on the PWA itself is real-time; cross-device on desktop
  is eventually-consistent.
- **`per_sku` physical-count mode** is a config switch but only the
  `per_section` mode is wired. Matches today's Excel sheet (Q4 default).
- **Account journal entries** auto-create on Apply only for products in
  real-time valuation. Standard-cost periodic products get the
  `stock.move` audit trail but no journal entry until period close —
  this is Odoo's native behaviour, not a module choice.
- **Sample/demo data is opt-in via `--with-demo`** and should not be
  installed onto a database that already has real product / location
  data.

## 13 — When in doubt

Reply on the same thread that built the module. I have the full
spec, the design decisions, and the test coverage matrix in context —
much faster than re-explaining.
