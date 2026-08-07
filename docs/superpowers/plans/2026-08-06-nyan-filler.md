# Nyan Cat Dark-Filler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `nyan_filler` integration that draws a native, on-device Nyan Cat animation at a low priority to fill the panel whenever it would otherwise be black (CI dwell gaps and idle), except during quiet hours.

**Architecture:** A third self-contained integration (`integrations/nyan_filler/`, pure `logic.py` + thin `main.py`) mirroring the existing two. It draws an `AnimationElement` at a new `PRIORITY_FILLER = 5` — above the empty/stub screen (priority 0) but below built-in apps (10) and every other tier — so the firmware's existing priority arbitration makes it fill black gaps, never override a built-in app, and yield to calendar/CI/alerts/sessions. The animation is a native `.anim` (`bicycle0`) generated at build time by an in-repo encoder ported from the firmware web draw-tool's `seq2anim.ts`; the device self-loops it, so the host only re-asserts one small draw per poll.

**Tech Stack:** Python ≥3.12, stdlib + `requests` (runtime); Pillow + the in-repo encoder (dev/build tools only); launchd; pytest.

**Reference spec:** `docs/superpowers/specs/2026-08-06-nyan-filler-design.md` (all four on-device spikes resolved in §5b).

## Global Constraints

- **Public repo — sanitize everything.** Never print `config.toml` (it holds personal data + the cloud token).
- **Runtime deps = stdlib + `BusyBarClient` only.** Pillow and the `.anim` encoder are dev/build tools under `tools/`, never imported by `integrations/nyan_filler/` at runtime.
- **`PRIORITY_FILLER = 5`** — exact value; ordering `0 < 5 < PRIORITY_AMBIENT (20)` (verified on-device: black gaps rest at priority 0, built-in apps at 10).
- **Config defaults (exact):** `enabled = true`, `poll_seconds = 1`, `quiet_hours = "00:00-07:00"`.
- **App name = `"nyan_filler"`; asset name = `"nyan_72x16.anim"`; element id = `"nyan"`.**
- **`.anim` format (`bicycle0`):** 36-byte header, `default` section, raw (`encoding=0`) frames, pixels packed **BGR**. Color mode `rgb888` = 0.
- Tests live in `tests/test_*.py`; run with `uv run pytest` (pythonpath=src is configured in `pyproject.toml`).
- Follow existing patterns: caller-owned `state`/cache dicts mutated in place; `run_once(...) -> str` summary; exponential backoff on `UNREACHABLE`.

## File Structure

- `tools/anim_encoder.py` — pure `bicycle0` encoder (frames+meta → bytes). Dev tool. **(Task 1)**
- `tools/build_nyan_anim.py` — render Nyan frames + encode → `assets/nyan/nyan_72x16.anim`. Dev tool. **(Task 2)**
- `assets/nyan/nyan_72x16.anim` — committed generated asset. **(Task 2)**
- `assets/nyan/meta.json` — `{fps, color_mode, sections}`. **(Task 2)**
- `src/busybar/display.py` — add `PRIORITY_FILLER = 5`. **(Task 3)**
- `src/busybar/config.py` — add `[nyan_filler]` to `DEFAULTS`. **(Task 3)**
- `src/busybar/client.py` — add `upload_asset(...)`. **(Task 5)**
- `integrations/nyan_filler/__init__.py` — empty package marker. **(Task 4)**
- `integrations/nyan_filler/logic.py` — quiet-hours + element builder (pure). **(Task 4)**
- `integrations/nyan_filler/main.py` — poll loop. **(Task 5)**
- `integrations/nyan_filler/com.busybar.nyan-filler.plist` — launchd agent. **(Task 6)**
- `integrations/nyan_filler/README.md` — install + behavior docs. **(Task 6)**
- `config.example.toml` — add `[nyan_filler]` section. **(Task 6)**
- `pyproject.toml` — add `pillow` to the `dev` dependency group. **(Task 2)**
- `tests/test_anim_encoder.py`, `tests/test_nyan_asset.py`, `tests/test_nyan_logic.py`, `tests/test_nyan_main.py`; append to `tests/test_display.py`, `tests/test_config.py`.

---

### Task 1: `bicycle0` animation encoder

