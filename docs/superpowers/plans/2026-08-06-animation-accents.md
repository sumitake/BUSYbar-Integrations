# Stock-Animation Accents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add device stock-animation accents to the two existing integrations — animated calendar icons at the 5-min/1-min escalation stages, a full-panel animation takeover for the first 60 s after an event starts, and an animated spinner on the CI running badge.

**Architecture:** Pure-logic changes in each integration's `logic.py` (new element construction gated by config), threaded through `main.py`; stock animations are referenced in place via `stock_path` (no assets bundled). Calendar `main.run_once` adopts `ci_status`'s proven unified shape-tracker clear so the new id-set transitions clear correctly.

**Tech Stack:** Python ≥3.12, stdlib + `requests`; pytest. No new dependencies.

**Reference spec:** `docs/superpowers/specs/2026-08-06-animation-accents-design.md` (spikes resolved §7).

## Global Constraints

- Public repo — sanitize; never print `config.toml`; no secrets.
- Runtime deps unchanged (stdlib + `requests`). **No new assets** — stock animations referenced via `stock_path` form **`shared/<name>.anim`** (verified working).
- Stock names (exact): `calendar_event_16x16` (5-min), `calendar_reminder_16x16` (1-min), `spinner_front_8x8` (CI), and the configurable `start_animation` (default `meeting_72x16`).
- Config defaults (exact): `[calendar_countdown]` `escalation_icons=true`, `start_animation="meeting_72x16"`, `start_window_seconds=60`; `[ci_status]` `running_spinner=true`.
- Priorities: warn/imminent stay `PRIORITY_AMBIENT_URGENT` (65, unchanged); the just-started takeover is 65; in-progress past the window is `PRIORITY_AMBIENT` (20, unchanged).
- Every animation element: `{"type": "animation", "loop": true, "timeout": timeout_s}` with `x`/`y` as specified.
- Icon at `x=0, y=0` (16×16); the countdown numeral keeps its existing `CD_TEXT_X=39`; spinner at `x=64, y=0` (8×8).
- Tests: `uv run pytest`. Follow existing patterns (caller-owned `state` dict; `run_once(...) -> str`; commit-on-`DRAWN`).
- Backward-compat: new `build_elements`/`select_priority`/`_build_running_elements`/`build_overlay_payload` params default to the pre-feature behavior so existing tests/callers are unaffected.

## File Structure

- `src/busybar/config.py` — new default keys. **(Task 1)**
- `integrations/calendar_countdown/logic.py` — `is_just_started`, icon/takeover elements, `select_priority` just-started tier. **(Task 2)**
- `integrations/calendar_countdown/main.py` — thread `just_started`; unified shape-tracker clear. **(Task 3)**
- `integrations/ci_status/logic.py` — running-badge spinner (gated). **(Task 4)**
- `integrations/calendar_countdown/README.md`, `integrations/ci_status/README.md`, `config.example.toml` — docs/config. **(Task 5)**
- Tests: `tests/test_config.py`, `tests/test_calendar_logic.py`, `tests/test_calendar_loop.py`, `tests/test_ci_logic.py`.
- On-device verification. **(Task 6)**

---

### Task 1: Config defaults

**Files:**
- Modify: `src/busybar/config.py` (`DEFAULTS`)
- Test: append to `tests/test_config.py`

**Interfaces:**
- Produces: `DEFAULTS["calendar_countdown"]["escalation_icons"|"start_animation"|"start_window_seconds"]`, `DEFAULTS["ci_status"]["running_spinner"]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py (append)
from busybar.config import load_config

def test_animation_accent_defaults():
    cfg = load_config(path=None)
    cal = cfg["calendar_countdown"]
    assert cal["escalation_icons"] is True
    assert cal["start_animation"] == "meeting_72x16"
    assert cal["start_window_seconds"] == 60
    assert cfg["ci_status"]["running_spinner"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_animation_accent_defaults -v`
Expected: FAIL (KeyError).

- [ ] **Step 3: Add the defaults** — in `src/busybar/config.py`, add these keys to the existing `DEFAULTS["calendar_countdown"]` dict (alongside the current keys):

