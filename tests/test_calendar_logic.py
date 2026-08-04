from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import (
    CalEvent, select_next_event, select_active_event, ascii_safe,
    build_elements, _bar_height, _urgency_color, _title_fits,
    COLOR_WHITE, COLOR_GRAY, COLOR_AMBER, COLOR_RED, COLOR_TEAL,
)

TZ = timezone.utc
NOW = datetime(2026, 8, 3, 13, 37, tzinfo=TZ)
CFG = {"progress_window_minutes": 60, "notice_minutes": 15, "warn_minutes": 5}


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


# --- bar height math -------------------------------------------------------

def test_bar_height_full_when_at_or_beyond_window():
    assert _bar_height(60, 60) == 16
    assert _bar_height(90, 60) == 16

def test_bar_height_scales_within_window():
    assert _bar_height(30, 60) == 8   # half window -> half height
    assert _bar_height(15, 60) == 4

def test_bar_height_clamped_min_one():
    assert _bar_height(0, 60) == 1
    assert _bar_height(-5, 60) == 1   # defensive: never negative height

def test_bar_height_window_disabled_defensively_full():
    assert _bar_height(5, 0) == 16


# --- urgency thresholds ------------------------------------------------------

def test_urgency_white_above_notice():
    assert _urgency_color(20, notice_minutes=15, warn_minutes=5) == COLOR_WHITE

def test_urgency_amber_at_and_below_notice():
    assert _urgency_color(15, notice_minutes=15, warn_minutes=5) == COLOR_AMBER
    assert _urgency_color(10, notice_minutes=15, warn_minutes=5) == COLOR_AMBER

def test_urgency_red_at_and_below_warn():
    assert _urgency_color(5, notice_minutes=15, warn_minutes=5) == COLOR_RED
    assert _urgency_color(0, notice_minutes=15, warn_minutes=5) == COLOR_RED


# --- title fit / scroll decision --------------------------------------------

def test_title_fits_short_string():
    assert _title_fits("Sync", 46) is True

def test_title_does_not_fit_long_string():
    assert _title_fits("A Very Long Meeting Title Indeed", 46) is False


# --- build_elements: upcoming event ------------------------------------------

def test_build_elements_upcoming_shape():
    e = ev(23)
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert set(by_id) == {"bar", "time", "title", "countdown"}

    bar = by_id["bar"]
    assert bar["type"] == "rectangle" and bar["fill"] == "solid"
    assert bar["x"] == 0 and bar["width"] == 2
    assert bar["fill_colors"] == [COLOR_WHITE]
    assert bar["y"] + bar["height"] == 16  # anchored to bottom
    # RectangleElement's default 1px white border swallows a 2px-wide bar
    # entirely (found on-device) -- must be explicitly disabled.
    assert bar["border_width"] == 0

    time_el = by_id["time"]
    assert time_el["type"] == "text" and time_el["font"] == "tiny"
    assert time_el["color"] == COLOR_GRAY and time_el["x"] == 4 and time_el["y"] == 0

    title = by_id["title"]
    assert title["type"] == "text" and title["font"] == "small"
    assert title["text"] == "Standup" and title["color"] == COLOR_WHITE

    cd = by_id["countdown"]
    assert cd["type"] == "countdown"
    assert cd["timestamp"] == str(int(e.start.timestamp()))
    assert isinstance(cd["timestamp"], str)
    assert cd["direction"] == "time_left" and cd["show_hours"] == "when_non_zero"
    assert cd["color"] == COLOR_WHITE
    assert "font" not in cd  # firmware-fixed font; must not be sent

def test_build_elements_urgency_colors_bar_and_countdown():
    amber_event = ev(10)   # within notice_minutes=15, outside warn_minutes=5
    els = build_elements(amber_event, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["bar"]["fill_colors"] == [COLOR_AMBER]
    assert by_id["countdown"]["color"] == COLOR_AMBER
    assert by_id["title"]["color"] == COLOR_WHITE  # title stays white regardless of urgency

    red_event = ev(3)      # within warn_minutes=5
    els = build_elements(red_event, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["bar"]["fill_colors"] == [COLOR_RED]
    assert by_id["countdown"]["color"] == COLOR_RED

def test_build_elements_long_title_scrolls_short_title_static():
    long_e = ev(23, title="A Very Long Meeting Title That Will Not Fit On Screen")
    els = build_elements(long_e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    assert title.get("scroll_rate") == 2000

    short_e = ev(23, title="Sync")
    els = build_elements(short_e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    assert "scroll_rate" not in title


# --- build_elements: in-progress event ---------------------------------------

def test_build_elements_in_progress_shape():
    e = ev(-5, dur_min=30, title="Standup")
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=True)
    by_id = {el["id"]: el for el in els}
    assert "time" not in by_id  # no start-time label while in progress

    bar = by_id["bar"]
    assert bar["fill_colors"] == [COLOR_TEAL]
    assert bar["height"] == 16 and bar["y"] == 0  # full height

    cd = by_id["countdown"]
    assert cd["timestamp"] == str(int(e.end.timestamp()))  # counts down to END
    assert cd["color"] == COLOR_TEAL

    title = by_id["title"]
    assert title["text"] == "Standup"
