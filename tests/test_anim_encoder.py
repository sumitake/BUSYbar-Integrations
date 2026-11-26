import struct

import pytest

from tools.anim_encoder import HEADER_LENGTH, encode_anim, parse_header

RED_BGR  = bytes([0, 0, 255])   # BGR packing of RGB red
BLUE_BGR = bytes([255, 0, 0])   # BGR packing of RGB blue
FRAME_BYTES = 72 * 16 * 3
DEFAULT_SECTION_LEN = 12 + len("default") + 1   # three u32 + NUL-terminated name
KEYFRAME = 0x10   # mask "fully white" (every pixel present) | pixel encoding "raw"
UNCHANGED = 0x00  # mask "fully black" (no pixel present: same as previous frame)

# First 54 bytes of the stock spinner_front_8x8.anim shipped by firmware 1.4.1:
# header + its single "default" section. Pins parse_header to a real file.
STOCK_SPINNER_PREFIX = bytes.fromhex(
    "6269 6379 636c 6531 0008 0800 4000 b300 2100 1400 0000 e302 0000 0100 0000 1000"
    "0000 0000 0000 0f00 0000 3600 0000 6465 6661 756c 7400".replace(" ", ""))


def _solid(color: bytes, w=72, h=16) -> bytes:
    return color * (w * h)


def _frame_header(data: bytes, offset: int) -> tuple[int, int, int]:
    return struct.unpack_from("<BHH", data, offset)


def test_header_fields_and_counts():
    data = encode_anim([_solid(RED_BGR), _solid(BLUE_BGR)], 72, 16, fps=2)
    h = parse_header(data)
    assert h["magic"] == b"bicycle1"
    assert (h["width"], h["height"]) == (72, 16)
    assert h["color_mode"] == 0            # BGR888
    assert h["fps"] == 2
    assert h["n_frames"] == 2
    assert h["n_sections"] == 1            # the implicit "default" section
    assert h["max_pixel_len"] == FRAME_BYTES
    assert h["max_mask_len"] == 0          # no frame carries mask data


def test_layout_matches_the_firmware_structs():
    data = encode_anim([_solid(RED_BGR), _solid(BLUE_BGR)], 72, 16, fps=2)
    assert HEADER_LENGTH == 34
    h = parse_header(data)
    assert h["sections_chunk_len"] == DEFAULT_SECTION_LEN
    assert len(data) == HEADER_LENGTH + h["sections_chunk_len"] + h["frames_chunk_len"]
    start, end, frame_offs = struct.unpack_from("<III", data, HEADER_LENGTH)
    assert (start, end) == (0, 1)
    assert frame_offs == HEADER_LENGTH + DEFAULT_SECTION_LEN
    assert data[HEADER_LENGTH + 12:HEADER_LENGTH + DEFAULT_SECTION_LEN] == b"default\x00"


def test_first_frame_is_a_full_raw_keyframe():
    frame = _solid(RED_BGR)
    data = encode_anim([frame, _solid(BLUE_BGR)], 72, 16, fps=2)
    off = HEADER_LENGTH + DEFAULT_SECTION_LEN
    joint, mask_bits, pixel_len = _frame_header(data, off)
    assert (joint, mask_bits, pixel_len) == (KEYFRAME, 0, FRAME_BYTES)
    assert data[off + 5:off + 5 + pixel_len] == frame


def test_repeated_frame_is_stored_as_unchanged():
    data = encode_anim([_solid(RED_BGR)] * 2 + [_solid(BLUE_BGR)], 72, 16, fps=1)
    assert parse_header(data)["n_frames"] == 3      # every display frame has a record
    second = HEADER_LENGTH + DEFAULT_SECTION_LEN + 5 + FRAME_BYTES
    assert _frame_header(data, second) == (UNCHANGED, 0, 0)
    assert _frame_header(data, second + 5)[0] == KEYFRAME


def test_long_identical_run_stays_small():
    data = encode_anim([_solid(RED_BGR)] * 300, 72, 16, fps=1)
    assert parse_header(data)["n_frames"] == 300
    assert len(data) == HEADER_LENGTH + DEFAULT_SECTION_LEN + 5 + FRAME_BYTES + 299 * 5


def test_section_start_is_always_a_keyframe():
    # Playback may begin at any section, so its first frame cannot depend on a
    # predecessor even when the pixels are identical.
    data = encode_anim([_solid(RED_BGR)] * 3, 72, 16, fps=1, sections=[(1, 2, "tail")])
    h = parse_header(data)
    assert h["n_sections"] == 2
    frames_at = HEADER_LENGTH + h["sections_chunk_len"]
    second = frames_at + 5 + FRAME_BYTES
    assert _frame_header(data, second)[0] == KEYFRAME
    assert _frame_header(data, second + 5 + FRAME_BYTES)[0] == UNCHANGED
    tail = HEADER_LENGTH + DEFAULT_SECTION_LEN
    assert struct.unpack_from("<III", data, tail) == (1, 2, second)
    assert data[tail + 12:tail + 17] == b"tail\x00"


def test_parse_header_reads_a_stock_firmware_file():
    h = parse_header(STOCK_SPINNER_PREFIX)
    assert (h["width"], h["height"], h["fps"]) == (8, 8, 33)
    assert (h["n_sections"], h["n_frames"]) == (1, 16)
    assert (h["max_mask_len"], h["max_pixel_len"]) == (64, 179)
    assert h["sections_chunk_len"] == 20 and h["frames_chunk_len"] == 739


def test_parse_header_rejects_the_retired_container():
    with pytest.raises(ValueError, match="bicycle1"):
        parse_header(b"bicycle0" + bytes(40))


@pytest.mark.parametrize("kwargs", [
    {"frames": [], "width": 72, "height": 16},
    {"frames": [b"\x00" * 10], "width": 72, "height": 16},          # wrong frame size
    {"frames": [bytes(200 * 120 * 3)], "width": 200, "height": 120},  # exceeds u16 length
])
def test_invalid_input_is_rejected(kwargs):
    with pytest.raises(ValueError):
        encode_anim(kwargs["frames"], kwargs["width"], kwargs["height"], fps=1)


def test_reserved_and_out_of_range_sections_are_rejected():
    frames = [_solid(RED_BGR)] * 2
    with pytest.raises(ValueError):
        encode_anim(frames, 72, 16, fps=1, sections=[(0, 1, "default")])
    with pytest.raises(ValueError):
        encode_anim(frames, 72, 16, fps=1, sections=[(1, 2, "past-the-end")])
    for bad_name in ("", "a\x00b"):   # names are NUL-terminated in the file
        with pytest.raises(ValueError):
            encode_anim(frames, 72, 16, fps=1, sections=[(0, 1, bad_name)])