```python
        # Stock-animation accents (2026-08-06). Icons/animation are device
        # stock, referenced by stock_path -- no assets bundled.
        "escalation_icons": True,       # animated calendar icons at warn (5m) / imminent (1m)
        "start_animation": "meeting_72x16",  # full-panel takeover for the first minute after
                                             # start (aligned with the T-0 chirp); "" disables
        "start_window_seconds": 60,     # how long the start takeover holds (also its urgent-priority window)
```

and add to `DEFAULTS["ci_status"]`:

```python
        "running_spinner": True,        # animated 8x8 spinner on the running badge
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/busybar/config.py tests/test_config.py
git commit -m "config: add stock-animation accent defaults for calendar + ci"
```

---

### Task 2: Calendar logic — icons, start takeover, priority

**Files:**
- Modify: `integrations/calendar_countdown/logic.py`
- Test: append to `tests/test_calendar_logic.py`

**Interfaces:**
- Consumes: existing `PRIORITY_AMBIENT_URGENT`, `CD_TEXT_X`, `_state_for`, `_minutes_left`, `STATE_WARNING`, `BG_GRADIENT`, `CalEvent`.
- Produces:
  - `ICON_EVENT = "calendar_event_16x16"`, `ICON_REMINDER = "calendar_reminder_16x16"`, `ICON_X = 0`, `ICON_Y = 0`, `ICON_TITLE_X = 18`, `START_ANIM_ID = "cal_start_anim"`, `CAL_ICON_ID = "cal_icon"`.
  - `is_just_started(event: CalEvent, now: datetime, in_progress: bool, start_window_seconds: int, start_animation: str) -> bool`
  - `select_priority(minutes_left, approach_minutes, notice_minutes, in_progress, just_started: bool = False) -> int` (new trailing param)
  - `build_elements(event, now, cfg, timeout_s, in_progress, just_started: bool = False) -> list[dict]` (new trailing param)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_calendar_logic.py (append)
from datetime import datetime, timedelta, timezone
from integrations.calendar_countdown.logic import (
    is_just_started, select_priority, build_elements, CalEvent,
    START_ANIM_ID, CAL_ICON_ID, ICON_EVENT, ICON_REMINDER)
from busybar.display import PRIORITY_AMBIENT_URGENT, PRIORITY_AMBIENT

def _ev(start):  # 30-min event
    return CalEvent(title="Standup", start=start, end=start + timedelta(minutes=30), all_day=False)

def _cfg(**over):
    base = {"poll_seconds": 10, "lookahead_hours": 12, "warn_minutes": 5, "notice_minutes": 15,
            "approach_minutes": 30, "imminent_minutes": 1, "progress_window_minutes": 60,
            "escalation_icons": True, "start_animation": "meeting_72x16", "start_window_seconds": 60}
    base.update(over); return base

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

def test_is_just_started_window():
    ev = _ev(NOW - timedelta(seconds=30))   # started 30s ago
    assert is_just_started(ev, NOW, True, 60, "meeting_72x16") is True
    ev2 = _ev(NOW - timedelta(seconds=90))  # started 90s ago
    assert is_just_started(ev2, NOW, True, 60, "meeting_72x16") is False
    assert is_just_started(ev, NOW, True, 60, "") is False       # disabled
    assert is_just_started(ev, NOW, False, 60, "meeting_72x16") is False  # not in progress

def test_priority_just_started_is_urgent():
    assert select_priority(0.0, 30, 15, True, just_started=True) == PRIORITY_AMBIENT_URGENT
    assert select_priority(0.0, 30, 15, True, just_started=False) == PRIORITY_AMBIENT  # unchanged

def test_warn_stage_adds_event_icon():
    ev = _ev(NOW + timedelta(minutes=4))   # 4m out -> warn, > imminent
    els = build_elements(ev, NOW, _cfg(), 15, in_progress=False)
    icon = next(e for e in els if e["id"] == CAL_ICON_ID)
    assert icon["type"] == "animation" and icon["stock_path"] == f"shared/{ICON_EVENT}.anim"
    assert icon["x"] == 0 and icon["y"] == 0
    assert any(e["id"] == "title" for e in els)   # title still present at warn

