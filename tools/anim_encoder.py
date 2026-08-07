"""Encoder for the BUSY Bar 'bicycle0' animation container.

Ported from the firmware web draw-tool's assets/frontend/util/seq2anim.ts
(busy-app/busybar-firmware). Uses only the raw-frame path (encoding=0): the
format also defines an RLE path, but raw frames are valid and far simpler, and
a 24-frame 72x16 animation is ~83 KB raw (stock anims run to ~2 MB). This is a
build-time tool; nothing in integrations/ imports it at runtime.
"""
from __future__ import annotations

import struct

HEADER_LENGTH = 36


def encode_anim(frames_bgr: list[bytes], width: int, height: int, fps: int,
                color_mode: str = "rgb888",
                sections: list[tuple[int, int, str]] | None = None) -> bytes:
    if not frames_bgr:
        raise ValueError("at least one frame required")
    expected = width * height * (3 if color_mode == "rgb888" else 1)
    for i, f in enumerate(frames_bgr):
        if len(f) != expected:
            raise ValueError(f"frame {i}: got {len(f)} bytes, expected {expected}")

    # Collapse consecutive identical frames into one encoded frame (duration++).
    # `duration` (and a section's duration_override) is serialized as a single
    # byte, so a run of >255 identical frames must be split into chunks of at
    # most 255 -- otherwise `bytes([duration])` raises ValueError. Splitting is
    # transparent: each chunk is another raw frame with the same pixel data.
    enc: list[list] = []  # [encoding, duration, data]
    last = None
    for f in frames_bgr:
        if last is not None and f == last and enc[-1][1] < 255:
            enc[-1][1] += 1
            continue
        last = f
        enc.append([0, 1, f])  # encoding=0 (raw), duration=1

    frames_chunk_len = sum(4 + len(e[2]) for e in enc)
    max_encoded_len = max(len(e[2]) for e in enc)

    n = len(frames_bgr)
    all_sections: list[tuple[int, int, str]] = [(0, n - 1, "default")]
    for s in (sections or []):
        if s[2] == "default":
            raise ValueError('section name "default" is reserved')
        all_sections.append(s)
    sections_chunk_len = sum(13 + len(name.encode()) + 1 for _, _, name in all_sections)

    # Map each display-frame index -> (byte offset of its encoded frame, remaining duration).
    disp: list[tuple[int, int]] = []
    off = HEADER_LENGTH + sections_chunk_len
    for _, dur, data in enc:
        for d in range(dur, 0, -1):
            disp.append((off, d))
        off += 4 + len(data)

    out = bytearray(b"bicycle0")
    out += bytes([0, width, height, 0 if color_mode == "rgb888" else 1])
    out += bytes([fps])
    out += struct.pack("<H", max_encoded_len)
    out += bytes([0])  # padding
    out += struct.pack("<IIIII", sections_chunk_len, frames_chunk_len,
                       len(all_sections), len(enc), n)
    assert len(out) == HEADER_LENGTH, len(out)

    for start, end, name in all_sections:
        frame_offs, duration_override = disp[start]
        out += struct.pack("<III", start, end, frame_offs)
        out += bytes([duration_override]) + name.encode() + b"\x00"

    for encoding, duration, data in enc:
        out += bytes([encoding, duration]) + struct.pack("<H", len(data)) + data

    return bytes(out)


def parse_header(data: bytes) -> dict:
    if data[:8] != b"bicycle0":
        raise ValueError("bad magic")
    scl, fcl, n_sections, n_encoded, n_display = struct.unpack_from("<IIIII", data, 16)
    return {
        "magic": data[:8], "width": data[9], "height": data[10],
        "color_mode": data[11], "fps": data[12],
        "n_sections": n_sections, "n_encoded": n_encoded, "n_display": n_display,
    }
