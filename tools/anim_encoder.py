"""Encoder for the BUSY Bar 'bicycle1' animation container.

Layout follows the firmware's lib/anim_file/anim_file_format.h
(busy-app/busybar-firmware, identical at tags 1.4.1 and 1.5.1). The container
changed at firmware 1.3.0; 1.2.4 and earlier use 'bicycle0'. Firmware 1.4.1
accepts the upload and the draw of a 'bicycle0' file, then shows a placeholder.

Every display frame gets one record: a mask saying which pixels changed, then
those pixels. This encoder uses only the two trivial masks -- every pixel
present (a raw keyframe) or none present (identical to the previous frame) --
which is valid, far simpler than the run-length and QOI-like paths, and small
enough: a 24-frame 72x16 animation is ~83 KB. This is a build-time tool;
nothing in integrations/ imports it at runtime.
"""
from __future__ import annotations

import struct

SIGNATURE = b"bicycle1"
# signature, flags, width, height, color format, max mask length (bits),
# max pixel length (bytes), fps, unused, sections chunk length,
# frames chunk length, section count, frame count -- all little-endian.
_HEADER = struct.Struct("<8s BBBB HHBB II II")
_SECTION = struct.Struct("<III")   # first frame, last frame (inclusive), file offset
_FRAME = struct.Struct("<BHH")     # joint encoding, mask length (bits), pixel length (bytes)
HEADER_LENGTH = _HEADER.size

COLOR_BGR888 = 0
_KEYFRAME = (1 << 4) | 0   # mask: fully white (all pixels present) | pixels: raw
_UNCHANGED = (0 << 4) | 0  # mask: fully black (no pixels present)  | pixels: raw, empty
_MAX_U16 = 0xFFFF


def encode_anim(frames_bgr: list[bytes], width: int, height: int, fps: int,
                color_mode: str = "rgb888",
                sections: list[tuple[int, int, str]] | None = None) -> bytes:
    """Encode BGR888 frames; ``sections`` are extra ``(start, end, name)`` ranges."""
    if color_mode != "rgb888":
        raise ValueError("only color_mode 'rgb888' is supported")
    if not frames_bgr:
        raise ValueError("at least one frame required")
    if not (0 < width <= 255 and 0 < height <= 255 and 0 < fps <= 255):
        raise ValueError("width, height and fps must each fit one byte")
    expected = width * height * 3
    if expected > _MAX_U16:
        raise ValueError(f"a raw {width}x{height} frame exceeds the format's 65535-byte frame limit")
    for i, frame in enumerate(frames_bgr):
        if len(frame) != expected:
            raise ValueError(f"frame {i}: got {len(frame)} bytes, expected {expected}")

    count = len(frames_bgr)
    all_sections: list[tuple[int, int, str]] = [(0, count - 1, "default")]
    for start, end, name in (sections or []):
        if name == "default":
            raise ValueError('section name "default" is reserved')
        if not name or "\x00" in name:
            raise ValueError("section names must be non-empty and contain no NUL")
        if not 0 <= start <= end < count:
            raise ValueError(f"section {name!r} is outside the frame range")
        all_sections.append((start, end, name))
    # Playback can begin at any section, so its first frame must stand alone.
    keyframes = {start for start, _, _ in all_sections}

    records: list[bytes] = []
    for i, frame in enumerate(frames_bgr):
        if i not in keyframes and frame == frames_bgr[i - 1]:
            records.append(_FRAME.pack(_UNCHANGED, 0, 0))
        else:
            records.append(_FRAME.pack(_KEYFRAME, 0, expected) + frame)

    encoded_sections = [(start, end, name.encode() + b"\x00") for start, end, name in all_sections]
    sections_chunk_len = sum(_SECTION.size + len(name) for _, _, name in encoded_sections)

    offsets: list[int] = []
    offset = HEADER_LENGTH + sections_chunk_len
    for record in records:
        offsets.append(offset)
        offset += len(record)

    out = bytearray(_HEADER.pack(
        SIGNATURE, 0, width, height, COLOR_BGR888,
        0, expected, fps, 0,
        sections_chunk_len, sum(len(record) for record in records),
        len(encoded_sections), count,
    ))
    for start, end, name in encoded_sections:
        out += _SECTION.pack(start, end, offsets[start]) + name
    for record in records:
        out += record
    return bytes(out)


def parse_header(data: bytes) -> dict:
    if data[:8] != SIGNATURE:
        raise ValueError(f"not a bicycle1 animation (signature {bytes(data[:8])!r})")
    (magic, _flags, width, height, color_mode, max_mask_len, max_pixel_len, fps, _unused,
     sections_chunk_len, frames_chunk_len, n_sections, n_frames) = _HEADER.unpack_from(data)
    return {
        "magic": magic, "width": width, "height": height,
        "color_mode": color_mode, "fps": fps,
        "max_mask_len": max_mask_len, "max_pixel_len": max_pixel_len,
        "sections_chunk_len": sections_chunk_len, "frames_chunk_len": frames_chunk_len,
        "n_sections": n_sections, "n_frames": n_frames,
    }
