/* Barcode capture.
 *
 * Two input paths:
 *   1. Hardware Bluetooth scanners — these are HID keyboards that type the
 *      barcode followed by Enter. We just keep an input focused and listen.
 *   2. Camera via the BarcodeDetector API (Chrome/Edge/Android). Falls back
 *      gracefully when unsupported.
 *
 * Both call the same onScan(barcode) callback.
 */

export function attachHardwareScanner(input, onScan) {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            const v = input.value.trim();
            if (v) {
                onScan(v);
                input.value = '';
            }
            e.preventDefault();
        }
    });
    // Keep focus pinned for HID scanner input.
    input.focus();
    document.addEventListener('click', () => input.focus());
}

export function cameraAvailable() {
    return 'BarcodeDetector' in window && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
}

export async function startCameraScan(videoEl, onScan) {
    if (!cameraAvailable()) {
        throw new Error(
            'Camera scanning is not supported on this device. Use a hardware scanner or type the barcode.'
        );
    }
    const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment' },
    });
    videoEl.srcObject = stream;
    await videoEl.play();
    const formats = ['code_128', 'code_39', 'ean_13', 'ean_8', 'upc_a', 'upc_e', 'qr_code'];
    const detector = new window.BarcodeDetector({ formats });
    let stopped = false;
    let lastValue = null;
    let lastTime = 0;
    async function loop() {
        if (stopped) return;
        try {
            const codes = await detector.detect(videoEl);
            if (codes && codes.length) {
                const v = codes[0].rawValue;
                const now = Date.now();
                // Debounce duplicate detections from the same frame range.
                if (v && (v !== lastValue || now - lastTime > 1200)) {
                    lastValue = v;
                    lastTime = now;
                    onScan(v);
                }
            }
        } catch (e) {
            // ignore detection misses
        }
        requestAnimationFrame(loop);
    }
    loop();
    return function stop() {
        stopped = true;
        stream.getTracks().forEach((t) => t.stop());
    };
}
