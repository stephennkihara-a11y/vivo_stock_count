/* Vivo Count PWA — main app.
 *
 * Screens:
 *   1. store  -> pick a store
 *   2. session -> pick a session
 *   3. sections -> pick a rack section (Scanner OR Physical Counter mode)
 *   4. scanner -> scan barcodes into the section
 *   5. physical -> enter the independent headcount
 *
 * Concurrent multi-scanner: each scanner sees the same section list. A
 * section locked by user A shows as "in progress — A" and cannot be opened
 * by user B (AC #14). The lock surfaces via the locked_by_id field on the
 * section payload, refreshed every section-list visit.
 */
import { api, drainQueue } from './api.js';
import { idb, uuid4 } from './idb.js';
import { attachHardwareScanner, cameraAvailable, startCameraScan } from './scanner.js';

const $main = document.getElementById('app-main');
const $footer = document.getElementById('footer-text');
const $queue = document.getElementById('queue-badge');
const $net = document.getElementById('net-status');
const deviceId = (() => {
    let id = localStorage.getItem('vivo_device_id');
    if (!id) {
        id = uuid4();
        localStorage.setItem('vivo_device_id', id);
    }
    return id;
})();

const state = {
    me: null,
    store: null,
    session: null,
    section: null,
    sectionLines: [],
    physicalKey: null,
};

// ----- Network + queue indicators -----
api.onNetChange((online) => {
    $net.classList.toggle('online', online);
    $net.classList.toggle('offline', !online);
    $net.textContent = online ? 'online' : 'offline';
});
api.onQueueChange((n) => {
    if (n > 0) {
        $queue.classList.remove('hidden');
        $queue.textContent = n + ' pending sync';
    } else {
        $queue.classList.add('hidden');
    }
});

// ----- Bootstrapping -----
if ('serviceWorker' in navigator) {
    navigator.serviceWorker
        .register('/vivo-count/pwa/sw.js', { scope: '/vivo-count/pwa' })
        .catch((e) => console.warn('SW failed', e));
}

(async function init() {
    try {
        state.me = await api.me();
        await renderStorePicker();
        await drainQueue();
    } catch (e) {
        $main.innerHTML = errorScreen('Could not connect — please sign in to Odoo first.');
    }
})();

// ----- Render helpers -----
function el(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
}

function errorScreen(msg) {
    return `<div class="screen"><div class="alert">${msg}</div></div>`;
}

function setFooter(s) {
    $footer.textContent = s || '—';
}

// ----- Screen: store picker -----
async function renderStorePicker() {
    setFooter(`Hello, ${state.me.name}`);
    $main.innerHTML = '<div class="screen"><h2>Pick a store</h2><div id="store-list" class="list"></div></div>';
    const stores = await api.stores();
    const $list = $main.querySelector('#store-list');
    if (!stores.length) {
        $list.innerHTML = '<div class="empty">No active count sessions. Ask your manager to start one.</div>';
        return;
    }
    for (const s of stores) {
        const row = el(
            `<button class="row" data-id="${s.id}"><span>${s.name}</span><span class="chev">›</span></button>`
        );
        row.addEventListener('click', () => {
            state.store = s;
            renderSessionPicker();
        });
        $list.appendChild(row);
    }
}

// ----- Screen: session picker -----
async function renderSessionPicker() {
    setFooter(state.store.name);
    $main.innerHTML = `
        <div class="screen">
            <div class="back" id="back">← Stores</div>
            <h2>Active sessions</h2>
            <div id="session-list" class="list"></div>
        </div>`;
    $main.querySelector('#back').addEventListener('click', renderStorePicker);
    const sessions = await api.sessions(state.store.id);
    const $list = $main.querySelector('#session-list');
    if (!sessions.length) {
        $list.innerHTML = '<div class="empty">No open sessions for this store.</div>';
        return;
    }
    for (const s of sessions) {
        const row = el(`
            <button class="row" data-id="${s.id}">
                <div class="col">
                    <strong>${s.name}</strong>
                    <small>${s.zone_name} — ${s.sections_reconciled}/${s.sections_total} reconciled</small>
                </div>
                <span class="chev">›</span>
            </button>`);
        row.addEventListener('click', () => {
            state.session = s;
            renderSectionList();
        });
        $list.appendChild(row);
    }
}

// ----- Screen: section list (the rack board) -----
const STATE_LABELS = {
    draft: 'Not started',
    scanning: 'Scanning',
    physical_count: 'Awaiting physical',
    variance_rescan: 'Variance re-scan',
    reconciled: 'Reconciled',
};

