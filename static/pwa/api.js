/* JSON-RPC-ish API client.
 *
 * Odoo's type='json' controllers accept a {"params": {...}} body and reply
 * with {"jsonrpc":"2.0","id":...,"result":...}. We unwrap result for
 * the caller and raise on transport / error fields.
 */
import { idb, uuid4 } from './idb.js';

let onlineCb = null;
let queueChangeCb = null;
let online = navigator.onLine;

function emitNet() {
    if (onlineCb) onlineCb(online);
}

async function emitQueue() {
    if (queueChangeCb) queueChangeCb(await idb.queueLength());
}

window.addEventListener('online', () => {
    online = true;
    emitNet();
    backoff = MIN_BACKOFF;
    flushQueue();
});
window.addEventListener('offline', () => {
    online = false;
    emitNet();
});
// A short dead-spot often ends without a clean 'online' event (e.g. the tab was
// backgrounded during the gap). Flush whenever the tab regains focus, too.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && online) {
        backoff = MIN_BACKOFF;
        flushQueue();
    }
});

async function rpc(path, params = {}) {
    const resp = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', method: 'call', params }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) {
        const e = new Error(data.error.message || 'RPC error');
        e.data = data.error.data;
        throw e;
    }
    return data.result;
}

export const api = {
    me: () => rpc('/vivo-count/api/me'),
    stores: () => rpc('/vivo-count/api/stores'),
    sessions: (location_id) => rpc('/vivo-count/api/sessions', { location_id }),
    sections: (session_id) => rpc('/vivo-count/api/sections', { session_id }),
    sectionLines: (section_id) => rpc('/vivo-count/api/section/lines', { section_id }),
    deleteLine: (line_id) => rpc('/vivo-count/api/line/delete', { line_id }),
    lookupBarcode: (barcode, session_id) =>
        rpc('/vivo-count/api/lookup_barcode', { barcode, session_id }),
    openSection: (section_id) => rpc('/vivo-count/api/section/open', { section_id }),
    finishScanning: (section_id) =>
        rpc('/vivo-count/api/section/finish_scanning', { section_id }),
    releaseLock: (section_id) =>
        rpc('/vivo-count/api/section/release_lock', { section_id }),
    submitPhysical: (section_id, physical_qty, idempotency_key) =>
        rpc('/vivo-count/api/section/submit_physical', {
            section_id,
            physical_qty,
            idempotency_key,
        }),
    /**
     * LOCAL-FIRST scan. The scan is persisted to IndexedDB BEFORE any network
     * call, so a scan lost mid-send, on a connection blip, or on device death
     * survives and replays on resume. It then attempts the server save:
     *   - success        -> drop the local copy (server is now authoritative);
     *   - server rejected -> drop (retrying an invalid scan won't help);
     *   - transport fail  -> leave 'pending' and let the backoff flusher retry.
     * Returns {synced:true,...server} or {queued:true, idempotency_key}. Never
     * throws — the counter keeps scanning regardless of the network.
     */
    async scan({ section_id, product_id, scanned_qty, device_id, product_name, barcode, scanned_barcode }) {
        const idempotency_key = uuid4();
        const record = {
            idempotency_key, // client uuid — the server de-dupes on this
            section_id,
            product_id,
            scanned_qty,
            device_id,
            product_name,
            barcode,
            // Set only for unknown captures (no product matched). Keys the
            // product-less line server-side and the local pending group.
            scanned_barcode,
            ts: Date.now(),
            status: 'pending',
        };
        await idb.queueScan(record); // (1) LOCAL-FIRST: persist before network
        await emitQueue();
        if (!online) {
            scheduleFlush();
            return { queued: true, idempotency_key };
        }
        try {
            const r = await rpc('/vivo-count/api/scan', _serverPayload(record));
            // Response received (success OR server-side rejection) is terminal:
            // the server has a verdict, so drop the local copy either way.
            await idb.removeQueued(idempotency_key);
            await emitQueue();
            return { ...r, idempotency_key, synced: !r.error };
        } catch (e) {
            // Transport failure (offline / timeout / 5xx) — stays pending.
            scheduleFlush();
            return { queued: true, idempotency_key };
        }
    },
    onNetChange: (cb) => {
        onlineCb = cb;
        emitNet();
    },
    onQueueChange: (cb) => {
        queueChangeCb = cb;
        emitQueue();
    },
    isOnline: () => online,
    flushQueue,
    drainQueue,
    // Re-emit the pending count after a direct IndexedDB change made outside
    // api.scan (e.g. a delete purging pending records).
    refreshQueue: () => emitQueue(),
};

// ----- Sync queue + auto-retry with backoff -----
const MIN_BACKOFF = 2000;
const MAX_BACKOFF = 30000;
let backoff = MIN_BACKOFF;
let flushTimer = null;

// Only the fields the /api/scan controller accepts — the local record carries
// extra display fields (product_name, barcode, ts, status) that must not be
// posted (the JSON controller rejects unexpected kwargs).
function _serverPayload(r) {
    return {
        section_id: r.section_id,
        product_id: r.product_id,
        scanned_qty: r.scanned_qty,
        idempotency_key: r.idempotency_key,
        device_id: r.device_id,
        scanned_barcode: r.scanned_barcode,
    };
}

/**
 * Flush every pending local scan to the server. Success or a server-side
 * rejection drops the item (terminal — the same request would get the same
 * verdict); a transport failure stops the drain and reschedules with backoff.
 * Idempotency (the server de-dupes on idempotency_key) makes a replay after an
 * ambiguous failure safe: it can never create a second line or double the qty.
 */
export async function flushQueue() {
    if (flushTimer) {
        clearTimeout(flushTimer);
        flushTimer = null;
    }
    if (!online) return;
    const items = await idb.allQueued();
    for (const item of items) {
        try {
            await rpc('/vivo-count/api/scan', _serverPayload(item));
            await idb.removeQueued(item.idempotency_key);
        } catch (e) {
            // Transport flap mid-drain — keep the rest, retry on backoff.
            await emitQueue();
            scheduleFlush();
            return;
        }
    }
    await emitQueue();
    backoff = MIN_BACKOFF; // queue drained clean
}

function scheduleFlush() {
    if (flushTimer || !online) return;
    flushTimer = setTimeout(() => {
        flushTimer = null;
        flushQueue();
    }, backoff);
    backoff = Math.min(backoff * 2, MAX_BACKOFF);
}

// Back-compat alias — app.js calls drainQueue() on boot.
export async function drainQueue() {
    backoff = MIN_BACKOFF;
    return flushQueue();
}