**Files:**
- Create: `tools/anim_encoder.py`
- Create: `tools/__init__.py` (empty, so tests can `from tools.anim_encoder import ...`)
- Test: `tests/test_anim_encoder.py`

**Interfaces:**
- Produces: `encode_anim(frames_bgr: list[bytes], width: int, height: int, fps: int, color_mode: str = "rgb888", sections: list[tuple[int,int,str]] | None = None) -> bytes` — each `frames_bgr[i]` is exactly `width*height*3` BGR bytes; returns a complete `.anim` file. `parse_header(data: bytes) -> dict` — decodes the header into `{"magic","width","height","color_mode","fps","n_sections","n_encoded","n_display"}` (used by tests and Task 2).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_anim_encoder.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_anim_encoder.py -v`
Expected: FAIL (`ModuleNotFoundError: tools.anim_encoder`).

- [ ] **Step 3: Write the encoder** (validated end-to-end on-device — SPK-4)

```python
# tools/anim_encoder.py
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
    enc: list[list] = []  # [encoding, duration, data]
    last = None
    for f in frames_bgr:
        if last is not None and f == last:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_anim_encoder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add tools/__init__.py tools/anim_encoder.py tests/test_anim_encoder.py
git commit -m "tools: add bicycle0 .anim encoder (ported from seq2anim.ts)"
```

---

### Task 2: Nyan frame generation + committed `.anim` asset

**Files:**
- Create: `tools/build_nyan_anim.py`
- Create: `assets/nyan/meta.json`
- Create (generated, committed): `assets/nyan/nyan_72x16.anim`
- Modify: `pyproject.toml` (add `pillow` to the `dev` group)
- Test: `tests/test_nyan_asset.py`

**Interfaces:**
- Consumes: `tools.anim_encoder.encode_anim`, `parse_header`.
- Produces: the committed `assets/nyan/nyan_72x16.anim` (72×16, rgb888) and `assets/nyan/meta.json`.

- [ ] **Step 1: Add Pillow as a dev dependency**

In `pyproject.toml`, under `[dependency-groups]`, change the `dev` list to:

```toml
dev = ["pytest>=8.3", "pillow>=10.0"]
```

Run: `uv sync` (installs Pillow into the dev env).

- [ ] **Step 2: Write the builder**

The renderer geometry is copied from the reference community app (`maxswinkels/busybar-apps`, `apps/nyan-cat/app.py`) — the pop-tart cat, rainbow, and twinkling stars, drawn into a flat 72×16 RGB buffer. `random` is seeded for deterministic, committable frames.

```python
# tools/build_nyan_anim.py
"""Render a fixed Nyan Cat loop and compile it to a bicycle0 .anim.

Renderer geometry adapted from the community reference app
(maxswinkels/busybar-apps, apps/nyan-cat). Deterministic (seeded RNG) so the
committed asset is reproducible. Build-time only.

    uv run python tools/build_nyan_anim.py
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image

from tools.anim_encoder import encode_anim

W, H = 72, 16
FRAMES = 24  # ~2s loop at 12 fps
FPS = 12

CRUST=(0xFF,0xCC,0x99); FROSTING=(0xFF,0x99,0xFF); SPRINKLE=(0xDD,0x33,0x88)
GRAY=(0x99,0x99,0x99); BLACK=(0,0,0); CHEEK=(0xFF,0x99,0x99); STAR=(0xFF,0xFF,0xFF)
RAINBOW=[(0xFF,0,0),(0xFF,0x99,0),(0xFF,0xFF,0),(0x33,0xFF,0),(0,0x99,0xFF),(0x66,0x33,0xFF)]
CX,BY=44,3; HX,HY=CX+9,5; TRAIL_END=CX-5

def _blank(): return [(0,0,0)]*(W*H)
def _rect(buf,x,y,w,h,rgb):
    x2,y2=min(W,x+w),min(H,y+h); x,y=max(0,x),max(0,y)
    for yy in range(y,y2):
        base=yy*W
        for xx in range(x,x2): buf[base+xx]=rgb

def _stars_state(): return [{"x":8,"y":3,"p":0},{"x":26,"y":13,"p":2},{"x":46,"y":1,"p":1},{"x":66,"y":11,"p":3}]
def _tick_stars(buf,stars,rng):
    for s in stars:
        s["x"]-=3; s["p"]=(s["p"]+1)%4
        if s["x"]<-2: s["x"]=W+rng.randint(0,10); s["y"]=rng.randint(1,H-2)
        x,y,p=s["x"],s["y"],s["p"]
        if p==0: _rect(buf,x,y,1,1,STAR)
        elif p==1: _rect(buf,x-1,y,3,1,STAR); _rect(buf,x,y-1,1,3,STAR)
        elif p==2: _rect(buf,x-2,y,5,1,STAR); _rect(buf,x,y-2,1,5,STAR)
        else:
            for dx,dy in ((-2,0),(2,0),(0,-2),(0,2)): _rect(buf,x+dx,y+dy,1,1,STAR)

def _rainbow(buf,phase):
    for band,color in enumerate(RAINBOW):
        y=2+band*2; x=0
        while x<TRAIL_END:
            w=min(8,TRAIL_END-x); off=(x//8+phase)%2
            _rect(buf,x,y+off,w,2,color); x+=w

def _cat(buf,phase):
    bob=phase; by,hy=BY+bob,HY+bob
    _rect(buf,CX-2,by+5,2,2,GRAY)
    _rect(buf,CX-4,by+3,2,2,GRAY) if phase==0 else _rect(buf,CX-4,by+7,2,2,GRAY)
    for lx in (CX+1,CX+5,CX+10,CX+14): _rect(buf,lx+bob,13,2,2,GRAY)
    _rect(buf,CX+1,by,12,1,CRUST); _rect(buf,CX,by+1,14,8,CRUST); _rect(buf,CX+1,by+9,12,1,CRUST)
    _rect(buf,CX+1,by+1,12,8,FROSTING)
    for sx,sy in ((2,2),(6,3),(3,5),(7,6),(5,7)): _rect(buf,CX+sx,by+sy,1,1,SPRINKLE)
    _rect(buf,HX+1,hy,8,1,GRAY); _rect(buf,HX,hy+1,10,6,GRAY); _rect(buf,HX+1,hy+7,8,1,GRAY)
    _rect(buf,HX+1,hy-2,1,1,GRAY); _rect(buf,HX+1,hy-1,2,1,GRAY)
    _rect(buf,HX+8,hy-2,1,1,GRAY); _rect(buf,HX+7,hy-1,2,1,GRAY)
    _rect(buf,HX+2,hy+2,1,1,BLACK); _rect(buf,HX+7,hy+2,1,1,BLACK)
    _rect(buf,HX+1,hy+4,1,1,CHEEK); _rect(buf,HX+8,hy+4,1,1,CHEEK)
    _rect(buf,HX+2,hy+4,1,1,BLACK); _rect(buf,HX+6,hy+4,1,1,BLACK); _rect(buf,HX+3,hy+5,3,1,BLACK)

def render_frames():
    rng=random.Random(1)  # deterministic
    stars=_stars_state(); frames=[]
    for t in range(FRAMES):
        phase=(t//3)%2; buf=_blank()
        _tick_stars(buf,stars,rng); _rainbow(buf,phase); _cat(buf,phase)
        frames.append(buf)
    return frames

def main():
    out_dir=Path(__file__).resolve().parents[1]/"assets"/"nyan"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames=render_frames()
    # write PNGs for inspection/reproducibility
    frames_dir=out_dir/"frames"; frames_dir.mkdir(exist_ok=True)
    bgr_frames=[]
    for i,buf in enumerate(frames):
        img=Image.new("RGB",(W,H)); img.putdata(buf); img.save(frames_dir/f"frame_{i}.png")
        bgr_frames.append(bytes(c for (r,g,b) in buf for c in (b,g,r)))
    meta={"fps":FPS,"color_mode":"rgb888","sections":[]}
    (out_dir/"meta.json").write_text(json.dumps(meta, indent=2)+"\n")
    data=encode_anim(bgr_frames, W, H, fps=FPS, color_mode="rgb888")
    (out_dir/"nyan_72x16.anim").write_bytes(data)
    print(f"wrote {out_dir/'nyan_72x16.anim'} ({len(data)} bytes, {len(frames)} frames)")

if __name__=="__main__":
    main()
```

- [ ] **Step 3: Generate the asset**

Run: `uv run python tools/build_nyan_anim.py`
Expected: prints the byte size; creates `assets/nyan/nyan_72x16.anim`, `assets/nyan/meta.json`, `assets/nyan/frames/frame_0.png … frame_23.png`.

- [ ] **Step 4: Write the asset test**

```python
# tests/test_nyan_asset.py
import json
from pathlib import Path
from tools.anim_encoder import parse_header

ASSET = Path(__file__).resolve().parents[1] / "assets" / "nyan" / "nyan_72x16.anim"
META  = Path(__file__).resolve().parents[1] / "assets" / "nyan" / "meta.json"

def test_committed_asset_parses():
    h = parse_header(ASSET.read_bytes())
    assert h["magic"] == b"bicycle0"
    assert (h["width"], h["height"]) == (72, 16)
    assert h["color_mode"] == 0
    assert h["n_display"] >= 1
    assert h["fps"] == json.loads(META.read_text())["fps"]
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_nyan_asset.py -v`
Expected: PASS.

- [ ] **Step 6: Commit** (commit the generated asset + frames)

```bash
git add pyproject.toml tools/build_nyan_anim.py assets/nyan/ tests/test_nyan_asset.py
git commit -m "assets: generate committed Nyan .anim (24-frame loop, 12fps)"
```

---

### Task 3: Shared wiring — `PRIORITY_FILLER` + config defaults

**Files:**
- Modify: `src/busybar/display.py` (add the constant near `PRIORITY_AMBIENT`)
- Modify: `src/busybar/config.py` (add `nyan_filler` to `DEFAULTS`)
- Test: append to `tests/test_display.py` and `tests/test_config.py`

**Interfaces:**
- Produces: `busybar.display.PRIORITY_FILLER = 5`; `DEFAULTS["nyan_filler"] = {"enabled","poll_seconds","quiet_hours"}`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_display.py (append)
from busybar.display import PRIORITY_FILLER, PRIORITY_AMBIENT

def test_filler_priority_below_builtin_and_ambient():
    assert PRIORITY_FILLER == 5
    assert 0 < PRIORITY_FILLER < 10 < PRIORITY_AMBIENT  # 10 = built-in app tier
```

```python
# tests/test_config.py (append)
from busybar.config import load_config

def test_nyan_filler_defaults():
    cfg = load_config(path=None)["nyan_filler"]
    assert cfg == {"enabled": True, "poll_seconds": 1, "quiet_hours": "00:00-07:00"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_display.py::test_filler_priority_below_builtin_and_ambient tests/test_config.py::test_nyan_filler_defaults -v`
Expected: FAIL (ImportError / KeyError).

- [ ] **Step 3: Add the constant** in `src/busybar/display.py`, immediately below the `PRIORITY_AMBIENT`/`AMBIENT_REDRAW_SECONDS` block:

```python
PRIORITY_FILLER = 5
"""Decorative screen-filler (e.g. nyan_filler). Strictly below PRIORITY_AMBIENT
(20) AND below the firmware's built-in-app tier (10), but above the empty/stub
screen (0). Verified on-device (spec 2026-08-06 §5b, SPK-3): the panel's
black/resting state -- true idle AND the CI overlay's silence gap -- rests at
priority 0, so a priority-5 draw fills those gaps; a built-in app at priority 10
outranks it, so the filler never overrides the clock/desktop. Every other tier
(ambient 20, overlay 21, raised 25, alert 60, urgent 65, session 90) preempts
it. Draw with loop=true and re-assert every poll: a same-element redraw
continues the on-device loop (SPK-2), so unconditional per-poll redraw does not
stutter.
"""
```

- [ ] **Step 4: Add the config defaults** in `src/busybar/config.py`, as a new top-level key in `DEFAULTS` (after `ci_status`):

```python
    "nyan_filler": {
        "enabled": True,
        "poll_seconds": 1,            # reclaims a dark gap within ~1s; the draw is
                                      # tiny and mostly-sleeping (see nyan_filler/README)
        "quiet_hours": "00:00-07:00", # local time; "" disables quiet hours entirely
    },
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_display.py tests/test_config.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 6: Commit**

```bash
git add src/busybar/display.py src/busybar/config.py tests/test_display.py tests/test_config.py
git commit -m "display+config: add PRIORITY_FILLER=5 tier and [nyan_filler] defaults"
```

---

### Task 4: `nyan_filler/logic.py` — quiet-hours + element builder (pure)

**Files:**
- Create: `integrations/nyan_filler/__init__.py` (empty)
- Create: `integrations/nyan_filler/logic.py`
- Test: `tests/test_nyan_logic.py`

**Interfaces:**
- Consumes: `busybar.display.PRIORITY_FILLER`.
- Produces:
  - `FILLER_APP = "nyan_filler"`, `ASSET_NAME = "nyan_72x16.anim"`, `ELEMENT_ID = "nyan"`
  - `parse_quiet_hours(s: str) -> tuple[int, int] | None` — `(start_min, end_min)` in minutes-since-midnight, or `None` when `s == ""`. Raises `ValueError` on malformed input.
  - `in_quiet_hours(now: datetime, window: tuple[int, int] | None) -> bool` — `False` when `window is None` or `start == end`; supports midnight wrap; inclusive start, exclusive end.
  - `build_filler_elements(asset: str, timeout_s: int) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_nyan_logic.py
from datetime import datetime
import pytest
from integrations.nyan_filler.logic import (
    parse_quiet_hours, in_quiet_hours, build_filler_elements, ELEMENT_ID)

def _at(h, m=0): return datetime(2026, 8, 6, h, m)

def test_parse_basic_and_empty():
    assert parse_quiet_hours("00:00-07:00") == (0, 420)
    assert parse_quiet_hours("23:00-07:00") == (1380, 420)
    assert parse_quiet_hours("") is None

@pytest.mark.parametrize("bad", ["7-8", "25:00-01:00", "01:60-02:00", "0100-0200", "01:00_02:00"])
def test_parse_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_quiet_hours(bad)

def test_same_day_window_inclusive_start_exclusive_end():
    w = parse_quiet_hours("00:00-07:00")
    assert in_quiet_hours(_at(0, 0), w) is True     # inclusive start
    assert in_quiet_hours(_at(3), w) is True
    assert in_quiet_hours(_at(6, 59), w) is True
    assert in_quiet_hours(_at(7, 0), w) is False    # exclusive end
    assert in_quiet_hours(_at(12), w) is False

def test_midnight_wrap_window():
    w = parse_quiet_hours("23:00-07:00")
    assert in_quiet_hours(_at(23, 30), w) is True
    assert in_quiet_hours(_at(2), w) is True
    assert in_quiet_hours(_at(7, 0), w) is False
    assert in_quiet_hours(_at(12), w) is False

def test_none_and_equal_bounds_never_quiet():
    assert in_quiet_hours(_at(3), None) is False
    assert in_quiet_hours(_at(3), (120, 120)) is False   # start == end -> never

def test_element_shape():
    els = build_filler_elements("nyan_72x16.anim", timeout_s=2)
    assert els == [{"id": ELEMENT_ID, "type": "animation", "path": "nyan_72x16.anim",
                    "x": 0, "y": 0, "loop": True, "timeout": 2}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_nyan_logic.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `logic.py`**

```python
# integrations/nyan_filler/logic.py
"""Pure helpers for the nyan_filler integration: quiet-hours parsing/gating and
the animation element payload. No I/O -- fully unit-tested."""
from __future__ import annotations

import re
from datetime import datetime

FILLER_APP = "nyan_filler"
ASSET_NAME = "nyan_72x16.anim"
ELEMENT_ID = "nyan"

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")


def parse_quiet_hours(s: str) -> tuple[int, int] | None:
    """'HH:MM-HH:MM' -> (start_min, end_min) minutes-since-midnight. '' -> None
    (quiet hours disabled). Raises ValueError on any other malformed input."""
    if s == "":
        return None
    m = _HHMM.match(s.strip())
    if not m:
        raise ValueError(f"invalid quiet_hours {s!r}; expected 'HH:MM-HH:MM' or ''")
    sh, sm, eh, em = (int(g) for g in m.groups())
    return sh * 60 + sm, eh * 60 + em


def in_quiet_hours(now: datetime, window: tuple[int, int] | None) -> bool:
    """True iff `now`'s local wall-clock falls in the window. Inclusive start,
    exclusive end. Supports a window that wraps midnight (start > end). A window
    with start == end is treated as 'never quiet'."""
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    cur = now.hour * 60 + now.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end   # wraps midnight


def build_filler_elements(asset: str, timeout_s: int) -> list[dict]:
    """The single looping animation element drawn at PRIORITY_FILLER."""
    return [{"id": ELEMENT_ID, "type": "animation", "path": asset,
             "x": 0, "y": 0, "loop": True, "timeout": timeout_s}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_nyan_logic.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/nyan_filler/__init__.py integrations/nyan_filler/logic.py tests/test_nyan_logic.py
git commit -m "nyan_filler: pure quiet-hours logic and animation element builder"
```

---

### Task 5: `client.upload_asset` + `nyan_filler/main.py` poll loop

**Files:**
- Modify: `src/busybar/client.py` (add `upload_asset`)
- Create: `integrations/nyan_filler/main.py`
- Test: `tests/test_nyan_main.py` (and one small `upload_asset` test in `tests/test_client.py`)

**Interfaces:**
- Consumes: `BusyBarClient.draw/clear`, `busybar.config.load_config/device_kwargs`, `busybar.display.PRIORITY_FILLER`, `logic.*`.
- Produces:
  - `BusyBarClient.upload_asset(application_name: str, filename: str, data: bytes) -> bool` — local-only POST of raw bytes; `True` on HTTP 200.
  - `nyan_filler.main.run_once(client, cfg, now, state, dry_run=False) -> str` — one poll cycle. `state` is a caller-owned dict (`{"quiet_cleared": bool}`) mutated in place.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_nyan_main.py
from datetime import datetime
from busybar.client import DrawResult
from busybar.display import PRIORITY_FILLER
from integrations.nyan_filler.main import run_once
from integrations.nyan_filler.logic import FILLER_APP, ASSET_NAME

class FakeClient:
    def __init__(self, result=DrawResult.DRAWN):
        self.result = result; self.draws = []; self.clears = 0
    def draw(self, app, elements, priority=50, led_notification_color=None):
        self.draws.append((app, elements, priority)); return self.result
    def clear(self, app):
        self.clears += 1; return True

BASE = {"nyan_filler": {"enabled": True, "poll_seconds": 1, "quiet_hours": "00:00-07:00"}}

def test_draws_at_filler_priority_when_active():
    c = FakeClient(); st = {}
    summary = run_once(c, BASE, datetime(2026, 8, 6, 12, 0), st)  # noon: not quiet
    assert len(c.draws) == 1
    app, elements, priority = c.draws[0]
    assert app == FILLER_APP and priority == PRIORITY_FILLER
    assert elements[0]["type"] == "animation" and elements[0]["path"] == ASSET_NAME
    assert elements[0]["loop"] is True
    assert "drawn" in summary

def test_quiet_hours_clears_once_then_stays_silent():
    c = FakeClient(); st = {}
    run_once(c, BASE, datetime(2026, 8, 6, 3, 0), st)   # 3am: quiet
    run_once(c, BASE, datetime(2026, 8, 6, 3, 1), st)   # still quiet
    assert c.clears == 1        # cleared once on entry, not every poll
    assert c.draws == []

def test_leaving_quiet_hours_draws_again():
    c = FakeClient(); st = {}
    run_once(c, BASE, datetime(2026, 8, 6, 3, 0), st)   # quiet -> clears
    run_once(c, BASE, datetime(2026, 8, 6, 8, 0), st)   # active -> draws
    assert c.clears == 1 and len(c.draws) == 1

def test_disabled_is_noop():
    c = FakeClient(); st = {}
    cfg = {"nyan_filler": {**BASE["nyan_filler"], "enabled": False}}
    summary = run_once(c, cfg, datetime(2026, 8, 6, 12, 0), st)
    assert c.draws == [] and "disabled" in summary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_nyan_main.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Add `upload_asset` to `src/busybar/client.py`** (after `clear`):

```python
    def upload_asset(self, application_name: str, filename: str, data: bytes) -> bool:
        """Upload a raw asset (e.g. a compiled .anim) to the device's app asset
        store. Local-only: assets live on the physical device, so this never
        uses the cloud transport. Returns True on HTTP 200."""
        resp = self._try_local(
            "POST",
            f"/api/assets/upload?application_name={application_name}&file={filename}",
            data=data, headers={"Content-Type": "application/octet-stream"})
        if resp is None:
            log.warning("asset upload unreachable: %s/%s", application_name, filename)
            return False
        if resp.status_code != 200:
            log.warning("asset upload failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return resp.status_code == 200
```

- [ ] **Step 4: Implement `main.py`**

```python
# integrations/nyan_filler/main.py
import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime

from busybar.client import BusyBarClient, DrawResult
from busybar.config import device_kwargs, load_config
from busybar.display import PRIORITY_FILLER

from .logic import (FILLER_APP, ASSET_NAME, build_filler_elements,
                    in_quiet_hours, parse_quiet_hours)

APP = FILLER_APP
log = logging.getLogger(APP)

ASSET_PATH = Path(__file__).resolve().parents[2] / "assets" / "nyan" / ASSET_NAME


def run_once(client, cfg: dict, now: datetime, state: dict, dry_run: bool = False) -> str:
    """One poll cycle. `state` is a caller-owned dict mutated in place:
    `quiet_cleared` records whether we've already released the panel for the
    current quiet window (so we clear once on entry, not every poll)."""
    c = cfg["nyan_filler"]
    if not c["enabled"]:
        return "disabled; no-op"

    window = parse_quiet_hours(c["quiet_hours"])
    if in_quiet_hours(now, window):
        if not state.get("quiet_cleared"):
            if not dry_run:
                client.clear(APP)
            state["quiet_cleared"] = True
            return "quiet hours: released panel"
        return "quiet hours: silent"
    state["quiet_cleared"] = False

    timeout_s = max(2, int(c["poll_seconds"]) * 2)  # self-clears if the poller dies
    elements = build_filler_elements(ASSET_NAME, timeout_s)
    if dry_run:
        return f"DRY-RUN draw @ {PRIORITY_FILLER}: {elements!r}"
    result = client.draw(APP, elements, priority=PRIORITY_FILLER)
    if result == DrawResult.UNREACHABLE:
        return "device unreachable"
    return f"nyan @ {PRIORITY_FILLER} -> {result.value}"


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar Nyan dark-filler")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    client = BusyBarClient(**device_kwargs(cfg))
    client.clear(APP)  # drop any stale element from a previous process

    # Self-healing install: (re)upload the committed asset on startup so the
    # device always has it. One ~83 KB POST per process start, never per poll.
    if not args.dry_run:
        if ASSET_PATH.exists():
            client.upload_asset(APP, ASSET_NAME, ASSET_PATH.read_bytes())
        else:
            log.warning("asset %s missing; run `uv run python tools/build_nyan_anim.py`", ASSET_PATH)

    state: dict = {}
    backoff = 5
    while True:
        summary = run_once(client, cfg, datetime.now(), state, args.dry_run)
        log.info(summary)
        if args.once:
            return 0
        if summary == "device unreachable":
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(cfg["nyan_filler"]["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_nyan_main.py tests/test_client.py -v`
Expected: PASS.

- [ ] **Step 6: Full-suite check + dry-run smoke test**

Run: `uv run pytest -q` (all pre-existing + new tests green)
Run: `uv run python -m nyan_filler.main --once --dry-run` (from `integrations/`; prints a DRY-RUN draw line, no device writes)

- [ ] **Step 7: Commit**

```bash
git add src/busybar/client.py integrations/nyan_filler/main.py tests/test_nyan_main.py tests/test_client.py
git commit -m "nyan_filler: poll loop with quiet-hours gate and startup asset upload"
```

---

### Task 6: Packaging — launchd plist, README, config example

**Files:**
- Create: `integrations/nyan_filler/com.busybar.nyan-filler.plist`
- Create: `integrations/nyan_filler/README.md`
- Modify: `config.example.toml` (add `[nyan_filler]`)

**Interfaces:** none (packaging/docs).

- [ ] **Step 1: Create the plist** (mirror `ci_status`'s template, placeholders `__REPO__`/`__UV__`/`__HOME__` are filled by the same install step as the other agents):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.busybar.nyan-filler</string>
  <key>WorkingDirectory</key><string>__REPO__/integrations</string>
  <key>ProgramArguments</key>
  <array>
    <string>__UV__</string>
    <string>run</string>
    <string>python</string>
    <string>-m</string>
    <string>nyan_filler.main</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>__REPO__/src</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>__HOME__/Library/Logs/busybar/nyan.log</string>
  <key>StandardErrorPath</key><string>__HOME__/Library/Logs/busybar/nyan.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Add the config example** to `config.example.toml`:

```toml
[nyan_filler]
enabled = true                # set false to disable the animation without uninstalling the agent
poll_seconds = 1              # how quickly a dark gap is reclaimed (the draw is tiny; see README)
quiet_hours = "00:00-07:00"   # local time; "" disables quiet hours entirely
```

- [ ] **Step 3: Write `integrations/nyan_filler/README.md`** covering: what it does (fills black gaps with an on-device Nyan animation at `PRIORITY_FILLER = 5`); that it **never overrides a built-in app** (priority 10) and yields to calendar/CI/alerts/sessions; the config keys; that the animation runs on-device (native `.anim`) so host cost is ~1 tiny draw/sec; how to regenerate the asset (`uv run python tools/build_nyan_anim.py`); and that the agent uploads the asset to the device on startup. Reference `docs/superpowers/specs/2026-08-06-nyan-filler-design.md` for the priority/spike rationale.

- [ ] **Step 4: Verify the suite still passes**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add integrations/nyan_filler/com.busybar.nyan-filler.plist integrations/nyan_filler/README.md config.example.toml
git commit -m "nyan_filler: launchd agent, README, and config example"
```

---

### Task 7: On-device verification (operator/primary pass — not a subagent task)

Requires the live device at `10.0.4.20`. Run after Task 6. This is a manual/primary checklist, not a pytest gate.

- [ ] **Asset renders:** `uv run python -m nyan_filler.main --once` uploads + draws once; capture `GET /api/screen?display=0` and confirm non-black Nyan pixels.
- [ ] **Fills gaps during a CI run:** with `ci_status` running and a real CI run active, watch the panel — confirm Nyan appears in the ~10 s gaps between CI frames (no black gaps), and the CI frames still preempt it.
- [ ] **Never overrides a built-in app:** put the device on a built-in app (e.g. the clock); confirm Nyan does **not** replace it (priority 5 < 10).
- [ ] **Quiet hours:** temporarily set `quiet_hours` to a window covering "now"; confirm the panel goes dark and stays dark; step past the end; confirm Nyan resumes.
- [ ] **Preemption:** confirm a calendar approach/imminent draw and a CI alert each cleanly take the screen and Nyan resumes after they clear.
- [ ] **Cost sanity:** confirm the process is idle/sleeping between polls (no measurable CPU).

---

## Self-Review

**1. Spec coverage:**
- §2 scope (new integration, PRIORITY_FILLER, launchd, config, quiet hours) → Tasks 3–6. ✓
- §5b spikes → encoded into Task 1/2 (encoder+asset), Task 3 (priority), Task 5 (redraw-every-poll). ✓
- §6 priority 5 + never-override-builtin → Task 3 + Task 7. ✓
- §7 authoring pipeline (ported seq2anim encoder, raw frames, BGR, committed asset) → Tasks 1–2. ✓
- §8 loop/redraw policy (unconditional per-poll redraw, timeout ≈ 2×poll, quiet-hours clear-on-entry) → Task 5. ✓
- §9 config keys/defaults → Task 3 + Task 6. ✓
- §10 quiet-hours semantics (wrap, empty, start==end, inclusive/exclusive, clear-on-entry) → Task 4 + Task 5. ✓
- §11 error handling (backoff, 409 normal via DrawResult, asset-missing warning) → Task 5. ✓
- §13 tests (quiet-hours boundaries, config parse) → Tasks 3–5. ✓
- §14 on-device verification → Task 7. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has runnable code; the on-device steps in Task 7 are explicitly a manual pass, not a pytest gate. ✓

**3. Type consistency:** `FILLER_APP`/`ASSET_NAME`/`ELEMENT_ID` defined in Task 4 and reused verbatim in Task 5. `encode_anim`/`parse_header` signatures identical across Tasks 1–2. `run_once(client, cfg, now, state, dry_run=False)` consistent between Task 5 impl and tests. `PRIORITY_FILLER` (Task 3) consumed in Task 5. ✓