def test_imminent_stage_uses_reminder_icon_and_drops_title():
    ev = _ev(NOW + timedelta(seconds=30))  # 0.5m out -> imminent
    els = build_elements(ev, NOW, _cfg(), 15, in_progress=False)
    icon = next(e for e in els if e["id"] == CAL_ICON_ID)
    assert icon["stock_path"] == f"shared/{ICON_REMINDER}.anim"
    assert not any(e["id"] == "title" for e in els)   # title dropped at imminent

def test_just_started_returns_takeover_animation():
    ev = _ev(NOW - timedelta(seconds=10))
    els = build_elements(ev, NOW, _cfg(), 15, in_progress=True, just_started=True)
    anim = next(e for e in els if e["id"] == START_ANIM_ID)
    assert anim["type"] == "animation" and anim["stock_path"] == "shared/meeting_72x16.anim"
    assert not any(e["id"] in ("cd_text", "ends") for e in els)   # takeover replaces the countdown

def test_escalation_icons_off_is_unchanged():
    ev = _ev(NOW + timedelta(minutes=4))
    els = build_elements(ev, NOW, _cfg(escalation_icons=False), 15, in_progress=False)
    assert not any(e["id"] == CAL_ICON_ID for e in els)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_calendar_logic.py -k "just_started or icon or priority_just" -v`
Expected: FAIL (ImportError / new params missing).

- [ ] **Step 3: Implement in `logic.py`**

Add near the other module constants:

```python
ICON_EVENT = "calendar_event_16x16"
ICON_REMINDER = "calendar_reminder_16x16"
ICON_X, ICON_Y = 0, 0
ICON_TITLE_X = 18          # title shifts right of the 16x16 icon (icon occupies x=0..15)
CAL_ICON_ID = "cal_icon"
START_ANIM_ID = "cal_start_anim"
```

Add the pure predicate:

```python
def is_just_started(event: CalEvent, now: datetime, in_progress: bool,
                    start_window_seconds: int, start_animation: str) -> bool:
    """True for the first `start_window_seconds` after an event begins, when a
    start-takeover animation is configured. The window aligns with the T-0
    chirp and holds the display at urgent priority as a 'running late' alarm."""
    if not in_progress or not start_animation:
        return False
    return (now - event.start).total_seconds() < start_window_seconds
```

Add the `just_started` tier to `select_priority` (new trailing param, checked first):

```python
def select_priority(minutes_left, approach_minutes, notice_minutes, in_progress,
                    just_started: bool = False) -> int:
    if just_started:
        return PRIORITY_AMBIENT_URGENT
    # ... existing body unchanged ...
```

In `build_elements`, add the trailing param `just_started: bool = False` and, at the very top of the body, the takeover short-circuit:

```python
    if just_started:
        bg = {"id": "bg", "type": "rectangle", "x": 0, "y": 0,
              "width": PANEL_WIDTH, "height": PANEL_HEIGHT, "fill": "gradient_v",
              "fill_colors": BG_GRADIENT[STATE_IN_PROGRESS], "border_width": 0, "timeout": timeout_s}
        anim = {"id": START_ANIM_ID, "type": "animation",
                "stock_path": f"shared/{cfg['start_animation']}.anim",
                "x": 0, "y": 0, "loop": True, "timeout": timeout_s}
        return [bg, anim]
```

Then, in the existing NOT-in_progress (upcoming) path, after the `title_element`/countdown are built and before assembling the final `elements` list, add the icon and adjust the title. Compute the stage from the already-available `minutes_left` and `cfg`:

```python
    icon_element = None
    if not in_progress and cfg.get("escalation_icons") and state == STATE_WARNING:
        imminent = minutes_left <= cfg["imminent_minutes"]
        icon_name = ICON_REMINDER if imminent else ICON_EVENT
        icon_element = {"id": CAL_ICON_ID, "type": "animation",
                        "stock_path": f"shared/{icon_name}.anim",
                        "x": ICON_X, "y": ICON_Y, "loop": True, "timeout": timeout_s}
        if imminent:
            title_element = None          # drop the title at imminent -> icon + big number
        else:
            title_element.update({"x": ICON_TITLE_X,
                                  "width": CD_TEXT_X - ICON_TITLE_X - 2})  # scroll in the gap
