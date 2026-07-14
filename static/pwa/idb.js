/* Tiny IndexedDB wrapper for the offline scan queue.
 *
 * Two stores:
 *   - scanQueue: pending scan submissions awaiting network drain
 *   - productCache: barcode -> product info (offline lookups)
 *
 * Idempotency: each queued scan has a UUIDv4 generated client-side.
 * The server's UNIQUE constraint on idempotency_key turns duplicate
 * submissions into no-ops, giving us exactly-once semantics across
 * retries, online flaps, and refreshes (AC #8).
 */
const DB_NAME = 'vivo_count';
const DB_VERSION = 1;

let dbPromise = null;

function openDB() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(DB_NAME, DB_VERSION);
        req.onupgradeneeded = (e) => {
            const db = e.target.result;
            if (!db.objectStoreNames.contains('scanQueue')) {
                db.createObjectStore('scanQueue', { keyPath: 'idempotency_key' });
            }
            if (!db.objectStoreNames.contains('productCache')) {
                db.createObjectStore('productCache', { keyPath: 'barcode' });
            }
        };
        req.onsuccess = (e) => resolve(e.target.result);
        req.onerror = (e) => reject(e.target.error);
    });
    return dbPromise;
}

function _tx(store, mode = 'readonly') {
    return openDB().then((db) => db.transaction(store, mode).objectStore(store));
}

export const idb = {
    async queueScan(scan) {
        const s = await _tx('scanQueue', 'readwrite');
        return new Promise((resolve, reject) => {
            const r = s.put(scan);
            r.onsuccess = () => resolve();
            r.onerror = () => reject(r.error);
        });
    },
    async queueLength() {
        const s = await _tx('scanQueue');
        return new Promise((resolve, reject) => {
            const r = s.count();
            r.onsuccess = () => resolve(r.result);
            r.onerror = () => reject(r.error);
        });
    },
    async allQueued() {
        const s = await _tx('scanQueue');
        return new Promise((resolve, reject) => {
            const r = s.getAll();
            r.onsuccess = () => resolve(r.result || []);
            r.onerror = () => reject(r.error);
        });
    },
    async removeQueued(idempotency_key) {
        const s = await _tx('scanQueue', 'readwrite');
        return new Promise((resolve, reject) => {
            const r = s.delete(idempotency_key);
            r.onsuccess = () => resolve();
            r.onerror = () => reject(r.error);
        });
    },
    /** Pending (not-yet-synced) local scan records for one section. */
    async pendingForSection(section_id) {
        const all = await this.allQueued();
        return all.filter((s) => s.section_id === section_id);
    },
    /** Drop every pending local scan for a (section, product) — used when a
     *  line is deleted so a removed scan is not resurrected on the next sync. */
    async removeQueuedByProduct(section_id, product_id) {
        const all = await this.allQueued();
        const victims = all.filter(
            (r) => r.section_id === section_id && r.product_id === product_id
        );
        for (const r of victims) await this.removeQueued(r.idempotency_key);
        return victims.length;
    },
    async cacheProduct(p) {
        if (!p.barcode) return;
        const s = await _tx('productCache', 'readwrite');
        return new Promise((resolve, reject) => {
            const r = s.put(p);
            r.onsuccess = () => resolve();
            r.onerror = () => reject(r.error);
        });
    },
    async lookupCachedProduct(barcode) {
        const s = await _tx('productCache');
        return new Promise((resolve, reject) => {
            const r = s.get(barcode);
            r.onsuccess = () => resolve(r.result || null);
            r.onerror = () => reject(r.error);
        });
    },
};

export function uuid4() {
    // RFC 4122 v4. crypto.randomUUID is preferred but not on every browser.
    if (crypto && crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const h = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
