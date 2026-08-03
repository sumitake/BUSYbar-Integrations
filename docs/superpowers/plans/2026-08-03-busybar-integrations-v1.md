# BUSY Bar Integrations v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `busybar` shared client plus two integrations (calendar countdown, CI status) as a public-safe uv monorepo.

**Architecture:** A small `busybar` Python package wraps the device's local HTTP API. Each integration is an independent polling script under `integrations/`, drawing via the firmware's own priority arbitration (calendar=20, CI alert=60). Pure decision logic lives in per-integration `logic.py` modules (fully unit-tested); side-effectful adapters (EventKit, GitHub REST, device HTTP) are thin and injected.

**Tech Stack:** Python ≥3.12, uv, requests, pyobjc-framework-EventKit (macOS-only dep marker), pytest. GitHub access via REST only, authenticated with a token obtained from `gh auth token` at process start.

**Spec:** `docs/superpowers/specs/2026-08-03-calendar-ci-integrations-design.md`

## Global Constraints

- Repo is **public**: no personal repo names, calendar names, tokens, or non-default LAN IPs in any committed file. Only the USB default `10.0.4.20` may appear.
- GitHub GraphQL is **prohibited** — never call `gh pr status`/`gh pr checks`/`gh run list`; REST endpoints only, always with `If-None-Match` ETag caching.
- Device text elements accept **printable ASCII only** (`^[\x20-\x7E]+$`); all display text must pass through `ascii_safe()`.
- Fonts enum: `tiny, small, normal, condensed, bold, large, extra_large, global`. Colors: `#RRGGBBAA`.
- Draw priorities: calendar_countdown = 20, ci_status = 60 (firmware: built-ins 10, BUSY session 90).
- Every element sets `timeout` ≈ 1.5× the poll interval so the display self-clears if a script dies.
- Device HTTP timeouts: (3 s connect, 5 s read). Unreachable device never crashes a loop; backoff 5 s doubling to cap 300 s.
- Commits: imperative scoped messages (`client: …`, `calendar: …`, `ci: …`, `docs: …`, `meta: …`).

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `config.example.toml`, `src/busybar/__init__.py`, `tests/__init__.py`

**Interfaces:**
- Produces: importable `busybar` package; `uv run pytest` runnable; `config.example.toml` keys consumed by Task 2.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "busybar"
version = "0.1.0"
description = "Integrations for the BUSY Bar local HTTP API"
requires-python = ">=3.12"
dependencies = [
    "requests>=2.32",
    "pyobjc-framework-EventKit>=10.3; sys_platform == 'darwin'",
]