```

Assemble `elements` so `title_element` is included only when not None, and append `icon_element` when present (draw order: icon after bg/title so it sits on top of the bg). Keep the existing in-progress branch unchanged (it runs only when `not just_started`).

*(The implementer reads the existing `build_elements` to integrate these; `state`, `minutes_left`, `title_element`, `CD_TEXT_X`, `STATE_WARNING`, `STATE_IN_PROGRESS`, `BG_GRADIENT`, `PANEL_WIDTH`/`PANEL_HEIGHT` are already in scope there.)*

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_calendar_logic.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add integrations/calendar_countdown/logic.py tests/test_calendar_logic.py
git commit -m "calendar: escalation icons, start-takeover animation, just-started priority"
```

---

### Task 3: Calendar main — thread just_started + unified shape-tracker clear

**Files:**
- Modify: `integrations/calendar_countdown/main.py` (`run_once`)
- Test: append to `tests/test_calendar_loop.py`

**Interfaces:**
- Consumes: `logic.is_just_started`, `build_elements`/`select_priority` new params.
- Produces: `run_once` draws the takeover at priority 65 during the start window and clears on any element-id-set change (`state["last_shape"]`), replacing the old `state["in_progress"]`-transition clear.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_calendar_loop.py (append)
from datetime import datetime, timedelta, timezone
from busybar.client import DrawResult
from busybar.display import PRIORITY_AMBIENT_URGENT
from integrations.calendar_countdown.main import run_once
from integrations.calendar_countdown.logic import CalEvent, START_ANIM_ID

class FakeClient:
    def __init__(self): self.draws=[]; self.clears=0
    def draw(self, app, elements, priority=50, led_notification_color=None):
        self.draws.append((elements, priority)); return DrawResult.DRAWN
    def clear(self, app): self.clears += 1; return True
    def get_busy(self): return {}
    def play_audio(self, *a, **k): return True
    def set_busy_simple(self, *a, **k): return True

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
def _cfg(**o):
    b={"poll_seconds":10,"lookahead_hours":12,"warn_minutes":5,"notice_minutes":15,
       "approach_minutes":30,"imminent_minutes":1,"progress_window_minutes":60,"include_all_day":False,
       "auto_busy":False,"calendars":[],"chirp":False,"escalation_icons":True,
       "start_animation":"meeting_72x16","start_window_seconds":60}
    b.update(o); return {"calendar_countdown": b}
def _fetch(ev):  # fetch(lookahead) -> [ev]
    return lambda hours: [ev]

def test_just_started_draws_takeover_at_urgent():
    ev = CalEvent("Standup", NOW - timedelta(seconds=15), NOW + timedelta(minutes=29), False)
    c = FakeClient(); st = {}
    run_once(c, _fetch(ev), _cfg(), NOW, dry_run=False, state=st)
    elements, priority = c.draws[-1]
    assert priority == PRIORITY_AMBIENT_URGENT
    assert any(e["id"] == START_ANIM_ID for e in elements)

def test_shape_change_triggers_clear():
    # First poll: warn stage (icon+title). Second poll: takeover (different id-set) -> clear.
    ev = CalEvent("Standup", NOW + timedelta(minutes=4), NOW + timedelta(minutes=34), False)
    c = FakeClient(); st = {}
    run_once(c, _fetch(ev), _cfg(), NOW, dry_run=False, state=st)            # warn
    ev2 = CalEvent("Standup", NOW - timedelta(seconds=10), NOW + timedelta(minutes=29), False)
    run_once(c, _fetch(ev2), _cfg(), NOW, dry_run=False, state=st)          # takeover
    assert c.clears >= 1   # id-set changed -> cleared before the takeover draw
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_calendar_loop.py -k "just_started or shape_change" -v`
Expected: FAIL.

- [ ] **Step 3: Implement in `main.run_once`**

After `event`/`in_progress` are resolved and before building elements, compute:

```python
    just_started = is_just_started(event, now, in_progress,
                                   c["start_window_seconds"], c["start_animation"])
```

Pass it through:

```python
    elements = build_elements(event, now, c, timeout_s, in_progress, just_started=just_started)
    minutes_left = _minutes_left(event, now, in_progress)
    priority = select_priority(minutes_left, c["approach_minutes"], c["notice_minutes"],
                               in_progress, just_started=just_started)
