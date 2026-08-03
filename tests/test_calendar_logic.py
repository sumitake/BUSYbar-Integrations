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
    e23, e65 = ev(23), ev(65)
    assert format_countdown(e23, NOW) == f"{e23.start.astimezone():%H:%M} Standup in 23m"
    assert format_countdown(e65, NOW) == f"{e65.start.astimezone():%H:%M} Standup in 1h05m"

def test_build_elements_shape():
    els = build_elements("hello", warning=False, timeout_s=90)
    assert els[0]["type"] == "text" and els[0]["font"] == "normal"
    assert els[0]["timeout"] == 90 and els[0]["color"] == "#FFFFFFFF"
    assert build_elements("x", warning=True, timeout_s=90)[0]["color"] == "#FFAA00FF"
