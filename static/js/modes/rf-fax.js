/**
 * RF Fax / OOK Bitmap decoder module.
 *
 * IIFE providing start/stop controls, SSE streaming, canvas bitmap
 * renderer, decoded line log, and export capabilities.
 */
var RfFaxMode = (function () {
    'use strict';

    var state = {
        running: false,
        initialized: false,
        eventSource: null,
        linesReceived: 0,
        expectedLines: 11,
        bitmap: null,       // 2D array: bitmap[row][col] = 0|1
        widthPixels: 0,
        imageCount: 0,
        lineLog: [],        // { timestamp, line_num, pixel_hex }
    };

    // Canvas refs
    var bitmapCanvas = null;
    var bitmapCtx = null;

    // ---- Initialization ----

    function init() {
        if (state.initialized) {
            checkStatus();
            return;
        }
        state.initialized = true;
        checkStatus();
    }

    function destroy() {
        disconnectSSE();
    }

    // ---- Status ----

    function checkStatus() {
        fetch('/rf_fax/status')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.running) {
                    state.running = true;
                    updateUI(true);
                    connectSSE();
                } else {
                    state.running = false;
                    updateUI(false);
                }
            })
            .catch(function () {});
    }

    // ---- Start / Stop ----

    function start() {
        if (state.running) return;

        var remoteSDR = typeof getRemoteSDRConfig === 'function' ? getRemoteSDRConfig() : null;
        if (remoteSDR === false) return; // validation failed, alert already shown

        var payload = {
            frequency: document.getElementById('rfFaxFrequency').value || '433.400',
            gain: document.getElementById('rfFaxGain').value || '0',
            ppm: document.getElementById('rfFaxPPM').value || '0',
            device: document.getElementById('deviceSelect')?.value || '0',
            sdr_type: document.getElementById('sdrTypeSelect')?.value || 'rtlsdr',
            short_pulse: document.getElementById('rfFaxShortPulse').value || '300',
            long_pulse: document.getElementById('rfFaxLongPulse').value || '600',
            bias_t: typeof getBiasTEnabled === 'function' ? getBiasTEnabled() : false,
        };
        if (remoteSDR) {
            payload.rtl_tcp_host = remoteSDR.host;
            payload.rtl_tcp_port = remoteSDR.port;
        }

        fetch('/rf_fax/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status === 'started') {
                state.running = true;
                state.linesReceived = 0;
                state.bitmap = null;
                state.widthPixels = 0;
                state.imageCount = 0;
                state.lineLog = [];
                updateUI(true);
                connectSSE();
                clearCanvas();
                clearLog();
            } else {
                alert('Error: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(function (err) {
            alert('Failed to start RF Fax decoder: ' + err);
        });
    }

    function stop() {
        fetch('/rf_fax/stop', { method: 'POST' })
            .then(function (r) { return r.json(); })
            .then(function () {
                state.running = false;
                updateUI(false);
                disconnectSSE();
            })
            .catch(function () {});
    }

    // ---- SSE ----

    function connectSSE() {
        disconnectSSE();
        var es = new EventSource('/rf_fax/stream');

        es.onmessage = function (e) {
            try {
                var msg = JSON.parse(e.data);
                handleMessage(msg);
            } catch (_) {}
        };

        es.onerror = function () {};

        state.eventSource = es;
    }

    function disconnectSSE() {
        if (state.eventSource) {
            state.eventSource.close();
            state.eventSource = null;
        }
    }

    function handleMessage(msg) {
        var type = msg.type;

        if (type === 'rf_fax_line') {
            handleLine(msg);
        } else if (type === 'rf_fax_image') {
            handleImage(msg);
        } else if (type === 'status') {
            if (msg.status === 'stopped') {
                state.running = false;
                updateUI(false);
                disconnectSSE();
            }
        } else if (type === 'error') {
            console.error('RF Fax error:', msg.text);
        }
    }

    // ---- Line handling ----

    function handleLine(msg) {
        state.linesReceived = msg.lines_received;

        // Update line count display
        var countEl = document.getElementById('rfFaxLineCount');
        if (countEl) {
            countEl.textContent = msg.lines_received + ' lines';
        }
        var barLines = document.getElementById('rfFaxStatusBarLines');
        if (barLines) {
            barLines.textContent = msg.lines_received + ' lines';
        }

        // Update bitmap in-memory for progressive rendering (unbounded — streaming mode)
        if (!state.bitmap) {
            state.bitmap = [];
        }
        while (state.bitmap.length <= msg.line_num) {
            state.bitmap.push([]);
        }
        state.bitmap[msg.line_num] = msg.pixels;
        if (msg.pixels.length > state.widthPixels) {
            state.widthPixels = msg.pixels.length;
        }

        // Progressive canvas render
        renderBitmap();

        // Add to log
        state.lineLog.push({
            timestamp: msg.timestamp,
            line_num: msg.line_num,
            pixel_hex: msg.pixel_hex,
            is_new: msg.is_new,
        });

        appendLogEntry(msg);
    }

    function handleImage(msg) {
        // rf_fax_image is no longer emitted by the backend in streaming mode.
        // Kept for compatibility if a future caller sends it.
        if (msg.bitmap) {
            state.bitmap = msg.bitmap;
            state.widthPixels = msg.width || state.widthPixels;
            renderBitmap();
        }
    }

    // ---- Canvas rendering ----

    function renderBitmap() {
        var canvas = document.getElementById('rfFaxCanvas');
        if (!canvas || !state.bitmap || state.bitmap.length === 0 || state.widthPixels === 0) return;

        var numRows = state.bitmap.length;
        var pixelSize = Math.max(1, Math.floor(canvas.parentElement.clientWidth / state.widthPixels));
        pixelSize = Math.min(pixelSize, 12); // cap pixel size

        var dpr = window.devicePixelRatio || 1;
        var w = state.widthPixels * pixelSize;
        var h = numRows * pixelSize;

        canvas.width = w * dpr;
        canvas.height = h * dpr;
        canvas.style.width = w + 'px';
        canvas.style.height = h + 'px';

        var ctx = canvas.getContext('2d');
        ctx.scale(dpr, dpr);

        // Background
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, w, h);

        // Draw pixels
        for (var row = 0; row < numRows; row++) {
            var pixels = state.bitmap[row];
            if (!pixels || pixels.length === 0) {
                // Draw unfilled rows as gray
                ctx.fillStyle = '#cccccc';
                ctx.fillRect(0, row * pixelSize, w, pixelSize);
                continue;
            }
            for (var col = 0; col < pixels.length; col++) {
                ctx.fillStyle = pixels[col] ? '#000000' : '#ffffff';
                ctx.fillRect(col * pixelSize, row * pixelSize, pixelSize, pixelSize);
            }
        }

        // Grid lines (if pixel size large enough)
        if (pixelSize >= 4) {
            ctx.strokeStyle = '#e0e0e0';
            ctx.lineWidth = 0.5;
            for (var gy = 0; gy <= numRows; gy++) {
                ctx.beginPath();
                ctx.moveTo(0, gy * pixelSize);
                ctx.lineTo(w, gy * pixelSize);
                ctx.stroke();
            }
            for (var gx = 0; gx <= state.widthPixels; gx++) {
                ctx.beginPath();
                ctx.moveTo(gx * pixelSize, 0);
                ctx.lineTo(gx * pixelSize, h);
                ctx.stroke();
            }
        }
    }

    function clearCanvas() {
        var canvas = document.getElementById('rfFaxCanvas');
        if (!canvas) return;
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
    }

    // ---- Log ----

    function appendLogEntry(msg) {
        var logPanel = document.getElementById('rfFaxLog');
        if (!logPanel) return;

        var div = document.createElement('div');
        div.style.cssText = 'font-family: var(--font-mono); font-size: 10px; color: ' +
            (msg.is_new ? '#00ff88' : 'var(--text-dim)') + '; padding: 1px 0;';
        div.textContent = msg.timestamp + ' Line ' +
            String(msg.line_num).padStart(2, '0') + ': ' +
            msg.pixel_hex + (msg.is_new ? '' : ' (dup)');
        logPanel.appendChild(div);
        logPanel.scrollTop = logPanel.scrollHeight;
    }

    function clearLog() {
        var logPanel = document.getElementById('rfFaxLog');
        if (logPanel) logPanel.innerHTML = '';
        state.lineLog = [];
        state.bitmap = null;
        state.widthPixels = 0;
        clearCanvas();
        var countEl = document.getElementById('rfFaxLineCount');
        if (countEl) countEl.textContent = '0 lines';
        var barLines = document.getElementById('rfFaxStatusBarLines');
        if (barLines) barLines.textContent = '0 lines';
    }

    // ---- Export ----

    function exportPng() {
        var canvas = document.getElementById('rfFaxCanvas');
        if (!canvas) return;
        var link = document.createElement('a');
        link.download = 'rf_fax_bitmap.png';
        link.href = canvas.toDataURL('image/png');
        link.click();
    }

    function tearPage() {
        // Save the current canvas as a timestamped PNG, then clear for the next page
        var canvas = document.getElementById('rfFaxCanvas');
        var ts = new Date().toTimeString().slice(0, 8).replace(/:/g, '');
        if (canvas && state.bitmap && state.bitmap.length > 0) {
            var link = document.createElement('a');
            link.download = 'rf_fax_' + ts + '.png';
            link.href = canvas.toDataURL('image/png');
            link.click();
        }

        // Add tear separator to log
        var logPanel = document.getElementById('rfFaxLog');
        if (logPanel) {
            var sep = document.createElement('div');
            sep.style.cssText = 'color: #555; font-size: 10px; padding: 4px 0; letter-spacing: 2px; border-top: 1px dashed #333; border-bottom: 1px dashed #333; margin: 4px 0; text-align: center;';
            sep.textContent = '─── page torn ' + new Date().toTimeString().slice(0, 8) + ' ───';
            logPanel.appendChild(sep);
            logPanel.scrollTop = logPanel.scrollHeight;
        }

        // Clear bitmap state for the next page
        state.bitmap = null;
        state.widthPixels = 0;
        state.linesReceived = 0;
        clearCanvas();
        var countEl = document.getElementById('rfFaxLineCount');
        if (countEl) countEl.textContent = '0 lines';
        var barLines = document.getElementById('rfFaxStatusBarLines');
        if (barLines) barLines.textContent = '0 lines';
    }

    function exportLog() {
        var lines = ['timestamp,line_num,pixel_hex,is_new'];
        state.lineLog.forEach(function (e) {
            lines.push(e.timestamp + ',' + e.line_num + ',' + e.pixel_hex + ',' + e.is_new);
        });
        downloadFile('rf_fax_log.csv', lines.join('\n'), 'text/csv');
    }

    function downloadFile(filename, content, type) {
        var blob = new Blob([content], { type: type });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = filename;
        a.click();
        URL.revokeObjectURL(url);
    }

    // ---- UI ----

    function updateUI(running) {
        var startBtn = document.getElementById('rfFaxStartBtn');
        var stopBtn = document.getElementById('rfFaxStopBtn');
        var indicator = document.getElementById('rfFaxStatusIndicator');
        var statusText = document.getElementById('rfFaxStatusText');

        if (startBtn) startBtn.style.display = running ? 'none' : '';
        if (stopBtn) stopBtn.style.display = running ? '' : 'none';

        if (indicator) {
            indicator.style.background = running ? '#00ff88' : 'var(--text-dim)';
        }
        if (statusText) {
            statusText.textContent = running ? 'Listening' : 'Standby';
        }

        // Toggle output panels
        var canvasPanel = document.getElementById('rfFaxCanvasPanel');
        var logPanel = document.getElementById('rfFaxLogPanel');
        if (canvasPanel) canvasPanel.style.display = running ? 'block' : 'none';
        if (logPanel) logPanel.style.display = running ? 'block' : 'none';
    }

    function setFreq(mhz) {
        var el = document.getElementById('rfFaxFrequency');
        if (el) el.value = mhz;
    }

    // ---- Public API ----

    return {
        init: init,
        destroy: destroy,
        start: start,
        stop: stop,
        setFreq: setFreq,
        exportPng: exportPng,
        exportLog: exportLog,
        clearLog: clearLog,
        tearPage: tearPage,
    };
})();