```

Replace the existing `state["in_progress"]`-transition clear block (the `if state is not None and state.get("in_progress") not in (None, in_progress): client.clear(APP)`) with a unified shape-tracker clear (mirrors `ci_status`):

```python
    new_shape = frozenset(e["id"] for e in elements)
    if state is not None:
        last_shape = state.get("last_shape")
        if last_shape is not None and last_shape != new_shape:
            client.clear(APP)   # id-set changed -> drop stale elements first (same as ci_status)
```

In the `result == DrawResult.DRAWN` commit block, replace the `state["in_progress"] = in_progress` line with:

```python
        state["last_shape"] = new_shape
```

In the `event is None` path, replace `state["in_progress"] = None` with `state["last_shape"] = None` (device is now blank). Leave `state["next_start"]`, the LED bookkeeping, and the chirp logic untouched.

Add `is_just_started` to the existing `from .logic import (...)` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_calendar_loop.py -v`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add integrations/calendar_countdown/main.py tests/test_calendar_loop.py
git commit -m "calendar: draw start-takeover at urgent; unified shape-tracker clear"
```

---

### Task 4: CI running-badge spinner

**Files:**
- Modify: `integrations/ci_status/logic.py` (`_build_running_elements`, `build_overlay_payload`), `integrations/ci_status/main.py` (pass `show_spinner`)
- Test: append to `tests/test_ci_logic.py`

**Interfaces:**
- Produces: `_build_running_elements(info, timeout_s, show_spinner: bool = False)`, `build_overlay_payload(..., show_spinner: bool = False)`; constants `RUN_SPINNER_ID = "run_spinner"`, `SPINNER_STOCK = "shared/spinner_front_8x8.anim"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ci_logic.py (append)
from datetime import datetime, timezone
from integrations.ci_status.logic import (
    build_overlay_payload, RunningInfo, OVERLAY_FRAME_CI_BADGE, RUN_SPINNER_ID)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
def _running():
    return RunningInfo(run={"name": "build", "run_started_at": "2026-08-06T11:58:00Z",
                            "pull_requests": [{"number": 42}], "workflow_id": 1},
                       repo="me/repo", other_count=0, median_minutes=8.0, now=NOW)

def test_spinner_present_and_title_reserved_when_on():
    p = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, 10, running=_running(), show_spinner=True)
    els = p["elements"]
    spin = next(e for e in els if e["id"] == RUN_SPINNER_ID)
    assert spin["type"] == "animation" and spin["stock_path"] == "shared/spinner_front_8x8.anim"
    assert spin["x"] == 64 and spin["y"] == 0
    title = next(e for e in els if e["id"] == "title")
    assert title["width"] == 60   # reserved so the scrolling title never runs under the spinner

def test_no_spinner_and_full_title_when_off():
    p = build_overlay_payload(OVERLAY_FRAME_CI_BADGE, 10, running=_running(), show_spinner=False)
    els = p["elements"]
    assert not any(e["id"] == RUN_SPINNER_ID for e in els)
    assert next(e for e in els if e["id"] == "title")["width"] == 68  # unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ci_logic.py -k spinner -v`
Expected: FAIL.

- [ ] **Step 3: Implement in `ci_status/logic.py`**

Add constants near the running-badge section:

```python
RUN_SPINNER_ID = "run_spinner"
SPINNER_STOCK = "shared/spinner_front_8x8.anim"
RUNNING_TITLE_WIDTH_SPINNER = 60   # reserve the top-right 8x8 corner (spinner at x=64)
```

In `_build_running_elements(info, timeout_s)` add the trailing param `show_spinner: bool = False`. Where the title element's `width` is set to `RUNNING_TITLE_WIDTH`, use `RUNNING_TITLE_WIDTH_SPINNER if show_spinner else RUNNING_TITLE_WIDTH` (and use the same value in the `_title_fits` scroll decision). After the elements list is assembled, before `return`:

```python
    if show_spinner:
        elements.append({"id": RUN_SPINNER_ID, "type": "animation", "stock_path": SPINNER_STOCK,
                         "x": 64, "y": 0, "loop": True, "timeout": timeout_s})
```

In `build_overlay_payload`, add trailing param `show_spinner: bool = False` and pass it through only on the CI-badge branch:

```python
    if frame_name == OVERLAY_FRAME_CI_BADGE:
        if running is None:
            return None
        return {"elements": _build_running_elements(running, timeout_s, show_spinner=show_spinner),
                "priority": PRIORITY_OVERLAY, "led": None}
```

