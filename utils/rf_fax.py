"""RF Fax / OOK bitmap decoder utilities.

Parses OOK PWM fax frames captured by rtl_433 flex decoder.
Each frame represents one scan line of a monochrome bitmap image:

  [SYNC: A5 5A] [LINE_NUM: 1B] [WIDTH: 1B] [PIXEL_DATA: N bytes] [CRC8: 1B]

CRC8 is XOR of LINE_NUM, WIDTH, and all PIXEL_DATA bytes.

The decoder accumulates scan lines and assembles them into a complete
bitmap that can be rendered as an image in the browser.

Designed for 433 MHz OOK PWM signals (inverted ratio: long=1, short=0).
Decode with rtl_433:
  rtl_433 -f 433400000 -R 0 -X "n=fax,m=OOK_PWM,s=300,l=600,r=8000,g=5000,t=150,bits>=64"
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger('intercept.rf_fax')

# Sync word bytes
SYNC_WORD = bytes([0xA5, 0x5A])


def crc8_xor(data: bytes) -> int:
    """Compute CRC8 as XOR of all bytes."""
    crc = 0
    for b in data:
        crc ^= b
    return crc


def parse_fax_frame(hex_data: str) -> dict[str, Any] | None:
    """Parse a fax frame from hex string (as output by rtl_433).

    Args:
        hex_data: Hex string from rtl_433 codes field, e.g. "a55a000b..."

    Returns:
        Dict with line_num, width, pixels (list of 0/1), pixel_bytes (hex),
        or None if invalid.
    """
    try:
        raw = bytes.fromhex(hex_data.replace(' ', ''))
    except ValueError:
        return None

    # Find sync word
    idx = raw.find(SYNC_WORD)
    if idx < 0:
        return None

    # Need at least: sync(2) + line_num(1) + width(1) + 1 pixel byte + crc(1)
    payload = raw[idx + 2:]
    if len(payload) < 4:
        return None

    line_num = payload[0]
    width_bytes = payload[1]

    if width_bytes == 0 or len(payload) < 2 + width_bytes + 1:
        return None

    pixel_data = payload[2:2 + width_bytes]
    crc_received = payload[2 + width_bytes]

    # Validate CRC
    crc_computed = crc8_xor(bytes([line_num, width_bytes]) + pixel_data)
    if crc_received != crc_computed:
        logger.debug(
            f"CRC mismatch line {line_num}: got 0x{crc_received:02x}, "
            f"expected 0x{crc_computed:02x}"
        )
        return None

    # Expand pixel bytes to bit array
    pixels: list[int] = []
    for byte_val in pixel_data:
        for bit_pos in range(7, -1, -1):
            pixels.append((byte_val >> bit_pos) & 1)

    return {
        'line_num': line_num,
        'width_bytes': width_bytes,
        'width_pixels': len(pixels),
        'pixels': pixels,
        'pixel_hex': pixel_data.hex(),
        'crc_ok': True,
    }


class FaxBitmapAssembler:
    """Accumulates scan lines and tracks image completion.

    Thread-safe: guards internal state with a lock so the SSE stream
    thread can read the assembled bitmap while rtl_433 output is being
    parsed on the ingest thread.
    """

    def __init__(self, expected_lines: int = 11):
        self.expected_lines = expected_lines
        self._lock = threading.Lock()
        self._lines: dict[int, list[int]] = {}  # line_num -> pixel list
        self._width_pixels: int = 0
        self._complete_count: int = 0  # how many full images received
        self._last_complete_bitmap: list[list[int]] | None = None

    def add_line(self, line_num: int, pixels: list[int]) -> dict[str, Any]:
        """Add a scan line. Returns status dict with completion info."""
        with self._lock:
            is_new = line_num not in self._lines
            self._lines[line_num] = pixels
            if len(pixels) > self._width_pixels:
                self._width_pixels = len(pixels)

            received = len(self._lines)
            complete = received >= self.expected_lines

            if complete and is_new:
                # Just completed a full image
                self._complete_count += 1
                self._last_complete_bitmap = self._get_bitmap_unlocked()

            return {
                'lines_received': received,
                'lines_expected': self.expected_lines,
                'complete': complete,
                'image_count': self._complete_count,
                'is_new_line': is_new,
            }

    def get_bitmap(self) -> list[list[int]] | None:
        """Get current bitmap as list of pixel rows, or None if empty."""
        with self._lock:
            return self._get_bitmap_unlocked()

    def _get_bitmap_unlocked(self) -> list[list[int]] | None:
        if not self._lines:
            return None
        # Use actual received line range, not a fixed expected count
        max_line = max(self._lines.keys())
        bitmap: list[list[int]] = []
        for i in range(max_line + 1):
            if i in self._lines:
                row = self._lines[i]
                if len(row) < self._width_pixels:
                    row = row + [0] * (self._width_pixels - len(row))
                bitmap.append(row)
            else:
                bitmap.append([0] * self._width_pixels)
        return bitmap

    def get_last_complete(self) -> list[list[int]] | None:
        """Get last fully completed bitmap."""
        with self._lock:
            return self._last_complete_bitmap

    def reset(self) -> None:
        """Clear all accumulated data."""
        with self._lock:
            self._lines.clear()
            self._width_pixels = 0

    @property
    def width_pixels(self) -> int:
        with self._lock:
            return self._width_pixels

    @property
    def lines_received(self) -> int:
        with self._lock:
            return len(self._lines)


def rf_fax_parser_thread(
    rtl_stdout,
    output_queue: queue.Queue,
    stop_event: threading.Event,
    expected_lines: int = 11,
    waterfall: bool = False,
) -> None:
    """Thread function: reads rtl_433 JSON output, parses fax frames.

    Reads JSON lines from rtl_433 stdout, extracts hex data from the
    'codes' field, parses fax frames, and pushes events to output_queue.

    Args:
        rtl_stdout: rtl_433 stdout pipe.
        output_queue: Queue for SSE events.
        stop_event: Threading event to signal shutdown.
        expected_lines: Expected lines per frame (frame mode only).
        waterfall: If True, ignore frame line_num and assign sequential
            row indices instead. Every received frame appends a new row,
            producing an endless scroll. If False (default), frame
            line_num is the row index and retransmissions overwrite.

    Events emitted:
      type='rf_fax_line'  — decoded scan line with pixel data
      type='rf_fax_image' — complete bitmap when all lines received
      type='rf_fax_raw'   — raw rtl_433 JSON for debugging
      type='status'       — start/stop notifications
      type='error'        — error messages
    """
    assembler = FaxBitmapAssembler(expected_lines=expected_lines)

    # Monotonically increasing row index for waterfall mode.
    # Defined here in the same function scope — not a closure —
    # so augmented assignment (+=) works without `nonlocal`.
    _waterfall_counter = 0

    try:
        for line in iter(rtl_stdout.readline, b''):
            if stop_event.is_set():
                break

            text = line.decode('utf-8', errors='replace').strip()
            if not text:
                continue

            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Non-JSON output from rtl_433 (startup messages, etc.)
                logger.debug(f"[rtl_433/rf_fax] {text}")
                continue

            # rtl_433 flex decoder puts hex in 'codes' (list or string),
            # 'code' (singular), or 'data' depending on version.
            codes = data.get('codes')
            if codes is not None and isinstance(codes, str):
                # Normalize: some versions emit a bare string, not a list
                codes = [codes] if codes else None

            if not codes:
                # Some rtl_433 versions use 'code' (singular)
                code = data.get('code')
                if code:
                    codes = [str(code)]

            if not codes:
                # Newer rtl_433 flex output uses 'data' directly
                raw_data = data.get('data')
                if raw_data:
                    codes = [str(raw_data)]

            if not codes:
                # Log actual JSON keys for debugging during live testing
                logger.debug(
                    f"[rtl_433/rf_fax] no code field — keys: {list(data.keys())}"
                )
                # Push raw event for non-fax data
                with contextlib.suppress(queue.Full):
                    output_queue.put_nowait({
                        'type': 'rf_fax_raw',
                        'data': data,
                        'timestamp': datetime.now().strftime('%H:%M:%S'),
                    })
                continue

            # Try each code entry (usually just one)
            for code_hex in codes:
                # rtl_433 may prefix with {bits} or wrap in braces
                hex_str = str(code_hex).strip()
                # Strip leading {N} bit count if present
                if hex_str.startswith('{'):
                    brace_end = hex_str.find('}')
                    if brace_end >= 0:
                        hex_str = hex_str[brace_end + 1:]

                frame = parse_fax_frame(hex_str)
                if frame is None:
                    # rtl_433 OOK_PWM maps short→'1', but some transmitters
                    # use long→'1' (inverted ratio). Try bit-flipping the hex.
                    try:
                        inv = bytes(b ^ 0xFF for b in bytes.fromhex(hex_str.replace(' ', '')))
                        frame = parse_fax_frame(inv.hex())
                    except ValueError:
                        pass
                if frame is None:
                    continue

                timestamp = datetime.now().strftime('%H:%M:%S')

                # In waterfall mode substitute a monotonic counter so every
                # received frame gets a unique row slot (no overwrites).
                if waterfall:
                    row_index = _waterfall_counter
                    _waterfall_counter += 1
                else:
                    row_index = frame['line_num']

                # Add line to assembler
                status = assembler.add_line(row_index, frame['pixels'])

                # Emit line event
                with contextlib.suppress(queue.Full):
                    output_queue.put_nowait({
                        'type': 'rf_fax_line',
                        'line_num': row_index,
                        'width_pixels': frame['width_pixels'],
                        'pixels': frame['pixels'],
                        'pixel_hex': frame['pixel_hex'],
                        'lines_received': status['lines_received'],
                        'lines_expected': status['lines_expected'],
                        'is_new': status['is_new_line'],
                        'timestamp': timestamp,
                    })

                # No auto-reset: accumulate all lines for the session duration.
                # Progressive rendering is driven entirely by rf_fax_line events.

    except Exception as e:
        logger.debug(f"RF Fax parser thread error: {e}")
        with contextlib.suppress(queue.Full):
            output_queue.put_nowait({
                'type': 'error',
                'text': str(e),
            })
