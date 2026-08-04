from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import CalEvent, _format_countdown
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
    event = make_event(23)
    summary = run_once(client, lambda hours: [event], CFG, NOW, dry_run=False)
    client.draw.assert_called_once()
    kwargs = client.draw.call_args.kwargs
    assert kwargs["priority"] == 20
    by_id = {el["id"]: el for el in kwargs["elements"]}
    # v1.4 "airy": no card elements.
    assert set(by_id) == {"bg", "title", "track", "track_fill", "time", "divider", "cd_text"}
    assert by_id["cd_text"]["text"] == _format_countdown((event.start - NOW).total_seconds() / 60)
    assert by_id["title"]["text"] == "STANDUP"   # uppercased after ascii_safe
    assert "drew" in summary

def test_draws_in_progress_event_targeting_active_over_upcoming():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    active = make_event(-5, dur_min=30, title="Active")
    upcoming = make_event(120, title="Later")
    summary = run_once(client, lambda hours: [active, upcoming], CFG, NOW, dry_run=False)
    kwargs = client.draw.call_args.kwargs
    by_id = {el["id"]: el for el in kwargs["elements"]}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "ends", "divider", "cd_text"}
    assert by_id["title"]["text"] == "ACTIVE"
    assert by_id["cd_text"]["text"] == _format_countdown((active.end - NOW).total_seconds() / 60)
    assert "time" not in by_id
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

def test_failed_draw_leaves_state_unchanged_and_retries_next_poll():
    # (a) A transition poll whose draw() doesn't land (UNREACHABLE here, but
    # the same reasoning applies to REJECTED/ERROR) must not commit `state`
    # -- otherwise no future poll would ever retry the clear+draw pair, and
    # a stale element set from before the transition would persist
    # indefinitely instead of just until its own bounded timeout.
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    assert state["in_progress"] is False

    active = make_event(-5, dur_min=30, title="Active")
    fetch = lambda hours: [active]

    client.draw.return_value = DrawResult.UNREACHABLE
    run_once(client, fetch, CFG, NOW, dry_run=False, state=state)
    assert state["in_progress"] is False   # unchanged: draw never landed
    assert client.clear.call_count == 1    # transition was still detected and clear attempted
    assert client.draw.call_count == 2

    # Next poll: state still mismatches (unchanged above), so it retries
    # clear() then draw(); this time draw succeeds and state finally commits.
    client.draw.return_value = DrawResult.DRAWN
    run_once(client, fetch, CFG, NOW, dry_run=False, state=state)
    assert client.clear.call_count == 2
    assert client.draw.call_count == 3
    assert state["in_progress"] is True

def test_clear_failure_does_not_block_state_commit_when_draw_succeeds():
    # (b) clear()'s own return value is intentionally ignored -- only
    # draw()'s result gates the state commit (see the comment in
    # main.run_once). If clear() fails but draw() still lands the new
    # element set, state should commit as transitioned: any leftover stale
    # ids are bounded by their own original timeout (a one-off, self-healing
    # gap), and gating on clear() too would make a persistently-failing
    # clear() retry every poll forever even once draws keep succeeding.
    client = Mock()
    client.clear.return_value = False
    client.draw.return_value = DrawResult.DRAWN
    state = {}
    run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False, state=state)
    active = make_event(-5, dur_min=30, title="Active")
    run_once(client, lambda hours: [active], CFG, NOW, dry_run=False, state=state)
    client.clear.assert_called_once_with("calendar_countdown")
    assert state["in_progress"] is True

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
