"""Generic OOK signal decoder routes.

Captures raw OOK frames using rtl_433's flex decoder and streams decoded
bit/hex data to the browser for live ASCII interpretation.  Supports
PWM, PPM, and Manchester modulation with fully configurable pulse timing.
"""

from __future__ import annotations

import contextlib
import queue
import subprocess
import threading
from typing import Any

from flask import Blueprint, Response, jsonify, request

import app as app_module
from utils.event_pipeline import process_event
from utils.logging import sensor_logger as logger
from utils.ook import ook_parser_thread
from utils.process import register_process, safe_terminate, unregister_process
from utils.sdr import SDRFactory, SDRType
from utils.sse import sse_stream_fanout
from utils.validation import (
    validate_device_index,
    validate_frequency,
    validate_gain,
    validate_ppm,
    validate_rtl_tcp_host,
    validate_rtl_tcp_port,
)

ook_bp = Blueprint('ook', __name__)

# Track which device is being used
ook_active_device: int | None = None

# Supported modulation schemes → rtl_433 flex decoder modulation string
_MODULATION_MAP = {
    'pwm': 'OOK_PWM',
    'ppm': 'OOK_PPM',
    'manchester': 'OOK_MC_ZEROBIT',
}


def _validate_encoding(value: Any) -> str:
    enc = str(value).lower().strip()
    if enc not in _MODULATION_MAP:
        raise ValueError(f"encoding must be one of: {', '.join(_MODULATION_MAP)}")
    return enc


