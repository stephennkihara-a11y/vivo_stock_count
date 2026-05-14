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
    drainQueue();
});
window.addEventListener('offline', () => {
    online = false;
    emitNet();
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
     * Send a scan to the server. If offline (or the request fails), the
     * scan is queued in IndexedDB and replayed on next online event.
     * Returns either the server response or a {queued: true} stub.
     */
    async scan({ section_id, product_id, scanned_qty, device_id }) {
        const idempotency_key = uuid4();
        const payload = {
            section_id,
            product_id,
            scanned_qty,
            idempotency_key,
            device_id,
        };
        if (!online) {
            await idb.queueScan(payload);
            await emitQueue();
            return { queued: true, idempotency_key };
        }
        try {
            const r = await rpc('/vivo-count/api/scan', payload);
            return r;
        } catch (e) {
            // Network or 5xx -> queue and resolve so UX continues.
            await idb.queueScan(payload);
            await emitQueue();
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
    drainQueue,
};

export async function drainQueue() {
    if (!online) return;
    const items = await idb.allQueued();
    for (const item of items) {
        try {
            const r = await rpc('/vivo-count/api/scan', item);
            if (!r || !r.error) {
                await idb.removeQueued(item.idempotency_key);
            }
        } catch (e) {
            // network flap mid-drain — stop and retry on next online event
            break;
        }
    }
    emitQueue();
}