async function renderSectionList() {
    setFooter(`${state.session.name}`);
    $main.innerHTML = `
        <div class="screen">
            <div class="back" id="back">← Sessions</div>
            <h2>Pick a rack</h2>
            <div id="section-list" class="list"></div>
        </div>`;
    $main.querySelector('#back').addEventListener('click', renderSessionPicker);
    const sections = await api.sections(state.session.id);
    const $list = $main.querySelector('#section-list');
    const reconciled = sections.filter((s) => s.state === 'reconciled').length;
    setFooter(`Rack ${reconciled}/${sections.length} reconciled`);
    for (const s of sections) {
        const lockMsg =
            s.locked_by_id && s.locked_by_id !== state.me.id
                ? `🔒 ${s.locked_by_name}`
                : '';
        const row = el(`
            <button class="row state-${s.state}" data-id="${s.id}" ${
            lockMsg && !s.is_mine ? 'disabled' : ''
        }>
                <div class="col">
                    <strong>${s.name}</strong>
                    <small>${s.zone_name} · ${STATE_LABELS[s.state] || s.state}${
            s.rescan_count > 0 ? ` · ↻ ${s.rescan_count}` : ''
        }</small>
                </div>
                <span class="meta">${lockMsg}</span>
            </button>`);
        row.addEventListener('click', () => {
            state.section = s;
            // Routing per current state.
            if (s.state === 'physical_count') {
                renderPhysical();
            } else if (s.state === 'reconciled') {
                renderReconciledRecap();
            } else {
                openSectionForScanning();
            }
        });
        $list.appendChild(row);
    }
}

// ----- Open + acquire lock + go into scanner mode -----
async function openSectionForScanning() {
    try {
        const r = await api.openSection(state.section.id);
        if (r && r.error) {
            alert(r.error);
            return renderSectionList();
        }
        state.section.state = r.state;
        renderScanner();
    } catch (e) {
        alert(e.message);
    }
}

// ----- Screen: scanner mode -----
async function renderScanner() {
    setFooter(`Scanning ${state.section.name}`);
    $main.innerHTML = `
        <div class="screen scanner">
            <div class="back" id="back">← Racks</div>
            <div class="actions">
                <button id="btn-finish" class="btn primary">Finish scanning</button>
                <button id="btn-pause" class="btn ghost">Pause</button>
            </div>
            <h2>${state.section.name}</h2>
            <div class="scan-bar">
                <input id="barcode-input" placeholder="Scan or type barcode" autocomplete="off" autofocus inputmode="text"/>
                <button id="btn-camera" class="btn ghost" ${cameraAvailable() ? '' : 'disabled'}>📷</button>
            </div>
            <video id="cam" playsinline muted style="display:none;max-width:100%;border-radius:6px;margin-top:8px"></video>
            <div id="last-scan" class="last-scan"></div>
            <div id="pending-banner" class="pending-banner hidden"></div>
            <h3 style="margin-top:16px">This rack</h3>
            <div id="line-list" class="list compact"></div>
            <div class="totals">
                <span>Scan total</span><strong id="scan-total">0</strong>
            </div>
        </div>`;

    $main.querySelector('#back').addEventListener('click', async () => {
        await api.releaseLock(state.section.id).catch(() => {});
        renderSectionList();
    });
    $main.querySelector('#btn-pause').addEventListener('click', async () => {
        await api.releaseLock(state.section.id).catch(() => {});
        renderSectionList();
    });
    $main.querySelector('#btn-finish').addEventListener('click', async () => {
        const r = await api.finishScanning(state.section.id);
        if (r && r.error) {
            alert(r.error);
        } else {
            renderSectionList();
        }
    });

    const $bc = $main.querySelector('#barcode-input');
    attachHardwareScanner($bc, handleScan);

    $main.querySelector('#btn-camera').addEventListener('click', async () => {
        const v = $main.querySelector('#cam');
        v.style.display = 'block';
        try {
            const stop = await startCameraScan(v, handleScan);
            v._stop = stop;
        } catch (e) {
            alert(e.message);
        }
    });

    await refreshLines();
    // Resume: push any local scans still pending from a prior session/blip.
    api.flushQueue().then(() => refreshLines()).catch(() => {});
}