(Quota branches unchanged — they never get a spinner.)

- [ ] **Step 4: Thread the config in `ci_status/main.py`**

At the `build_overlay_payload(...)` call in `run_once`, add `show_spinner=c["running_spinner"]`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_ci_logic.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add integrations/ci_status/logic.py integrations/ci_status/main.py tests/test_ci_logic.py
git commit -m "ci: animated spinner on the running badge (config-gated)"
```

---

### Task 5: Docs + config example

**Files:**
- Modify: `integrations/calendar_countdown/README.md`, `integrations/ci_status/README.md`, `config.example.toml`

- [ ] **Step 1: Update `config.example.toml`** — add to the existing `[calendar_countdown]` block:

```toml
escalation_icons = true         # animated calendar icons at the 5-min / 1-min stages
start_animation = "meeting_72x16"  # full-panel takeover for the first minute after an event starts; "" disables
start_window_seconds = 60       # how long that takeover holds
```

and to the existing `[ci_status]` block:

```toml
running_spinner = true          # animated 8x8 spinner on the running badge
```

- [ ] **Step 2: Update the two integration READMEs** — in `calendar_countdown/README.md`, document: the animated calendar icons at warn (5-min, `calendar_event`) and imminent (1-min, `calendar_reminder`, title dropped); the full-panel **start takeover** for the first `start_window_seconds` after an event begins (held at urgent priority, aligned with the T-0 chirp, shows the fixed word of the chosen `start_animation`, configurable/`""`-disables); that stock animations are referenced by `stock_path` (no assets bundled). In `ci_status/README.md`, document the `running_spinner` on the running badge (quota frames unaffected). Reference `docs/superpowers/specs/2026-08-06-animation-accents-design.md`.

- [ ] **Step 3: Verify suite still green**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add integrations/calendar_countdown/README.md integrations/ci_status/README.md config.example.toml
git commit -m "docs: document animation accents + config for calendar and ci"
```

---

### Task 6: On-device verification (operator/primary pass — not a subagent task)

Requires the live device and the two agents restarted (`launchctl kickstart -k gui/$(id -u)/com.busybar.calendar-countdown` and `.../com.busybar.ci-status` after the branch is checked out, or run `--once` from `integrations/`). Manual/primary checklist:

- [ ] **Calendar warn (5-min):** event icon appears left, title + amber countdown visible, no clipping.
- [ ] **Calendar imminent (1-min):** reminder-bell icon left, big red countdown, title dropped, LED blinks (existing) — and confirm the title's old pixels are gone (shape clear worked).
- [ ] **Calendar start takeover:** at T-0 (with the chirp), the full-panel animation shows for ~60 s at urgent priority, then reverts cleanly to the "ENDS" display (no stale takeover pixels).
- [ ] **Warn title legibility:** confirm the scrolling title in the narrow `x=18..38` band reads acceptably; if cramped, apply the spec §9 fallback (drop the title at warn too).
- [ ] **CI running spinner:** during a real run, the 8×8 spinner animates top-right and the title/ETA are not occluded; quota frames show no spinner.
- [ ] Capture framebuffers (`/api/screen?display=0`) for each state as evidence.

---

## Self-Review

**1. Spec coverage:** §4a warn/imminent icons → Task 2 (+3 wiring); §4b start takeover + priority → Tasks 2/3; §5 CI spinner + title reserve → Task 4; §6 config → Task 1 (+5 example); §3 stock_path form → constants in Tasks 2/4; §8 tests → Tasks 1–4; on-device → Task 6. ✓
**2. Placeholder scan:** No TBD/TODO; every code step has concrete code; Task 6 is explicitly a manual pass. The `build_elements` integration references in-scope existing symbols and gives the exact new blocks. ✓
**3. Type consistency:** `just_started` trailing-param default `False` consistent across `is_just_started`/`select_priority`/`build_elements`; `show_spinner` default `False` across `_build_running_elements`/`build_overlay_payload`; ids (`cal_icon`/`cal_start_anim`/`run_spinner`) and constants reused verbatim between logic and tests; `state["last_shape"]` (frozenset) consistent with the commit + `event is None` paths. ✓
