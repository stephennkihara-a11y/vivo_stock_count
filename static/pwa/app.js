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
            <h2>${state.section.name}</h2>
            <div class="scan-bar">
                <input id="barcode-input" placeholder="Scan or type barcode" autocomplete="off" autofocus inputmode="text"/>
                <button id="btn-camera" class="btn ghost" ${cameraAvailable() ? '' : 'disabled'}>📷</button>
            </div>
            <video id="cam" playsinline muted style="display:none;max-width:100%;border-radius:6px;margin-top:8px"></video>
            <div id="last-scan" class="last-scan"></div>
            <h3 style="margin-top:16px">This rack</h3>
            <div id="line-list" class="list compact"></div>
            <div class="totals">
                <span>Scan total</span><strong id="scan-total">0</strong>
            </div>
            <div class="actions">
                <button id="btn-finish" class="btn primary">Finish scanning</button>
                <button id="btn-pause" class="btn ghost">Pause</button>
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
}

async function handleScan(barcode) {
    const product = await resolveBarcode(barcode);
    if (!product) {
        const $last = $main.querySelector('#last-scan');
        if ($last) $last.innerHTML = `<div class="warn">No SKU found for ${barcode}</div>`;
        return;
    }
    // Scan-once-then-type-qty (AC #6). Default 1; user can edit and re-tap.
    const qty = Number(prompt(`Quantity for ${product.name}?`, '1'));
    if (!qty || qty <= 0) return;
    const r = await api.scan({
        section_id: state.section.id,
        product_id: product.product_id || product.id,
        scanned_qty: qty,
        device_id: deviceId,
    });
    const $last = $main.querySelector('#last-scan');
    if (r.queued) {
        $last.innerHTML = `<div class="ok queued">Queued offline: ${product.name} × ${qty}</div>`;
    } else if (r.error) {
        $last.innerHTML = `<div class="warn">${r.error}</div>`;
    } else {
        $last.innerHTML = `<div class="ok">${product.name} → total ${r.counted_qty}</div>`;
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
    if (!api.isOnline()) return; // offline UI keeps the last-rendered list
    const lines = await api.sectionLines(state.section.id);
    state.sectionLines = lines;
    const $list = $main.querySelector('#line-list');
    if (!$list) return;
    $list.innerHTML = '';
    let total = 0;
    for (const l of lines) {
        total += l.counted_qty;
        const row = el(`
            <div class="row compact">
                <div class="col">
                    <strong>${l.product_name}</strong>
                    <small>${l.barcode || ''}</small>
                </div>
                <span class="qty">${l.counted_qty}</span>
            </div>`);
        $list.appendChild(row);
    }
    const $t = $main.querySelector('#scan-total');
    if ($t) $t.textContent = total;
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
