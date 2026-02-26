/**
 * Generic OOK Signal Decoder module.
 *
 * IIFE providing start/stop controls, SSE streaming, and a live-updating
 * frame log with configurable bit order (MSB/LSB) and ASCII interpretation.
 * The backend sends raw bits; all byte grouping and ASCII display is done
 * here so bit order can be flipped without restarting the decoder.
 */
var OokMode = (function () {
    'use strict';

    var state = {
        running: false,
        initialized: false,
        eventSource: null,
        frames: [],          // raw frame objects from SSE
        frameCount: 0,
        bitOrder: 'msb',     // 'msb' | 'lsb'
    };

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
        fetch('/ook/status')
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
        if (remoteSDR === false) return;

        var payload = {
            frequency: document.getElementById('ookFrequency').value || '433.920',
            gain: document.getElementById('ookGain').value || '0',
            ppm: document.getElementById('ookPPM').value || '0',
            device: document.getElementById('deviceSelect')?.value || '0',
            sdr_type: document.getElementById('sdrTypeSelect')?.value || 'rtlsdr',
            encoding: document.getElementById('ookEncoding').value || 'pwm',
            short_pulse: document.getElementById('ookShortPulse').value || '300',
            long_pulse: document.getElementById('ookLongPulse').value || '600',
            reset_limit: document.getElementById('ookResetLimit').value || '8000',
            gap_limit: document.getElementById('ookGapLimit').value || '5000',
            tolerance: document.getElementById('ookTolerance').value || '150',
            min_bits: document.getElementById('ookMinBits').value || '8',
            deduplicate: document.getElementById('ookDeduplicate')?.checked || false,
            bias_t: typeof getBiasTEnabled === 'function' ? getBiasTEnabled() : false,
        };
        if (remoteSDR) {
            payload.rtl_tcp_host = remoteSDR.host;
            payload.rtl_tcp_port = remoteSDR.port;
        }

        fetch('/ook/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.status === 'started') {
                state.running = true;
                state.frames = [];
                state.frameCount = 0;
                updateUI(true);
                connectSSE();
                clearOutput();
            } else {
                alert('Error: ' + (data.message || 'Unknown error'));
            }
        })
        .catch(function (err) {
            alert('Failed to start OOK decoder: ' + err);
        });
    }

    function stop() {
        fetch('/ook/stop', { method: 'POST' })
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
        var es = new EventSource('/ook/stream');
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
        if (msg.type === 'ook_frame') {
            handleFrame(msg);
        } else if (msg.type === 'status') {
            if (msg.status === 'stopped') {
                state.running = false;
                updateUI(false);
                disconnectSSE();
            }
        } else if (msg.type === 'error') {
            console.error('OOK error:', msg.text);
        }
    }

    // ---- Frame handling ----

    function handleFrame(msg) {
        state.frames.push(msg);
        state.frameCount++;

        var countEl = document.getElementById('ookFrameCount');
        if (countEl) countEl.textContent = state.frameCount + ' frames';
        var barEl = document.getElementById('ookStatusBarFrames');
        if (barEl) barEl.textContent = state.frameCount + ' frames';

        appendFrameEntry(msg, state.bitOrder);
    }

    // ---- Bit interpretation ----

    /**
     * Interpret a raw bit string as bytes and attempt ASCII.
     * @param {string} bits - MSB-first bit string from backend
     * @param {string} order - 'msb' | 'lsb'
     * @returns {{hex: string, ascii: string, printable: string}}
     */
    function interpretBits(bits, order) {
        var hexChars = [];
        var asciiChars = [];
        var printableChars = [];

        for (var i = 0; i + 8 <= bits.length; i += 8) {
            var byteBits = bits.slice(i, i + 8);
            if (order === 'lsb') {
                byteBits = byteBits.split('').reverse().join('');
            }
            var byteVal = parseInt(byteBits, 2);
            hexChars.push(byteVal.toString(16).padStart(2, '0'));

            if (byteVal >= 0x20 && byteVal <= 0x7E) {
                asciiChars.push(String.fromCharCode(byteVal));
                printableChars.push(String.fromCharCode(byteVal));
            } else {
                asciiChars.push('.');
            }
        }

        return {
            hex: hexChars.join(''),
            ascii: asciiChars.join(''),
            printable: printableChars.join(''),
        };
    }

    function appendFrameEntry(msg, order) {
        var panel = document.getElementById('ookOutput');
        if (!panel) return;

        var interp = interpretBits(msg.bits, order);
        var hasPrintable = interp.printable.length > 0;

        var div = document.createElement('div');
        div.className = 'ook-frame';
        div.dataset.bits = msg.bits;
        div.dataset.bitCount = msg.bit_count;
        div.dataset.inverted = msg.inverted ? '1' : '0';

        var color = hasPrintable ? '#00ff88' : 'var(--text-dim)';
        var suffix = '';
        if (msg.inverted) suffix += ' <span style="opacity:.5">(inv)</span>';

        div.innerHTML =
            '<span style="color:var(--text-dim)">' + msg.timestamp + '</span>' +
            '  <span style="color:#888">[' + msg.bit_count + 'b]</span>' +
            suffix +
            '<br>' +
            '<span style="padding-left:8em; color:' + color + '; font-family:var(--font-mono); font-size:10px">' +
            'hex: ' + interp.hex +
            '</span>' +
            '<br>' +
            '<span style="padding-left:8em; color:' + (hasPrintable ? '#aaffcc' : '#555') + '; font-family:var(--font-mono); font-size:10px">' +
            'ascii: ' + interp.ascii +
            '</span>';

        div.style.cssText = 'font-size:11px; padding: 4px 0; border-bottom: 1px solid #1a1a1a; line-height:1.6;';

        panel.appendChild(div);
        panel.scrollTop = panel.scrollHeight;
    }

    // ---- Bit order toggle ----

    function setBitOrder(order) {
        state.bitOrder = order;

        // Update button states
        var msbBtn = document.getElementById('ookBitMSB');
        var lsbBtn = document.getElementById('ookBitLSB');
        if (msbBtn) msbBtn.style.background = order === 'msb' ? 'var(--accent)' : '';
        if (msbBtn) msbBtn.style.color = order === 'msb' ? '#000' : '';
        if (lsbBtn) lsbBtn.style.background = order === 'lsb' ? 'var(--accent)' : '';
        if (lsbBtn) lsbBtn.style.color = order === 'lsb' ? '#000' : '';

        // Re-render all stored frames
        var panel = document.getElementById('ookOutput');
        if (!panel) return;
        panel.innerHTML = '';
        state.frames.forEach(function (msg) {
            appendFrameEntry(msg, order);
        });
    }

    // ---- Output panel ----

    function clearOutput() {
        var panel = document.getElementById('ookOutput');
        if (panel) panel.innerHTML = '';
        state.frames = [];
        state.frameCount = 0;
        var countEl = document.getElementById('ookFrameCount');
        if (countEl) countEl.textContent = '0 frames';
        var barEl = document.getElementById('ookStatusBarFrames');
        if (barEl) barEl.textContent = '0 frames';
    }

    function exportLog() {
        var lines = ['timestamp,bit_count,hex_msb,ascii_msb,inverted'];
        state.frames.forEach(function (msg) {
            var interp = interpretBits(msg.bits, 'msb');
            lines.push([
                msg.timestamp,
                msg.bit_count,
                interp.hex,
                '"' + interp.ascii.replace(/"/g, '""') + '"',
                msg.inverted,
            ].join(','));
        });
        var blob = new Blob([lines.join('\n')], { type: 'text/csv' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'ook_frames.csv';
        a.click();
        URL.revokeObjectURL(url);
    }

    // ---- Modulation selector ----

    function setEncoding(enc) {
        document.getElementById('ookEncoding').value = enc;

        // Update button highlight
        ['pwm', 'ppm', 'manchester'].forEach(function (e) {
            var btn = document.getElementById('ookEnc_' + e);
            if (!btn) return;
            if (e === enc) {
                btn.style.background = 'var(--accent)';
                btn.style.color = '#000';
            } else {
                btn.style.background = '';
                btn.style.color = '';
            }
        });

        // Update timing hint
        var hints = {
            pwm: 'Short pulse = 0, long pulse = 1. Most common for ISM OOK.',
            ppm: 'Short gap = 0, long gap = 1. Pulse position encoding.',
            manchester: 'Rising edge = 1, falling edge = 0. Self-clocking.',
        };
        var hint = document.getElementById('ookEncodingHint');
        if (hint) hint.textContent = hints[enc] || '';
    }

    function setFreq(mhz) {
        var el = document.getElementById('ookFrequency');
        if (el) el.value = mhz;
    }

    // ---- UI ----

    function updateUI(running) {
        var startBtn = document.getElementById('ookStartBtn');
        var stopBtn = document.getElementById('ookStopBtn');
        var indicator = document.getElementById('ookStatusIndicator');
        var statusText = document.getElementById('ookStatusText');

        if (startBtn) startBtn.style.display = running ? 'none' : '';
        if (stopBtn) stopBtn.style.display = running ? '' : 'none';
        if (indicator) indicator.style.background = running ? '#00ff88' : 'var(--text-dim)';
        if (statusText) statusText.textContent = running ? 'Listening' : 'Standby';

        var outputPanel = document.getElementById('ookOutputPanel');
        if (outputPanel) outputPanel.style.display = running ? 'block' : 'none';
    }

    // ---- Public API ----

    return {
        init: init,
        destroy: destroy,
        start: start,
        stop: stop,
        setFreq: setFreq,
        setEncoding: setEncoding,
        setBitOrder: setBitOrder,
        clearOutput: clearOutput,
        exportLog: exportLog,
    };
})();
