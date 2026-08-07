import struct
from tools.anim_encoder import encode_anim, parse_header

RED_BGR  = bytes([0, 0, 255])   # BGR packing of RGB red
BLUE_BGR = bytes([255, 0, 0])   # BGR packing of RGB blue

def _solid(color: bytes, w=72, h=16) -> bytes:
    return color * (w * h)

def test_header_fields_and_counts():
    data = encode_anim([_solid(RED_BGR), _solid(BLUE_BGR)], 72, 16, fps=2)
    h = parse_header(data)
    assert h["magic"] == b"bicycle0"
    assert (h["width"], h["height"]) == (72, 16)
    assert h["color_mode"] == 0            # rgb888
    assert h["fps"] == 2
    assert h["n_display"] == 2             # two display frames
    assert h["n_encoded"] == 2             # two distinct encoded frames
    assert h["n_sections"] == 1            # the implicit "default" section

def test_consecutive_identical_frames_dedup():
    data = encode_anim([_solid(RED_BGR)] * 3, 72, 16, fps=1)
    h = parse_header(data)
    assert h["n_display"] == 3             # three display frames...
    assert h["n_encoded"] == 1             # ...collapsed to one encoded frame

def test_first_frame_pixels_roundtrip():
    # The first raw frame's bytes must be the exact BGR payload we passed in.
    frame = _solid(RED_BGR)
    data = encode_anim([frame, _solid(BLUE_BGR)], 72, 16, fps=2)
    # frames start after header(36) + sections chunk; the default section is
    # 13 + len("default") + 1 = 21 bytes -> frames at offset 57.
    off = 36 + 21
    encoding, duration, length = data[off], data[off+1], struct.unpack_from("<H", data, off+2)[0]
    payload = data[off+4:off+4+length]
    assert encoding == 0 and length == 72*16*3
    assert payload == frame