@ook_bp.route('/ook/start', methods=['POST'])
def start_ook() -> Response:
    global ook_active_device

    with app_module.ook_lock:
        if app_module.ook_process:
            return jsonify({'status': 'error', 'message': 'OOK decoder already running'}), 409

        data = request.json or {}

        try:
            freq = validate_frequency(data.get('frequency', '433.920'))
            gain = validate_gain(data.get('gain', '0'))
            ppm = validate_ppm(data.get('ppm', '0'))
            device = validate_device_index(data.get('device', '0'))
        except ValueError as e:
            return jsonify({'status': 'error', 'message': str(e)}), 400

        try:
            encoding = _validate_encoding(data.get('encoding', 'pwm'))
        except ValueError as e:
            return jsonify({'status': 'error', 'message': str(e)}), 400

        # OOK flex decoder timing parameters
        short_pulse = int(data.get('short_pulse', 300))
        long_pulse = int(data.get('long_pulse', 600))
        reset_limit = int(data.get('reset_limit', 8000))
        gap_limit = int(data.get('gap_limit', 5000))
        tolerance = int(data.get('tolerance', 150))
        min_bits = int(data.get('min_bits', 8))
        deduplicate = bool(data.get('deduplicate', False))

        rtl_tcp_host = data.get('rtl_tcp_host') or None
        rtl_tcp_port = data.get('rtl_tcp_port', 1234)

        if not rtl_tcp_host:
            device_int = int(device)
            error = app_module.claim_sdr_device(device_int, 'ook')
            if error:
                return jsonify({
                    'status': 'error',
                    'error_type': 'DEVICE_BUSY',
                    'message': error,
                }), 409
            ook_active_device = device_int

        while not app_module.ook_queue.empty():
            try:
                app_module.ook_queue.get_nowait()
            except queue.Empty:
                break

        sdr_type_str = data.get('sdr_type', 'rtlsdr')
        try:
            sdr_type = SDRType(sdr_type_str)
        except ValueError:
            sdr_type = SDRType.RTL_SDR

        if rtl_tcp_host:
            try:
                rtl_tcp_host = validate_rtl_tcp_host(rtl_tcp_host)
                rtl_tcp_port = validate_rtl_tcp_port(rtl_tcp_port)
            except ValueError as e:
                return jsonify({'status': 'error', 'message': str(e)}), 400
            sdr_device = SDRFactory.create_network_device(rtl_tcp_host, rtl_tcp_port)
            logger.info(f'Using remote SDR: rtl_tcp://{rtl_tcp_host}:{rtl_tcp_port}')
        else:
            sdr_device = SDRFactory.create_default_device(sdr_type, index=device)

        builder = SDRFactory.get_builder(sdr_device.sdr_type)
        bias_t = data.get('bias_t', False)

        # Build base ISM command then replace protocol flags with flex decoder
        cmd = builder.build_ism_command(
            device=sdr_device,
            frequency_mhz=freq,
            gain=float(gain) if gain and gain != '0' else None,
            ppm=int(ppm) if ppm and ppm != '0' else None,
            bias_t=bias_t,
        )

        modulation = _MODULATION_MAP[encoding]
        flex_spec = (
            f'n=ook,m={modulation},'
            f's={short_pulse},l={long_pulse},'
            f'r={reset_limit},g={gap_limit},'
            f't={tolerance},bits>={min_bits}'
        )

        # Strip any existing -R flags from the base command
        filtered_cmd: list[str] = []
        skip_next = False
        for arg in cmd:
            if skip_next:
                skip_next = False
                continue
            if arg == '-R':
                skip_next = True
                continue
            filtered_cmd.append(arg)

        filtered_cmd.extend(['-M', 'level', '-R', '0', '-X', flex_spec])

        full_cmd = ' '.join(filtered_cmd)
        logger.info(f'OOK decoder running: {full_cmd}')

        try:
            rtl_process = subprocess.Popen(
                filtered_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            register_process(rtl_process)

            _stderr_noise = ('bitbuffer_add_bit', 'row count limit')

            def monitor_stderr() -> None:
                for line in rtl_process.stderr:
                    err_text = line.decode('utf-8', errors='replace').strip()
                    if err_text and not any(n in err_text for n in _stderr_noise):
                        logger.debug(f'[rtl_433/ook] {err_text}')

            stderr_thread = threading.Thread(target=monitor_stderr)
            stderr_thread.daemon = True
            stderr_thread.start()

            stop_event = threading.Event()
            parser_thread = threading.Thread(
                target=ook_parser_thread,
                args=(
                    rtl_process.stdout,
                    app_module.ook_queue,
                    stop_event,
                    encoding,
                    deduplicate,
                ),
            )
            parser_thread.daemon = True
            parser_thread.start()

            app_module.ook_process = rtl_process
            app_module.ook_process._stop_parser = stop_event
            app_module.ook_process._parser_thread = parser_thread

            app_module.ook_queue.put({'type': 'status', 'status': 'started'})

            return jsonify({
                'status': 'started',
                'command': full_cmd,
                'encoding': encoding,
                'modulation': modulation,
                'flex_spec': flex_spec,
                'deduplicate': deduplicate,
            })

        except FileNotFoundError as e:
            if ook_active_device is not None:
                app_module.release_sdr_device(ook_active_device)
                ook_active_device = None
            return jsonify({'status': 'error', 'message': f'Tool not found: {e.filename}'}), 400

        except Exception as e:
            try:
                rtl_process.terminate()
                rtl_process.wait(timeout=2)
            except Exception:
                with contextlib.suppress(Exception):
                    rtl_process.kill()
            unregister_process(rtl_process)
            if ook_active_device is not None:
                app_module.release_sdr_device(ook_active_device)
                ook_active_device = None
            return jsonify({'status': 'error', 'message': str(e)}), 500


@ook_bp.route('/ook/stop', methods=['POST'])
def stop_ook() -> Response:
    global ook_active_device

    with app_module.ook_lock:
        if app_module.ook_process:
            stop_event = getattr(app_module.ook_process, '_stop_parser', None)
            if stop_event:
                stop_event.set()

            safe_terminate(app_module.ook_process)
            unregister_process(app_module.ook_process)
            app_module.ook_process = None

            if ook_active_device is not None:
                app_module.release_sdr_device(ook_active_device)
                ook_active_device = None

            app_module.ook_queue.put({'type': 'status', 'status': 'stopped'})
            return jsonify({'status': 'stopped'})

        return jsonify({'status': 'not_running'})


@ook_bp.route('/ook/status')
def ook_status() -> Response:
    with app_module.ook_lock:
        running = (
            app_module.ook_process is not None
            and app_module.ook_process.poll() is None
        )
        return jsonify({'running': running})


@ook_bp.route('/ook/stream')
def ook_stream() -> Response:
    def _on_msg(msg: dict[str, Any]) -> None:
        process_event('ook', msg, msg.get('type'))

    response = Response(
        sse_stream_fanout(
            source_queue=app_module.ook_queue,
            channel_key='ook',
            timeout=1.0,
            keepalive_interval=30.0,
            on_message=_on_msg,
        ),
        mimetype='text/event-stream',
    )
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response
