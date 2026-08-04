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
                              "warn_minutes": 5, "notice_minutes": 15,
                              "progress_window_minutes": 60,
                              "include_all_day": False,
                              "auto_busy": False, "calendars": []}}


def make_event(offset_min: int, dur_min: int = 30, title: str = "Standup") -> CalEvent:
    start = NOW + timedelta(minutes=offset_min)
    return CalEvent(title, start, start + timedelta(minutes=dur_min), False)


def test_draws_countdown_for_upcoming_event():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    summary = run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False)
    client.draw.assert_called_once()
    kwargs = client.draw.call_args.kwargs
    assert kwargs["priority"] == 20
    by_id = {el["id"]: el for el in kwargs["elements"]}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "time_card", "time", "divider", "cd_card", "countdown"}
    assert by_id["countdown"]["timestamp"] == str(int(make_event(23).start.timestamp()))
    assert by_id["title"]["text"] == "Standup"
    assert "drew" in summary

def test_draws_in_progress_event_targeting_active_over_upcoming():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    active = make_event(-5, dur_min=30, title="Active")
    upcoming = make_event(120, title="Later")
    summary = run_once(client, lambda hours: [active, upcoming], CFG, NOW, dry_run=False)
    kwargs = client.draw.call_args.kwargs
    by_id = {el["id"]: el for el in kwargs["elements"]}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "ends", "divider", "cd_card", "countdown"}
    assert by_id["title"]["text"] == "Active"
    assert by_id["countdown"]["timestamp"] == str(int(active.end.timestamp()))
    assert "time" not in by_id and "time_card" not in by_id
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


# --- state-transition clearing ------------------------------------------------
#
# The upcoming and in-progress layouts use different element id sets
# (time_card+time vs ends) and the device's draw endpoint upserts by id
# rather than replacing an app's whole element set (confirmed on-device: a
# stale opaque time_card rendered behind the new "ends" text for ~30s after
# a transition, until captured/fixed -- see display-v1.3-report.md). `state`
# lets run_once clear only at the transition, not every poll.

def test_no_clear_on_first_draw_with_fresh_state():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_not_called()
    assert state["in_progress"] is False

def test_no_clear_across_polls_with_same_state():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    run_once(client, lambda hours: [make_event(22)], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_not_called()

def test_clears_on_upcoming_to_in_progress_transition():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_called_once_with("calendar_countdown")
    assert state["in_progress"] is True

def test_clears_on_in_progress_to_upcoming_transition():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_called_once_with("calendar_countdown")

def test_no_transition_clear_when_state_omitted():
    # Backward-compatible default: callers (and the other tests above) that
    # don't pass state see the pre-fix behavior -- no extra clear calls.
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False)
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False)
    client.clear.assert_not_called()

def test_state_reset_after_no_event_clear():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    run_once(client, lambda hours: [], CFG, NOW, dry_run=False, state=state)  # clears, resets state
    assert state["in_progress"] is None
    client.clear.reset_mock()
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    # No stale elements remain after the "no event" clear -- no extra clear needed.
    client.clear.assert_not_called()
