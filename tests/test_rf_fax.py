"""Tests for RF Fax / OOK bitmap decoder utilities."""

from __future__ import annotations

import threading

import pytest

from utils.rf_fax import FaxBitmapAssembler, crc8_xor, parse_fax_frame

# ---------------------------------------------------------------------------
# CRC8
# ---------------------------------------------------------------------------

class TestCrc8Xor:
    def test_empty_bytes(self):
        assert crc8_xor(b'') == 0

    def test_single_byte(self):
        assert crc8_xor(b'\x42') == 0x42

    def test_known_values(self):
        # XOR of 0x01, 0x02, 0x03 = 0x01^0x02^0x03 = 0x00
        assert crc8_xor(b'\x01\x02\x03') == 0

    def test_all_same_byte_even_count(self):
        # XOR of same byte twice cancels out
        assert crc8_xor(b'\xAB\xAB') == 0

    def test_all_same_byte_odd_count(self):
        assert crc8_xor(b'\xAB\xAB\xAB') == 0xAB


# ---------------------------------------------------------------------------
# Frame parser
# ---------------------------------------------------------------------------

def _build_frame(line_num: int, pixel_bytes: bytes) -> str:
    """Build a valid fax frame hex string."""
    width = len(pixel_bytes)
    payload = bytes([line_num, width]) + pixel_bytes
    crc = crc8_xor(payload)
    frame = bytes([0xA5, 0x5A]) + payload + bytes([crc])
    return frame.hex()


class TestParseFaxFrame:
    def test_valid_frame(self):
        pixel_data = bytes([0b10101010, 0b01010101])
        hex_str = _build_frame(3, pixel_data)
        result = parse_fax_frame(hex_str)
        assert result is not None
        assert result['line_num'] == 3
        assert result['width_bytes'] == 2
        assert result['width_pixels'] == 16
        assert result['crc_ok'] is True
        # Check pixel expansion: 0xAA = 10101010, 0x55 = 01010101
        expected = [1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0, 1]
        assert result['pixels'] == expected

    def test_bad_crc_returns_none(self):
        pixel_data = bytes([0xFF])
        # Build a frame with a deliberately wrong CRC
        payload = bytes([0, 1]) + pixel_data
        bad_crc = crc8_xor(payload) ^ 0xFF  # invert the correct CRC
        bad_frame = bytes([0xA5, 0x5A]) + payload + bytes([bad_crc])
        assert parse_fax_frame(bad_frame.hex()) is None

    def test_no_sync_returns_none(self):
        assert parse_fax_frame('deadbeef') is None

    def test_too_short_returns_none(self):
        # Just sync + 2 bytes (need at least sync + line + width + 1px + crc)
        assert parse_fax_frame('a55a0001') is None

    def test_empty_string_returns_none(self):
        assert parse_fax_frame('') is None

    def test_invalid_hex_returns_none(self):
        assert parse_fax_frame('not_hex_data') is None

    def test_bit_inverted_frame(self):
        """Verifies that the parser handles normal frames; bit-inversion
        is done by the caller (rf_fax_parser_thread), not parse_fax_frame."""
        pixel_data = bytes([0b11110000])
        hex_str = _build_frame(1, pixel_data)
        result = parse_fax_frame(hex_str)
        assert result is not None
        assert result['pixels'] == [1, 1, 1, 1, 0, 0, 0, 0]

    def test_frame_with_prefix_data(self):
        """Frame preceded by noise bytes — sync word should still be found."""
        pixel_data = bytes([0xAA])
        hex_str = 'deadbeef' + _build_frame(5, pixel_data)
        result = parse_fax_frame(hex_str)
        assert result is not None
        assert result['line_num'] == 5


# ---------------------------------------------------------------------------
# Bitmap assembler
# ---------------------------------------------------------------------------

class TestFaxBitmapAssembler:
    def test_add_lines_and_complete(self):
        asm = FaxBitmapAssembler(expected_lines=3)
        s0 = asm.add_line(0, [1, 0, 1])
        assert s0['lines_received'] == 1
        assert s0['complete'] is False

        s1 = asm.add_line(1, [0, 1, 0])
        assert s1['lines_received'] == 2
        assert s1['complete'] is False

        s2 = asm.add_line(2, [1, 1, 1])
        assert s2['lines_received'] == 3
        assert s2['complete'] is True
        assert s2['image_count'] == 1

    def test_get_bitmap_shape(self):
        asm = FaxBitmapAssembler(expected_lines=2)
        asm.add_line(0, [1, 0])
        asm.add_line(1, [0, 1])
        bitmap = asm.get_bitmap()
        assert bitmap is not None
        assert len(bitmap) == 2
        assert len(bitmap[0]) == 2

    def test_missing_line_padded(self):
        asm = FaxBitmapAssembler(expected_lines=3)
        asm.add_line(0, [1, 0])
        asm.add_line(2, [0, 1])
        bitmap = asm.get_bitmap()
        assert bitmap is not None
        assert len(bitmap) == 3
        # Line 1 missing — should be padded with zeros
        assert bitmap[1] == [0, 0]

    def test_reset_clears_state(self):
        asm = FaxBitmapAssembler(expected_lines=2)
        asm.add_line(0, [1])
        assert asm.lines_received == 1
        asm.reset()
        assert asm.lines_received == 0
        assert asm.get_bitmap() is None

    def test_duplicate_line_not_new(self):
        asm = FaxBitmapAssembler(expected_lines=2)
        s1 = asm.add_line(0, [1])
        assert s1['is_new_line'] is True
        s2 = asm.add_line(0, [0])  # overwrite
        assert s2['is_new_line'] is False
        assert s2['lines_received'] == 1  # still just 1 unique line

    def test_width_tracks_max(self):
        asm = FaxBitmapAssembler(expected_lines=3)
        asm.add_line(0, [1])
        assert asm.width_pixels == 1
        asm.add_line(1, [1, 0, 1, 0])
        assert asm.width_pixels == 4

    def test_thread_safety(self):
        asm = FaxBitmapAssembler(expected_lines=100)
        errors = []

        def add_lines(start, count):
            try:
                for i in range(start, start + count):
                    asm.add_line(i, [1, 0])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_lines, args=(i * 25, 25)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        assert asm.lines_received == 100


# ---------------------------------------------------------------------------
# Input validation (route-level)
# ---------------------------------------------------------------------------

class TestRfFaxValidation:
    """Tests that validate_positive_int is used correctly for timing params."""

    def test_validate_positive_int_rejects_negative(self):
        from utils.validation import validate_positive_int
        with pytest.raises(ValueError):
            validate_positive_int(-1, 'short_pulse')

    def test_validate_positive_int_rejects_over_max(self):
        from utils.validation import validate_positive_int
        with pytest.raises(ValueError):
            validate_positive_int(99999, 'short_pulse', max_val=10000)

    def test_validate_positive_int_accepts_valid(self):
        from utils.validation import validate_positive_int
        assert validate_positive_int(300, 'short_pulse', max_val=10000) == 300

    def test_validate_positive_int_accepts_string(self):
        from utils.validation import validate_positive_int
        assert validate_positive_int('600', 'long_pulse', max_val=20000) == 600

    def test_validate_positive_int_rejects_non_numeric(self):
        from utils.validation import validate_positive_int
        with pytest.raises(ValueError):
            validate_positive_int('abc', 'tolerance')
