"""RF Fax / OOK bitmap decoder routes.

Captures OOK PWM fax frames at 433 MHz using rtl_433 with a custom
flex decoder, parses scan lines, and streams decoded pixel data to
the browser for real-time bitmap reconstruction.
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
from utils.process import register_process, safe_terminate, unregister_process
from utils.rf_fax import rf_fax_parser_thread
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

rf_fax_bp = Blueprint('rf_fax', __name__)

# Track which device is being used
rf_fax_active_device: int | None = None


@rf_fax_bp.route('/rf_fax/start', methods=['POST'])
def start_rf_fax() -> Response:
    global rf_fax_active_device

    with app_module.rf_fax_lock:
        if app_module.rf_fax_process:
            return jsonify({'status': 'error', 'message': 'RF Fax decoder already running'}), 409

        data = request.json or {}

        # Validate standard SDR inputs
        try:
            freq = validate_frequency(data.get('frequency', '433.400'))
            gain = validate_gain(data.get('gain', '0'))
            ppm = validate_ppm(data.get('ppm', '0'))
            device = validate_device_index(data.get('device', '0'))
        except ValueError as e:
            return jsonify({'status': 'error', 'message': str(e)}), 400

        # Check for rtl_tcp (remote SDR) connection
        rtl_tcp_host = data.get('rtl_tcp_host') or None
        rtl_tcp_port = data.get('rtl_tcp_port', 1234)

        # OOK PWM timing parameters (defaults match Flag 7 transmitter)
        short_pulse = int(data.get('short_pulse', 300))
        long_pulse = int(data.get('long_pulse', 600))
        reset_limit = int(data.get('reset_limit', 8000))
        gap_limit = int(data.get('gap_limit', 5000))
        tolerance = int(data.get('tolerance', 150))
        min_bits = int(data.get('min_bits', 64))
        expected_lines = int(data.get('expected_lines', 11))
        waterfall = bool(data.get('waterfall', False))

        # Claim local device only if not using remote rtl_tcp
        if not rtl_tcp_host:
            device_int = int(device)
            error = app_module.claim_sdr_device(device_int, 'rf_fax')
            if error:
                return jsonify({
                    'status': 'error',
                    'error_type': 'DEVICE_BUSY',
                    'message': error,
                }), 409
            rf_fax_active_device = device_int

        # Clear queue
        while not app_module.rf_fax_queue.empty():
            try:
                app_module.rf_fax_queue.get_nowait()
            except queue.Empty:
                break

        # Build rtl_433 command with custom flex decoder
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
            logger.info(f"Using remote SDR: rtl_tcp://{rtl_tcp_host}:{rtl_tcp_port}")
        else:
            sdr_device = SDRFactory.create_default_device(sdr_type, index=device)
        builder = SDRFactory.get_builder(sdr_device.sdr_type)

        bias_t = data.get('bias_t', False)

        # Build base ISM command, then customize for fax flex decoder
        cmd = builder.build_ism_command(
            device=sdr_device,
            frequency_mhz=freq,
            gain=float(gain) if gain and gain != '0' else None,
            ppm=int(ppm) if ppm and ppm != '0' else None,
            bias_t=bias_t,
        )

        # Replace auto-detect protocols with our custom flex decoder
        # Remove any existing -R flags and add -R 0 (disable all)
        # Then add -X with our OOK PWM flex decoder spec
        flex_spec = (
            f'n=fax,m=OOK_PWM,'
            f's={short_pulse},l={long_pulse},'
            f'r={reset_limit},g={gap_limit},'
            f't={tolerance},bits>={min_bits}'
        )

        # Filter out any existing -R flags from base command
        filtered_cmd = []
        skip_next = False
        for i, arg in enumerate(cmd):
            if skip_next:
                skip_next = False
                continue
            if arg == '-R':
                skip_next = True
                continue
            filtered_cmd.append(arg)

        filtered_cmd.extend(['-R', '0', '-X', flex_spec])

        full_cmd = ' '.join(filtered_cmd)
        logger.info(f"RF Fax decoder running: {full_cmd}")

        try:
            rtl_process = subprocess.Popen(
                filtered_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            register_process(rtl_process)

            # Monitor rtl_433 stderr
            _stderr_noise = ('bitbuffer_add_bit', 'row count limit')

            def monitor_stderr():
                for line in rtl_process.stderr:
                    err_text = line.decode('utf-8', errors='replace').strip()
                    if err_text and not any(n in err_text for n in _stderr_noise):
                        logger.debug(f"[rtl_433/rf_fax] {err_text}")

            stderr_thread = threading.Thread(target=monitor_stderr)
            stderr_thread.daemon = True
            stderr_thread.start()

            # Start parser thread
            stop_event = threading.Event()
            parser_thread = threading.Thread(
                target=rf_fax_parser_thread,
                args=(
                    rtl_process.stdout,
                    app_module.rf_fax_queue,
                    stop_event,
                    expected_lines,
                    waterfall,
                ),
            )
            parser_thread.daemon = True
            parser_thread.start()

            app_module.rf_fax_process = rtl_process
            app_module.rf_fax_process._stop_parser = stop_event
            app_module.rf_fax_process._parser_thread = parser_thread

            app_module.rf_fax_queue.put({'type': 'status', 'status': 'started'})

            return jsonify({
                'status': 'started',
                'command': full_cmd,
                'flex_spec': flex_spec,
                'expected_lines': expected_lines,
            })

        except FileNotFoundError as e:
            if rf_fax_active_device is not None:
                app_module.release_sdr_device(rf_fax_active_device)
                rf_fax_active_device = None
            return jsonify({'status': 'error', 'message': f'Tool not found: {e.filename}'}), 400

        except Exception as e:
            try:
                rtl_process.terminate()
                rtl_process.wait(timeout=2)
            except Exception:
                with contextlib.suppress(Exception):
                    rtl_process.kill()
            unregister_process(rtl_process)
            if rf_fax_active_device is not None:
                app_module.release_sdr_device(rf_fax_active_device)
                rf_fax_active_device = None
            return jsonify({'status': 'error', 'message': str(e)}), 500


@rf_fax_bp.route('/rf_fax/stop', methods=['POST'])
def stop_rf_fax() -> Response:
    global rf_fax_active_device

    with app_module.rf_fax_lock:
        if app_module.rf_fax_process:
            # Signal parser thread to stop
            stop_event = getattr(app_module.rf_fax_process, '_stop_parser', None)
            if stop_event:
                stop_event.set()

            safe_terminate(app_module.rf_fax_process)
            unregister_process(app_module.rf_fax_process)
            app_module.rf_fax_process = None

            if rf_fax_active_device is not None:
                app_module.release_sdr_device(rf_fax_active_device)
                rf_fax_active_device = None

            app_module.rf_fax_queue.put({'type': 'status', 'status': 'stopped'})
            return jsonify({'status': 'stopped'})

        return jsonify({'status': 'not_running'})


@rf_fax_bp.route('/rf_fax/status')
def rf_fax_status() -> Response:
    with app_module.rf_fax_lock:
        running = (
            app_module.rf_fax_process is not None
            and app_module.rf_fax_process.poll() is None
        )
        return jsonify({'running': running})


@rf_fax_bp.route('/rf_fax/stream')
def rf_fax_stream() -> Response:
    def _on_msg(msg: dict[str, Any]) -> None:
        process_event('rf_fax', msg, msg.get('type'))

    response = Response(
        sse_stream_fanout(
            source_queue=app_module.rf_fax_queue,
            channel_key='rf_fax',
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