async function handleScan(barcode) {
    // Auto-add one unit per scan — a barcode scanner fires once per physical
    // item, so no quantity prompt. Repeat scans of the same item accumulate
    // (record_scan sums scanned_qty server-side).
    const qty = 1;
    const product = await resolveBarcode(barcode);
    const $last = $main.querySelector('#last-scan');
    if (!product) {
        // No SKU matched — CAPTURE the raw barcode as a product-less line
        // instead of dropping it. No popup, no note prompt, zero extra taps;
        // it will render RED in the list and flow through the reports.
        const r = await api.scan({
            section_id: state.section.id,
            product_id: null,
            scanned_qty: qty,
            device_id: deviceId,
            product_name: 'Unknown',
            barcode: barcode,
            scanned_barcode: barcode,
        });
        if (r.error) {
            if ($last) $last.innerHTML = `<div class="warn">${r.error}</div>`;
        } else if (r.queued) {
            if ($last)
                $last.innerHTML = `<div class="ok queued">Saved on device — syncing: Unknown (${barcode})</div>`;
        } else {
            if ($last)
                $last.innerHTML = `<div class="warn">⚠ Unknown barcode ${barcode} — captured ✓</div>`;
        }
        await refreshLines();
        return;
    }
    const r = await api.scan({
        section_id: state.section.id,
        product_id: product.product_id || product.id,
        scanned_qty: qty,
        device_id: deviceId,
        product_name: product.name,
        barcode: product.barcode,
    });
    if (r.error) {
        // Server was reached and rejected it (e.g. rack no longer scanning).
        $last.innerHTML = `<div class="warn">${r.error}</div>`;
    } else if (r.queued) {
        // Saved on the device; will sync automatically when the network returns.
        $last.innerHTML = `<div class="ok queued">Saved on device — syncing: ${product.name}</div>`;
    } else {
        $last.innerHTML = `<div class="ok">${product.name} — saved ✓</div>`;
    }
    await refreshLines();
}

async function resolveBarcode(barcode) {
    if (!api.isOnline()) {
        const cached = await idb.lookupCachedProduct(barcode);
        if (cached) return cached;
        return null;
    }
    const r = await api.lookupBarcode(barcode, state.session.id);
    if (r && r.found) {
        await idb.cacheProduct(r);
        return r;
    }
    return null;
}

async function refreshLines() {
    const $list = $main.querySelector('#line-list');
    if (!$list) return;

    // Device source of truth for scans not yet confirmed by the server.
    const pending = await idb.pendingForSection(state.section.id);
    // Server-confirmed lines; keep the last-known set when offline.
    if (api.isOnline()) {
        try {
            state.sectionLines = await api.sectionLines(state.section.id);
        } catch (e) {
            /* keep last-known list */
        }
    }
    const serverLines = state.sectionLines || [];

    // A line is either a known SKU (keyed by product_id) or an unknown capture
    // (keyed by scanned_barcode). One key space covers both so saved+pending
    // merge on the same row.
    const keyOf = (r) =>
        r.is_unknown || (!r.product_id && r.scanned_barcode)
            ? `u:${r.scanned_barcode}`
            : `p:${r.product_id}`;

    // Group pending scans so a row shows saved + in-flight together.
    const pendingByKey = {};
    for (const p of pending) {
        const isUnknown = !p.product_id && !!p.scanned_barcode;
        const key = keyOf(p);
        const slot = (pendingByKey[key] = pendingByKey[key] || {
            qty: 0,
            name: isUnknown ? 'Unknown' : p.product_name,
            barcode: isUnknown ? p.scanned_barcode : p.barcode,
            product_id: p.product_id || null,
            scanned_barcode: p.scanned_barcode || null,
            is_unknown: isUnknown,
        });
        slot.qty += p.scanned_qty || 1;
    }

    const rows = [];
    const seen = new Set();
    for (const l of serverLines) {
        const key = keyOf(l);
        const pend = pendingByKey[key];
        rows.push({
            line_id: l.id,
            product_id: l.product_id,
            scanned_barcode: l.scanned_barcode,
            is_unknown: !!l.is_unknown,
            name: l.is_unknown ? 'Unknown' : l.product_name,
            barcode: l.is_unknown ? l.scanned_barcode : l.barcode,
            saved: l.counted_qty || 0,
            pending: pend ? pend.qty : 0,
        });
        seen.add(key);
    }
    // Pending-only rows (first scan still in flight, no server line yet).
    for (const key of Object.keys(pendingByKey)) {
        if (seen.has(key)) continue;
        const pend = pendingByKey[key];
        rows.push({
            line_id: null,
            product_id: pend.product_id,
            scanned_barcode: pend.scanned_barcode,
            is_unknown: pend.is_unknown,
            name: pend.name,
            barcode: pend.barcode,
            saved: 0,
            pending: pend.qty,
        });
    }

    $list.innerHTML = '';
    let total = 0;
    for (const row of rows) {
        const shown = row.saved + row.pending; // running count the counter sees
        total += shown;
        const isPending = row.pending > 0;
        const status = isPending
            ? '<span class="line-status pending" title="Syncing">⏳</span>'
            : '<span class="line-status saved" title="Saved on server">✓</span>';
        const $row = el(`
            <div class="row compact ${isPending ? 'is-pending' : ''} ${
            row.is_unknown ? 'is-unknown' : ''
        }">
                <div class="col">
                    <strong>${row.name || ''}</strong>
                    <small>${row.barcode || ''}</small>
                </div>
                ${status}
                <span class="qty">${shown}</span>
                <button class="line-del" title="Remove line" aria-label="Remove line">✕</button>
            </div>`);
        $row.querySelector('.line-del').addEventListener('click', async (ev) => {
            ev.stopPropagation();
            if (!confirm(`Remove ${row.name} from this rack?`)) return;
            // Purge any not-yet-synced local scans for this item FIRST, so a
            // deleted scan can never be resurrected by a later sync. Unknown
            // captures are keyed by barcode, known lines by product.
            if (row.is_unknown) {
                await idb.removeQueuedByBarcode(state.section.id, row.scanned_barcode);
            } else {
                await idb.removeQueuedByProduct(state.section.id, row.product_id);
            }
            await api.refreshQueue();
            if (row.line_id) {
                try {
                    const r = await api.deleteLine(row.line_id);
                    if (r && r.error) alert(r.error);
                } catch (e) {
                    alert('Cannot remove a saved line while offline.');
                }
            }
            await refreshLines();
        });
        $list.appendChild($row);
    }

    const $t = $main.querySelector('#scan-total');
    if ($t) $t.textContent = total;

    const $banner = $main.querySelector('#pending-banner');
    if ($banner) {
        if (pending.length > 0) {
            $banner.textContent = `${pending.length} scan(s) pending — syncing…`;
            $banner.classList.remove('hidden');
        } else {
            $banner.classList.add('hidden');
        }
    }
}