[dependency-groups]
dev = ["pytest>=8.3"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/busybar"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
config.toml
.venv/
__pycache__/
*.pyc
.pytest_cache/
uv.lock
```

(`uv.lock` ignored deliberately: this is a library-style repo people fork; no personal lock noise.)

- [ ] **Step 3: Write `config.example.toml`** (placeholders only — public repo)

```toml
# Copy to config.toml (git-ignored) and edit. All keys optional; defaults shown.

[device]
host = "10.0.4.20"          # USB-Ethernet default; set your LAN IP for Wi-Fi

[calendar_countdown]
poll_seconds = 60
lookahead_hours = 12
warn_minutes = 5
include_all_day = false
auto_busy = false
# calendars = ["Work"]      # omit to read all calendars

[ci_status]
poll_seconds = 120
repos = ["your-user/your-repo"]
show_green = false
# stale_queued_minutes = 15 # omit to disable stuck-queue detection
```

- [ ] **Step 4: Create empty `src/busybar/__init__.py` and `tests/__init__.py`**, then verify:

Run: `uv sync && uv run python -c "import busybar; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore config.example.toml src tests
git commit -m "meta: scaffold uv project with busybar package"
```

---

### Task 2: Config loader

**Files:**
- Create: `src/busybar/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `load_config(path: Path | None = None) -> dict` — deep-merges DEFAULTS ← toml file ← `BUSYBAR_HOST` env var. Missing file ⇒ pure defaults. `find_config() -> Path | None` locates `config.toml` next to the repo root (two parents up from `src/busybar/`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from pathlib import Path
from busybar.config import load_config

def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg["device"]["host"] == "10.0.4.20"
    assert cfg["calendar_countdown"]["poll_seconds"] == 60
    assert cfg["ci_status"]["repos"] == []

def test_file_overrides_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nhost = "192.0.2.7"\n[ci_status]\nrepos = ["a/b"]\n')
    cfg = load_config(p)
    assert cfg["device"]["host"] == "192.0.2.7"
    assert cfg["ci_status"]["repos"] == ["a/b"]
    assert cfg["calendar_countdown"]["warn_minutes"] == 5  # untouched default

def test_env_overrides_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('[device]\nhost = "192.0.2.7"\n')
    monkeypatch.setenv("BUSYBAR_HOST", "192.0.2.99")
    assert load_config(p)["device"]["host"] == "192.0.2.99"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'load_config'`

- [ ] **Step 3: Implement `src/busybar/config.py`**

```python
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "device": {"host": "10.0.4.20"},
    "calendar_countdown": {
        "poll_seconds": 60,
        "lookahead_hours": 12,
        "warn_minutes": 5,
        "include_all_day": False,
        "auto_busy": False,
        "calendars": [],
    },
    "ci_status": {
        "poll_seconds": 120,
        "repos": [],
        "show_green": False,
        "stale_queued_minutes": 0,  # 0 = disabled
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def find_config() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "config.toml"
    return candidate if candidate.exists() else None


def load_config(path: Path | None = None) -> dict:
    if path is None:
        path = find_config()
    cfg = DEFAULTS
    if path is not None and Path(path).exists():
        with open(path, "rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))
    env_host = os.environ.get("BUSYBAR_HOST")
    if env_host:
        cfg = _merge(cfg, {"device": {"host": env_host}})
    return cfg
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_config.py -v` — Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/busybar/config.py tests/test_config.py
git commit -m "client: add config loader with defaults and env override"
```

---

### Task 3: BusyBarClient

**Files:**
- Create: `src/busybar/client.py`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: nothing (requests only).
- Produces (used by both integrations):
  - `DrawResult` enum: `DRAWN`, `REJECTED`, `UNREACHABLE`
  - `BusyBarClient(host: str = "10.0.4.20", timeout: tuple = (3, 5))`
  - `.draw(application_name: str, elements: list[dict], priority: int = 50, led_notification_color: str | None = None) -> DrawResult`
  - `.clear(application_name: str) -> bool`
  - `.status() -> dict | None`
  - `.get_busy() -> dict | None`
  - `.set_busy_simple(time_left_ms: int) -> bool` — PUT `/api/busy/snapshot` with `{"type": "SIMPLE", "card_id": "00000000-0000-0000-0000-000000000000", "time_left_ms": …, "is_paused": false}`

All HTTP goes through module-level `requests.request` (patched in tests).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_client.py
from unittest.mock import Mock, patch
import requests
from busybar.client import BusyBarClient, DrawResult

ELEMENTS = [{"id": "0", "type": "text", "text": "hi", "font": "normal"}]


def _response(status_code: int, payload: dict | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    return resp


@patch("busybar.client.requests.request")
def test_draw_success(mock_request):
    mock_request.return_value = _response(200)
    client = BusyBarClient(host="192.0.2.1")
    assert client.draw("app", ELEMENTS, priority=20) == DrawResult.DRAWN
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "http://192.0.2.1/api/display/draw"
    body = mock_request.call_args.kwargs["json"]
    assert body["application_name"] == "app" and body["priority"] == 20
    assert "led_notification_color" not in body


@patch("busybar.client.requests.request")
def test_draw_409_is_rejected_not_error(mock_request):
    mock_request.return_value = _response(409)
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.REJECTED


@patch("busybar.client.requests.request")
def test_draw_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.UNREACHABLE


@patch("busybar.client.requests.request")
def test_clear_scopes_to_app(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().clear("app") is True
    assert mock_request.call_args.kwargs["params"] == {"application_name": "app"}


@patch("busybar.client.requests.request")
def test_status_none_when_unreachable(mock_request):
    mock_request.side_effect = requests.Timeout()
    assert BusyBarClient().status() is None


@patch("busybar.client.requests.request")
def test_set_busy_simple_payload(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().set_busy_simple(90_000) is True
    body = mock_request.call_args.kwargs["json"]
    assert body == {
        "type": "SIMPLE",
        "card_id": "00000000-0000-0000-0000-000000000000",
        "time_left_ms": 90_000,
        "is_paused": False,
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'busybar.client'`

- [ ] **Step 3: Implement `src/busybar/client.py`**

```python
import logging
from enum import Enum

import requests

log = logging.getLogger(__name__)

NULL_CARD_ID = "00000000-0000-0000-0000-000000000000"


class DrawResult(Enum):
    DRAWN = "drawn"
    REJECTED = "rejected"        # 409: higher-priority app on screen — expected
    UNREACHABLE = "unreachable"  # device off / USB unplugged — caller backs off


class BusyBarClient:
    def __init__(self, host: str = "10.0.4.20", timeout: tuple = (3, 5)):
        self.base = f"http://{host}"
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> requests.Response | None:
        try:
            return requests.request(method, f"{self.base}{path}", timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            log.debug("device unreachable: %s", exc)
            return None

    def draw(self, application_name: str, elements: list[dict], priority: int = 50,
             led_notification_color: str | None = None) -> DrawResult:
        body: dict = {"application_name": application_name, "priority": priority,
                      "elements": elements}
        if led_notification_color is not None:
            body["led_notification_color"] = led_notification_color
        resp = self._request("POST", "/api/display/draw", json=body)
        if resp is None:
            return DrawResult.UNREACHABLE
        if resp.status_code == 409:
            return DrawResult.REJECTED
        if resp.status_code == 200:
            return DrawResult.DRAWN
        log.warning("draw failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return DrawResult.UNREACHABLE

    def clear(self, application_name: str) -> bool:
        resp = self._request("DELETE", "/api/display/draw",
                             params={"application_name": application_name})
        return resp is not None and resp.status_code == 200

    def status(self) -> dict | None:
        resp = self._request("GET", "/api/status")
        return resp.json() if resp is not None and resp.status_code == 200 else None

    def get_busy(self) -> dict | None:
        resp = self._request("GET", "/api/busy/snapshot")
        return resp.json() if resp is not None and resp.status_code == 200 else None

    def set_busy_simple(self, time_left_ms: int) -> bool:
        body = {"type": "SIMPLE", "card_id": NULL_CARD_ID,
                "time_left_ms": time_left_ms, "is_paused": False}
        resp = self._request("PUT", "/api/busy/snapshot", json=body)
        return resp is not None and resp.status_code == 200
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_client.py -v` — Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add src/busybar/client.py tests/test_client.py
git commit -m "client: add BusyBarClient with draw/clear/status/busy"
```

---

### Task 4: Calendar pure logic

**Files:**
- Create: `integrations/calendar_countdown/__init__.py` (empty), `integrations/calendar_countdown/logic.py`
- Test: `tests/test_calendar_logic.py`

**Interfaces:**
- Produces (consumed by Task 5):
  - `CalEvent` dataclass: `title: str, start: datetime, end: datetime, all_day: bool` (aware datetimes)
  - `select_next_event(events: list[CalEvent], now: datetime, lookahead_hours: int, include_all_day: bool) -> CalEvent | None` — earliest event with `start >= now` within lookahead
  - `select_active_event(events, now) -> CalEvent | None` — event with `start <= now < end` (for auto_busy)
  - `ascii_safe(s: str) -> str` — printable-ASCII projection, never empty (falls back to `"event"`)
  - `format_countdown(event: CalEvent, now: datetime) -> str` — e.g. `"14:00 Standup in 23m"`, hours as `"in 1h05m"`
  - `build_elements(text: str, warning: bool, timeout_s: int) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_calendar_logic.py
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import (
    CalEvent, select_next_event, select_active_event, ascii_safe,
    format_countdown, build_elements,
)

TZ = timezone.utc
NOW = datetime(2026, 8, 3, 13, 37, tzinfo=TZ)


def ev(offset_min: int, title: str = "Standup", dur_min: int = 30, all_day: bool = False) -> CalEvent:
    start = NOW + timedelta(minutes=offset_min)
    return CalEvent(title=title, start=start, end=start + timedelta(minutes=dur_min), all_day=all_day)


def test_selects_earliest_upcoming():
    assert select_next_event([ev(120), ev(23), ev(400)], NOW, 12, False).start == ev(23).start

def test_ignores_past_and_beyond_lookahead():
    assert select_next_event([ev(-10), ev(13 * 60)], NOW, 12, False) is None

def test_all_day_skipped_unless_enabled():
    events = [ev(60, all_day=True)]
    assert select_next_event(events, NOW, 12, False) is None
    assert select_next_event(events, NOW, 12, True) is not None

def test_active_event():
    assert select_active_event([ev(-5, dur_min=30)], NOW) is not None
    assert select_active_event([ev(5)], NOW) is None

def test_ascii_safe():
    assert ascii_safe("Café · Sync") == "Caf Sync"
    assert ascii_safe("日本語") == "event"

def test_format_countdown_minutes_and_hours():
    assert format_countdown(ev(23), NOW) == "14:00 Standup in 23m"
    assert format_countdown(ev(65), NOW) == "14:42 Standup in 1h05m"

def test_build_elements_shape():
    els = build_elements("hello", warning=False, timeout_s=90)
    assert els[0]["type"] == "text" and els[0]["font"] == "normal"
    assert els[0]["timeout"] == 90 and els[0]["color"] == "#FFFFFFFF"
    assert build_elements("x", warning=True, timeout_s=90)[0]["color"] == "#FFAA00FF"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_calendar_logic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'calendar_countdown'`

- [ ] **Step 3: Implement `integrations/calendar_countdown/logic.py`**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class CalEvent:
    title: str
    start: datetime
    end: datetime
    all_day: bool


def select_next_event(events: list[CalEvent], now: datetime,
                      lookahead_hours: int, include_all_day: bool) -> CalEvent | None:
    horizon = now + timedelta(hours=lookahead_hours)
    upcoming = [e for e in events
                if e.start >= now and e.start <= horizon
                and (include_all_day or not e.all_day)]
    return min(upcoming, key=lambda e: e.start) if upcoming else None


def select_active_event(events: list[CalEvent], now: datetime) -> CalEvent | None:
    active = [e for e in events if e.start <= now < e.end and not e.all_day]
    return min(active, key=lambda e: e.end) if active else None


def ascii_safe(s: str) -> str:
    cleaned = "".join(ch for ch in s if 0x20 <= ord(ch) <= 0x7E)
    cleaned = " ".join(cleaned.split())  # collapse runs left by stripped chars
    return cleaned or "event"


def format_countdown(event: CalEvent, now: datetime) -> str:
    minutes = int((event.start - now).total_seconds() // 60)
    if minutes >= 60:
        remain = f"in {minutes // 60}h{minutes % 60:02d}m"
    else:
        remain = f"in {minutes}m"
    local_start = event.start.astimezone()
    return f"{local_start:%H:%M} {ascii_safe(event.title)} {remain}"


def build_elements(text: str, warning: bool, timeout_s: int) -> list[dict]:
    return [{
        "id": "countdown",
        "type": "text",
        "text": ascii_safe(text),
        "font": "normal",
        "x": 0,
        "y": 4,
        "width": 72,
        "color": "#FFAA00FF" if warning else "#FFFFFFFF",
        "scroll_rate": 2000,
        "scroll_start_delay": 1000,
        "scroll_repeat_delay": 2000,
        "timeout": timeout_s,
    }]
```

Note: `format_countdown` renders start time in the **local** timezone (`astimezone()`); tests use UTC-aware inputs and the test machine's offset is irrelevant to the minutes math. If the `%H:%M` assertions fail locally due to TZ, set `TZ=UTC` for the test run in `ci.yml` and locally: `TZ=UTC uv run pytest …` — determinism matters more than prettiness here.

- [ ] **Step 4: Run to verify pass**

Run: `TZ=UTC uv run pytest tests/test_calendar_logic.py -v` — Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/calendar_countdown tests/test_calendar_logic.py
git commit -m "calendar: add pure selection/formatting logic"
```

---

### Task 5: EventKit adapter + calendar main loop

**Files:**
- Create: `integrations/calendar_countdown/eventkit.py`, `integrations/calendar_countdown/main.py`
- Test: `tests/test_calendar_loop.py`

**Interfaces:**
- Consumes: Task 3 client, Task 4 logic, Task 2 config.
- Produces: `run_once(client, fetch, cfg, now, dry_run) -> str` (returns a human-readable action summary; testable with fakes). CLI: `uv run python -m calendar_countdown.main [--once] [--dry-run]` run from `integrations/`.

- [ ] **Step 1: Write the failing test for the loop body**

```python
# tests/test_calendar_loop.py
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import CalEvent
from calendar_countdown.main import run_once
from busybar.client import DrawResult

TZ = timezone.utc
NOW = datetime(2026, 8, 3, 13, 37, tzinfo=TZ)
CFG = {"calendar_countdown": {"poll_seconds": 60, "lookahead_hours": 12,
                              "warn_minutes": 5, "include_all_day": False,
                              "auto_busy": False, "calendars": []}}


def make_event(offset_min: int) -> CalEvent:
    start = NOW + timedelta(minutes=offset_min)
    return CalEvent("Standup", start, start + timedelta(minutes=30), False)


def test_draws_countdown_for_upcoming_event():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    summary = run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False)
    client.draw.assert_called_once()
    kwargs = client.draw.call_args.kwargs
    assert kwargs["priority"] == 20
    assert "Standup in 23m" in kwargs["elements"][0]["text"]
    assert "drew" in summary


def test_clears_when_no_event():
    client = Mock()
    run_once(client, lambda hours: [], CFG, NOW, dry_run=False)
    client.clear.assert_called_once_with("calendar_countdown")
    client.draw.assert_not_called()


def test_dry_run_never_touches_device():
    client = Mock()
    summary = run_once(client, lambda hours: [make_event(3)], CFG, NOW, dry_run=True)
    client.draw.assert_not_called()
    client.clear.assert_not_called()
    assert "DRY-RUN" in summary
```

- [ ] **Step 2: Run to verify failure**

Run: `TZ=UTC uv run pytest tests/test_calendar_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'calendar_countdown.main'`

- [ ] **Step 3: Implement `integrations/calendar_countdown/eventkit.py`** (thin, untested adapter — all logic stays in logic.py)

```python
"""macOS EventKit adapter. Imported only on macOS, only from main()."""
import threading
from datetime import datetime, timedelta, timezone

from EventKit import (EKEventStore, EKEntityTypeEvent,
                      EKAuthorizationStatusFullAccess)

from .logic import CalEvent

_store: EKEventStore | None = None


def ensure_access() -> bool:
    """Request calendar access; returns True when full access is granted."""
    global _store
    _store = EKEventStore.alloc().init()
    status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
    if status == EKAuthorizationStatusFullAccess:
        return True
    done = threading.Event()
    result: list[bool] = [False]

    def _cb(granted, error):
        result[0] = bool(granted)
        done.set()

    _store.requestFullAccessToEventsWithCompletion_(_cb)
    done.wait(timeout=120)  # user is answering the macOS permission dialog
    return result[0]


def fetch_events(lookahead_hours: int, calendar_names: list[str]) -> list[CalEvent]:
    now = datetime.now(timezone.utc)
    calendars = _store.calendarsForEntityType_(EKEntityTypeEvent)
    if calendar_names:
        calendars = [c for c in calendars if c.title() in calendar_names]
    predicate = _store.predicateForEventsWithStartDate_endDate_calendars_(
        now, now + timedelta(hours=lookahead_hours), calendars)
    events = _store.eventsMatchingPredicate_(predicate) or []
    out = []
    for e in events:
        out.append(CalEvent(
            title=str(e.title() or "event"),
            start=datetime.fromtimestamp(e.startDate().timeIntervalSince1970(), tz=timezone.utc),
            end=datetime.fromtimestamp(e.endDate().timeIntervalSince1970(), tz=timezone.utc),
            all_day=bool(e.isAllDay()),
        ))
    return out
```

- [ ] **Step 4: Implement `integrations/calendar_countdown/main.py`**

```python
import argparse
import logging
import time
from datetime import datetime, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import load_config

from .logic import (build_elements, format_countdown,
                    select_active_event, select_next_event)

APP = "calendar_countdown"
PRIORITY = 20
log = logging.getLogger(APP)


def run_once(client, fetch, cfg: dict, now: datetime, dry_run: bool) -> str:
    c = cfg["calendar_countdown"]
    timeout_s = int(c["poll_seconds"] * 1.5)
    events = fetch(c["lookahead_hours"])
    nxt = select_next_event(events, now, c["lookahead_hours"], c["include_all_day"])

    if c["auto_busy"] and not dry_run:
        active = select_active_event(events, now)
        if active is not None:
            remaining_ms = int((active.end - now).total_seconds() * 1000)
            busy = client.get_busy() or {}
            if busy.get("type") in (None, "NOT_STARTED"):
                client.set_busy_simple(remaining_ms)

    if nxt is None:
        if not dry_run:
            client.clear(APP)
        return "no upcoming event; cleared"

    warning = (nxt.start - now).total_seconds() / 60 <= c["warn_minutes"]
    text = format_countdown(nxt, now)
    elements = build_elements(text, warning, timeout_s)
    if dry_run:
        return f"DRY-RUN would draw: {text!r} (warning={warning})"
    result = client.draw(APP, elements, priority=PRIORITY)
    return f"drew {text!r} -> {result.value}"


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar calendar countdown")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import eventkit  # macOS-only import kept out of module scope for tests
    if not eventkit.ensure_access():
        log.error("Calendar access denied. Grant access in System Settings > "
                  "Privacy & Security > Calendars, then rerun.")
        return 1

    cfg = load_config()
    client = BusyBarClient(host=cfg["device"]["host"])
    fetch = lambda hours: eventkit.fetch_events(hours, cfg["calendar_countdown"]["calendars"])

    backoff = 5
    while True:
        summary = run_once(client, fetch, cfg, datetime.now(timezone.utc), args.dry_run)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith(DrawResult.UNREACHABLE.value):
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(cfg["calendar_countdown"]["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run tests to verify pass**

Run: `TZ=UTC uv run pytest tests/test_calendar_loop.py -v` — Expected: 3 PASS

- [ ] **Step 6: Manual smoke test (device attached)**

Run from repo root: `cd integrations && uv run python -m calendar_countdown.main --once --dry-run`
Expected: macOS calendar permission prompt on first run, then a `DRY-RUN would draw: …` line naming your real next event. Then repeat without `--dry-run` and confirm the text appears on the bar.

- [ ] **Step 7: Commit**

```bash
git add integrations/calendar_countdown tests/test_calendar_loop.py
git commit -m "calendar: add EventKit adapter and polling loop"
```

---

### Task 6: CI status pure logic

**Files:**
- Create: `integrations/ci_status/__init__.py` (empty), `integrations/ci_status/logic.py`
- Test: `tests/test_ci_logic.py`

**Interfaces:**
- Produces (consumed by Task 8):
  - `RepoState` dataclass: `repo: str, failing: list[str], stuck: list[str]` (workflow names)
  - `evaluate_runs(repo: str, runs: list[dict], now: datetime, stale_queued_minutes: int) -> RepoState` — considers only the **latest run per `workflow_id`**; failing conclusions = `{"failure", "timed_out", "startup_failure"}`; stuck = `status == "queued"` and `created_at` older than threshold (0 disables)
  - `build_ci_payload(states: list[RepoState], show_green: bool, timeout_s: int) -> dict | None` — returns `{"elements": …, "priority": 60, "led": "#FF0000FF" | None}` or `None` meaning "clear the display"

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ci_logic.py
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from ci_status.logic import RepoState, evaluate_runs, build_ci_payload

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)


def run(workflow_id: int, name: str, status: str, conclusion: str | None,
        created_min_ago: int = 5) -> dict:
    created = (NOW - timedelta(minutes=created_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"workflow_id": workflow_id, "name": name, "status": status,
            "conclusion": conclusion, "created_at": created}


def test_failure_detected_on_latest_run_only():
    runs = [run(1, "tests", "completed", "success"),          # newest for wf 1
            run(1, "tests", "completed", "failure", 60),      # older failure — ignore
            run(2, "lint", "completed", "failure")]
    state = evaluate_runs("o/r", runs, NOW, 0)
    assert state.failing == ["lint"] and state.stuck == []


def test_stuck_queued_detection_respects_threshold():
    runs = [run(1, "tests", "queued", None, created_min_ago=20)]
    assert evaluate_runs("o/r", runs, NOW, 15).stuck == ["tests"]
    assert evaluate_runs("o/r", runs, NOW, 0).stuck == []       # disabled
    assert evaluate_runs("o/r", runs, NOW, 30).stuck == []      # under threshold


def test_payload_none_when_green_and_quiet():
    assert build_ci_payload([RepoState("o/r", [], [])], False, 180) is None


def test_payload_red_on_failure():
    payload = build_ci_payload([RepoState("o/r", ["tests"], [])], False, 180)
    assert payload["priority"] == 60 and payload["led"] == "#FF0000FF"
    assert "o/r" in payload["elements"][0]["text"]
    assert payload["elements"][0]["color"] == "#FF0000FF"


def test_payload_yellow_on_stuck_only():
    payload = build_ci_payload([RepoState("o/r", [], ["tests"])], False, 180)
    assert payload["led"] is None and payload["elements"][0]["color"] == "#FFFF00FF"
    assert "stuck" in payload["elements"][0]["text"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ci_logic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ci_status'`

- [ ] **Step 3: Implement `integrations/ci_status/logic.py`**

```python
from dataclasses import dataclass
from datetime import datetime, timezone

FAILING = {"failure", "timed_out", "startup_failure"}


@dataclass
class RepoState:
    repo: str
    failing: list[str]
    stuck: list[str]


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def evaluate_runs(repo: str, runs: list[dict], now: datetime,
                  stale_queued_minutes: int) -> RepoState:
    latest: dict[int, dict] = {}
    for r in runs:  # API returns newest first; keep first seen per workflow
        latest.setdefault(r["workflow_id"], r)
    failing, stuck = [], []
    for r in latest.values():
        if r.get("conclusion") in FAILING:
            failing.append(r["name"])
        elif r.get("status") == "queued" and stale_queued_minutes > 0:
            age_min = (now - _parse_ts(r["created_at"])).total_seconds() / 60
            if age_min >= stale_queued_minutes:
                stuck.append(r["name"])
    return RepoState(repo=repo, failing=sorted(failing), stuck=sorted(stuck))


def _element(text: str, color: str, timeout_s: int) -> dict:
    return {"id": "ci", "type": "text", "text": text, "font": "normal",
            "x": 0, "y": 4, "width": 72, "color": color,
            "scroll_rate": 2000, "scroll_start_delay": 1000,
            "scroll_repeat_delay": 2000, "timeout": timeout_s}


def build_ci_payload(states: list[RepoState], show_green: bool,
                     timeout_s: int) -> dict | None:
    failures = [(s.repo, name) for s in states for name in s.failing]
    stuck = [(s.repo, name) for s in states for name in s.stuck]
    if failures:
        text = "CI FAIL " + " ".join(f"{repo}:{name}" for repo, name in failures)
        return {"elements": [_element(text, "#FF0000FF", timeout_s)],
                "priority": 60, "led": "#FF0000FF"}
    if stuck:
        text = "CI stuck " + " ".join(f"{repo}:{name}" for repo, name in stuck)
        return {"elements": [_element(text, "#FFFF00FF", timeout_s)],
                "priority": 60, "led": None}
    if show_green:
        return {"elements": [_element("CI ok", "#00FF00FF", timeout_s)],
                "priority": 60, "led": None}
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ci_logic.py -v` — Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/ci_status tests/test_ci_logic.py
git commit -m "ci: add run evaluation and display payload logic"
```

---

### Task 7: GitHub REST poller with ETag caching

**Files:**
- Create: `integrations/ci_status/github.py`
- Test: `tests/test_ci_github.py`

**Interfaces:**
- Consumes: nothing internal (requests + subprocess).
- Produces (consumed by Task 8):
  - `get_token() -> str` — runs `gh auth token`; raises `RuntimeError` with an actionable message if `gh` is missing or unauthenticated. Token lives in memory only.
  - `RestPoller(token: str)` with `.fetch_runs(repo: str) -> list[dict] | None` — GET `https://api.github.com/repos/{repo}/actions/runs?per_page=10` with `If-None-Match` when an ETag is cached. Returns the `workflow_runs` list on 200 (and caches the new ETag), `None` on 304 (“no change”), and `None` on any error (treat as no-change; never raise from the poll loop).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ci_github.py
from unittest.mock import Mock, patch
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from ci_status.github import RestPoller, get_token


def _response(status: int, body: dict | None = None, etag: str | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.headers = {"ETag": etag} if etag else {}
    return resp


@patch("ci_status.github.requests.get")
def test_fetch_returns_runs_and_caches_etag(mock_get):
    mock_get.return_value = _response(200, {"workflow_runs": [{"id": 1}]}, etag='W/"abc"')
    poller = RestPoller("tok")
    assert poller.fetch_runs("o/r") == [{"id": 1}]
    first_headers = mock_get.call_args.kwargs["headers"]
    assert "If-None-Match" not in first_headers
    assert first_headers["Authorization"] == "Bearer tok"
    assert "graphql" not in mock_get.call_args.args[0]

    mock_get.return_value = _response(304)
    assert poller.fetch_runs("o/r") is None  # 304 -> no change
    assert mock_get.call_args.kwargs["headers"]["If-None-Match"] == 'W/"abc"'


@patch("ci_status.github.requests.get")
def test_fetch_swallows_network_errors(mock_get):
    mock_get.side_effect = requests.ConnectionError()
    assert RestPoller("tok").fetch_runs("o/r") is None


@patch("ci_status.github.subprocess.run")
def test_get_token_error_is_actionable(mock_run):
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="not logged in")
    try:
        get_token()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "gh auth login" in str(exc)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ci_github.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ci_status.github'`

- [ ] **Step 3: Implement `integrations/ci_status/github.py`**

```python
"""GitHub REST access. REST ONLY — GraphQL is prohibited in this repo
(see spec: operator GraphQL quota exhaustion). Conditional requests keep
steady-state REST quota usage near zero (304s don't count)."""
import logging
import subprocess

import requests

log = logging.getLogger("ci_status")
API = "https://api.github.com"


def get_token() -> str:
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True,
                              text=True, timeout=10)
    except FileNotFoundError:
        raise RuntimeError("GitHub CLI not found. Install gh, then run: gh auth login")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"gh has no stored auth ({proc.stderr.strip()}). "
                           "Run: gh auth login")
    return proc.stdout.strip()


class RestPoller:
    def __init__(self, token: str):
        self._token = token
        self._etags: dict[str, str] = {}

    def fetch_runs(self, repo: str) -> list[dict] | None:
        url = f"{API}/repos/{repo}/actions/runs"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if repo in self._etags:
            headers["If-None-Match"] = self._etags[repo]
        try:
            resp = requests.get(url, headers=headers,
                                params={"per_page": 10}, timeout=(5, 15))
        except requests.RequestException as exc:
            log.debug("github unreachable: %s", exc)
            return None
        if resp.status_code == 304:
            return None
        if resp.status_code != 200:
            log.warning("github %s for %s", resp.status_code, repo)
            return None
        if "ETag" in resp.headers:
            self._etags[repo] = resp.headers["ETag"]
        return resp.json().get("workflow_runs", [])
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_ci_github.py -v` — Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add integrations/ci_status/github.py tests/test_ci_github.py
git commit -m "ci: add REST-only GitHub poller with ETag caching"
```

---

### Task 8: CI status main loop

**Files:**
- Create: `integrations/ci_status/main.py`
- Test: `tests/test_ci_loop.py`

**Interfaces:**
- Consumes: Tasks 3, 6, 7 (`BusyBarClient`, `evaluate_runs`/`build_ci_payload`, `RestPoller`).
- Produces: `run_once(client, poller, cfg, now, state_cache, dry_run) -> str`. `state_cache: dict[str, RepoState]` persists last-known state across polls so a `304` keeps the previous verdict. CLI: `uv run python -m ci_status.main [--once] [--dry-run]` from `integrations/`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ci_loop.py
from datetime import datetime, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from busybar.client import DrawResult
from ci_status.main import run_once

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)
CFG = {"ci_status": {"poll_seconds": 120, "repos": ["o/r"],
                     "show_green": False, "stale_queued_minutes": 0}}


def _run(conclusion: str) -> dict:
    return {"workflow_id": 1, "name": "tests", "status": "completed",
            "conclusion": conclusion, "created_at": "2026-08-03T13:30:00Z"}


def test_draws_red_on_failure():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == 60
    assert client.draw.call_args.kwargs["led_notification_color"] == "#FF0000FF"
    assert "FAIL" in summary


def test_clears_when_green():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False)
    client.clear.assert_called_once_with("ci_status")


def test_304_keeps_previous_state():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = None  # 304 / error
    cache = {}
    # Seed cache via an initial failing poll, then a 304 poll must still draw red.
    poller_seed = Mock(); poller_seed.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller_seed, CFG, NOW, cache, dry_run=False)
    client.reset_mock(); client.draw.return_value = DrawResult.DRAWN
    run_once(client, poller, CFG, NOW, cache, dry_run=False)
    client.draw.assert_called_once()


def test_dry_run_touches_nothing():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=True)
    client.draw.assert_not_called(); client.clear.assert_not_called()
    assert "DRY-RUN" in summary
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_ci_loop.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ci_status.main'`

- [ ] **Step 3: Implement `integrations/ci_status/main.py`**

```python
import argparse
import logging
import time
from datetime import datetime, timezone

from busybar.client import BusyBarClient
from busybar.config import load_config

from .logic import RepoState, build_ci_payload, evaluate_runs

APP = "ci_status"
log = logging.getLogger(APP)


def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool) -> str:
    c = cfg["ci_status"]
    timeout_s = int(c["poll_seconds"] * 1.5)
    for repo in c["repos"]:
        runs = poller.fetch_runs(repo)
        if runs is not None:  # None = 304/no-change/error -> keep cached state
            state_cache[repo] = evaluate_runs(repo, runs, now,
                                              c["stale_queued_minutes"])
    payload = build_ci_payload(list(state_cache.values()), c["show_green"], timeout_s)
    if dry_run:
        return f"DRY-RUN payload: {payload!r}"
    if payload is None:
        client.clear(APP)
        return "all green; cleared"
    result = client.draw(APP, payload["elements"], priority=payload["priority"],
                         led_notification_color=payload["led"])
    return f"{payload['elements'][0]['text'][:40]!r} -> {result.value}"


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar CI status")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    if not cfg["ci_status"]["repos"]:
        log.error("No repos configured. Copy config.example.toml to config.toml "
                  "and set [ci_status] repos.")
        return 1
    from .github import RestPoller, get_token
    try:
        poller = RestPoller(get_token())
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    client = BusyBarClient(host=cfg["device"]["host"])

    state_cache: dict[str, RepoState] = {}
    backoff = 5
    while True:
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith("unreachable"):  # device offline: back off, not full poll
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(cfg["ci_status"]["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_ci_loop.py -v` — Expected: 4 PASS

- [ ] **Step 5: Manual smoke test**

`cd integrations && uv run python -m ci_status.main --once --dry-run` (with a real repo in local `config.toml`)
Expected: logs the REST-derived payload (or `None` when green) without touching device or display. Then without `--dry-run` against the bar.

- [ ] **Step 6: Commit**

```bash
git add integrations/ci_status/main.py tests/test_ci_loop.py
git commit -m "ci: add polling loop with state cache and dry-run"
```

---

### Task 9: launchd plists + integration READMEs

**Files:**
- Create: `integrations/calendar_countdown/com.busybar.calendar-countdown.plist`, `integrations/ci_status/com.busybar.ci-status.plist`, `integrations/calendar_countdown/README.md`, `integrations/ci_status/README.md`

**Interfaces:**
- Consumes: CLI entry points from Tasks 5 and 8.
- Produces: installable LaunchAgents; per-integration docs.

- [ ] **Step 1: Write the calendar plist** (template — `__REPO__` and `__UV__` replaced at install)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.busybar.calendar-countdown</string>
  <key>WorkingDirectory</key><string>__REPO__/integrations</string>
  <key>ProgramArguments</key>
  <array>
    <string>__UV__</string>
    <string>run</string>
    <string>python</string>
    <string>-m</string>
    <string>calendar_countdown.main</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/busybar-calendar.log</string>
  <key>StandardErrorPath</key><string>/tmp/busybar-calendar.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Write the CI plist** (`integrations/ci_status/com.busybar.ci-status.plist`)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.busybar.ci-status</string>
  <key>WorkingDirectory</key><string>__REPO__/integrations</string>
  <key>ProgramArguments</key>
  <array>
    <string>__UV__</string>
    <string>run</string>
    <string>python</string>
    <string>-m</string>
    <string>ci_status.main</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/busybar-ci.log</string>
  <key>StandardErrorPath</key><string>/tmp/busybar-ci.log</string>
</dict>
</plist>
```

- [ ] **Step 3: Write `integrations/calendar_countdown/README.md`**

Contents (write in full, this outline is binding): What it does (one paragraph + the display format example `14:00 Standup in 23m`); Requirements (macOS, calendar permission, device reachable); Setup (`cp config.example.toml config.toml`, edit `[calendar_countdown]`, `cd integrations && uv run python -m calendar_countdown.main --once --dry-run` first); Config reference table for the six keys with defaults; Autostart:

```bash
sed -e "s|__REPO__|$(git rev-parse --show-toplevel)|" -e "s|__UV__|$(command -v uv)|" \
  com.busybar.calendar-countdown.plist > ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.busybar.calendar-countdown.plist
```

plus the matching `launchctl bootout` uninstall line. Note that the permission prompt must be answered once in a foreground run before installing the agent.

- [ ] **Step 4: Write `integrations/ci_status/README.md`**

Same structure. Must additionally state: REST-only design and why (GraphQL quota isolation), ETag/304 behavior (steady-state ≈ zero quota), that auth comes from `gh auth login` (no token in config), both GitHub-hosted and self-hosted runners are covered, and what `stale_queued_minutes` catches (offline self-hosted runner ⇒ yellow "CI stuck").

- [ ] **Step 5: Verify plists parse**

Run: `plutil -lint integrations/calendar_countdown/*.plist integrations/ci_status/*.plist`
Expected: both `OK`

- [ ] **Step 6: Commit**

```bash
git add integrations/*/README.md integrations/*/*.plist
git commit -m "docs: add launchd agents and per-integration READMEs"
```

---

### Task 10: Root README, CI workflow, final verification

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md` (replace stub)

**Interfaces:**
- Consumes: everything.

- [ ] **Step 1: Write `.github/workflows/ci.yml`**

```yaml
name: ci
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    env:
      TZ: UTC
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --group dev
      - run: uv run pytest -v

  secret-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

(Note: pyobjc has a `sys_platform == 'darwin'` marker, so `uv sync` on ubuntu skips it; no test imports EventKit at module scope — that is why `eventkit.py` is imported inside `main()` only.)

- [ ] **Step 2: Replace root `README.md`** following the operator's public-repo house style (purpose → requirements → setup → configuration → index). Write in full:

- Title + one-line pitch: local-API integrations for the BUSY Bar.
- What's inside: table linking `integrations/calendar_countdown/` and `integrations/ci_status/` with one-line descriptions.
- Requirements: BUSY Bar on USB (default `10.0.4.20`) or LAN, Python 3.12+, `uv`; per-integration extras noted in their READMEs.
- Quick start: clone → `uv sync` → `cp config.example.toml config.toml` → run an integration with `--once --dry-run`.
- How it works: 4-sentence primer on `display/draw`, `application_name`, priority arbitration (20/60/90 table), and element `timeout` self-clearing.
- Adding an integration: point at `src/busybar/client.py` + the logic/adapter split convention.
- Note that `config.toml` is git-ignored and the repo policy is no-secrets-by-construction.

- [ ] **Step 3: Full-suite + sanitization sweep**

Run: `TZ=UTC uv run pytest -v` — Expected: all pass (≈28 tests).
Run: `git grep -InE '10\.0\.4\.(2[1-9]|[3-9][0-9])|192\.168\.|sumitake/|josumi' -- . ':!docs/superpowers'` — Expected: **no output** (no personal LAN IPs, usernames, or real repo names outside spec/plan docs).

- [ ] **Step 4: Commit**

```bash
git add README.md .github
git commit -m "docs: add root README and CI (pytest + gitleaks)"
```

- [ ] **Step 5: Stop — do not push.** Pushing to the public repo is operator-gated; report completion and ask.