// ----- Screen: physical count mode -----
function renderPhysical() {
    setFooter(`Physical count — ${state.section.name}`);
    if (!state.physicalKey) state.physicalKey = uuid4();
    $main.innerHTML = `
        <div class="screen physical">
            <div class="back" id="back">← Racks</div>
            <h2>${state.section.name}</h2>
            <p class="hint">Enter your independent headcount for this rack. The scan total is hidden until both are submitted.</p>
            <div class="big-input">
                <input id="phys-input" type="number" inputmode="numeric" min="0" autofocus/>
            </div>
            <div class="actions">
                <button id="btn-submit" class="btn primary">Submit physical count</button>
            </div>
            <div id="phys-result"></div>
        </div>`;
    $main.querySelector('#back').addEventListener('click', renderSectionList);
    $main.querySelector('#btn-submit').addEventListener('click', async () => {
        const v = Number($main.querySelector('#phys-input').value);
        if (Number.isNaN(v) || v < 0) {
            alert('Enter a number ≥ 0');
            return;
        }
        const r = await api.submitPhysical(state.section.id, v, state.physicalKey);
        if (r && r.error) {
            $main.querySelector('#phys-result').innerHTML =
                `<div class="warn">${r.error}</div>`;
            return;
        }
        // Side-by-side reveal (only after submit) per spec 7.2.
        const matched = r.scan_total_qty === r.physical_total_qty;
        $main.querySelector('#phys-result').innerHTML = `
            <div class="${matched ? 'ok' : 'warn'} reveal">
                <div>Scan total: <strong>${r.scan_total_qty}</strong></div>
                <div>Physical: <strong>${r.physical_total_qty}</strong></div>
                <div>${
                    matched
                        ? '✅ Reconciled — well done.'
                        : "❌ Counts don't match — re-scan and re-count this rack."
                }</div>
            </div>`;
        state.physicalKey = null;
        setTimeout(renderSectionList, 1800);
    });
}

// ----- Screen: reconciled recap -----
function renderReconciledRecap() {
    $main.innerHTML = `
        <div class="screen">
            <div class="back" id="back">← Racks</div>
            <h2>${state.section.name}</h2>
            <div class="ok reveal">Reconciled.</div>
        </div>`;
    $main.querySelector('#back').addEventListener('click', renderSectionList);
}
