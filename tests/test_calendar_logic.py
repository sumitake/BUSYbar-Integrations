from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import (
    CalEvent, select_next_event, select_active_event, ascii_safe,
    build_elements, _track_fill_width, _state_for, _title_fits,
    _format_countdown,
    STATE_NORMAL, STATE_NOTICE, STATE_WARNING, STATE_IN_PROGRESS, STATES,
    BG_GRADIENT, TITLE_COLOR, TRACK_COLOR, TRACK_FILL_GRADIENT,
    TRACK_FILL_IN_PROGRESS, TIME_CARD_COLOR, TIME_TEXT_COLOR,
    ENDS_TEXT_COLOR, ENDS_TEXT, DIVIDER_COLOR, CD_CARD_COLOR, DIGIT_COLOR,
    PANEL_WIDTH, PANEL_HEIGHT, CD_TEXT_X, CD_TEXT_CHAR_PX, CD_TEXT_MAX_WIDTH,
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


# --- state selection ----------------------------------------------------------

def test_state_normal_above_notice():
    assert _state_for(20, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_NORMAL

def test_state_notice_at_and_below_notice_threshold():
    assert _state_for(15, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_NOTICE
    assert _state_for(10, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_NOTICE

def test_state_warning_at_and_below_warn_threshold():
    assert _state_for(5, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_WARNING
    assert _state_for(0, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_WARNING
    assert _state_for(-3, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_WARNING

def test_state_in_progress_overrides_thresholds():
    # Even minutes_left values that would read "normal" by threshold alone
    # must resolve to in_progress once in_progress=True.
    assert _state_for(999, notice_minutes=15, warn_minutes=5, in_progress=True) == STATE_IN_PROGRESS


# --- palette completeness ------------------------------------------------------

def test_every_state_has_a_palette_entry_in_every_state_keyed_table():
    tables = [BG_GRADIENT, TITLE_COLOR, TRACK_FILL_GRADIENT, DIVIDER_COLOR, CD_CARD_COLOR, DIGIT_COLOR]
    for table in tables:
        # TRACK_FILL_GRADIENT intentionally omits in_progress (solid fill instead).
        expected = set(STATES) - {STATE_IN_PROGRESS} if table is TRACK_FILL_GRADIENT else set(STATES)
        assert set(table) == expected


# --- track-fill drain math ------------------------------------------------------

def test_track_fill_full_when_at_or_beyond_window():
    assert _track_fill_width(60, 60) == PANEL_WIDTH
    assert _track_fill_width(90, 60) == PANEL_WIDTH

def test_track_fill_scales_within_window():
    assert _track_fill_width(30, 60) == 36   # half window -> half width (72 * 0.5)
    assert _track_fill_width(15, 60) == 18

def test_track_fill_clamped_min_one():
    assert _track_fill_width(0, 60) == 1
    assert _track_fill_width(-5, 60) == 1   # defensive: never negative width

def test_track_fill_window_disabled_defensively_full():
    assert _track_fill_width(5, 0) == PANEL_WIDTH


# --- title fit / scroll decision --------------------------------------------

def test_title_fits_short_string():
    assert _title_fits("Sync", 70) is True

def test_title_does_not_fit_long_string():
    assert _title_fits("A Very Long Meeting Title That Does Not Fit At All", 70) is False


# --- countdown text formatting ------------------------------------------------
#
# _format_countdown feeds the cd_text element (a plain `text` element,
# extra_large font, replacing the native `countdown` element -- see
# logic.py's CD_TEXT_X comment for why). Floored to whole minutes; switches
# to an hour-only form at 10+ hours so the rendered text never overflows the
# space available in cd_card (see the width tests below).

def test_countdown_format_under_an_hour():
    assert _format_countdown(0) == "0m"
    assert _format_countdown(1) == "1m"
    assert _format_countdown(54.7) == "54m"   # floored, not rounded

def test_countdown_format_boundary_59_60_minutes():
    assert _format_countdown(59) == "59m"
    assert _format_countdown(59.9) == "59m"   # still floors to 59, not 60
    assert _format_countdown(60) == "1h00m"

def test_countdown_format_hours_and_minutes():
    assert _format_countdown(65) == "1h05m"
    assert _format_countdown(125) == "2h05m"

def test_countdown_format_boundary_9h59_10h():
    assert _format_countdown(9 * 60 + 59) == "9h59m"    # 599 min -> full form
    assert _format_countdown(10 * 60) == "10h"          # 600 min -> hour-only form
    assert _format_countdown(10 * 60 + 59) == "10h"      # still hour-only just past the boundary

def test_countdown_format_clamps_negative_to_zero():
    assert _format_countdown(-5) == "0m"

def test_countdown_format_never_overflows_card_width():
    # Sweep a wide range of inputs (including the worst case within each
    # branch: 9h59m for the full form, and just past the 10h cutover) and
    # verify every produced string fits CD_TEXT_MAX_WIDTH at the measured
    # per-glyph width.
    samples_minutes = [0, 1, 30, 59, 60, 65, 125, 9 * 60 + 59, 10 * 60, 12 * 60, 23 * 60 + 59]
    for minutes in samples_minutes:
        text = _format_countdown(minutes)
        assert len(text) * CD_TEXT_CHAR_PX <= CD_TEXT_MAX_WIDTH, (minutes, text)

def test_countdown_full_form_at_10h_would_have_overflowed():
    # Documents *why* the >=10h rule exists: without it, "10h00m" (the full
    # <H>h<MM>m form at 2-digit hours) is 6 glyphs, same length as the
    # "12h00m" measured on-device at ~37px -- wider than CD_TEXT_MAX_WIDTH.
    hypothetical_full_form = "10h00m"
    assert len(hypothetical_full_form) * CD_TEXT_CHAR_PX > CD_TEXT_MAX_WIDTH
    # The actual rule avoids it:
    assert _format_countdown(10 * 60) == "10h"


# --- build_elements: upcoming event ------------------------------------------

def test_build_elements_upcoming_shape():
    e = ev(23)
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "time_card", "time", "divider", "cd_card", "cd_text"}
    # Draw order is z-order (first = behind); set-membership alone wouldn't
    # catch a reorder that visually breaks layering (e.g. cd_card drawn
    # after cd_text would hide the countdown digits behind its fill).
    assert [el["id"] for el in els] == [
        "bg", "title", "track", "track_fill", "time_card", "time", "divider", "cd_card", "cd_text",
    ]

    bg = by_id["bg"]
    assert bg["type"] == "rectangle" and bg["x"] == 0 and bg["y"] == 0
    assert bg["width"] == PANEL_WIDTH and bg["height"] == PANEL_HEIGHT
    assert bg["fill"] == "gradient_v" and bg["fill_colors"] == BG_GRADIENT[STATE_NORMAL]
    assert bg["border_width"] == 0

    title = by_id["title"]
    assert title["type"] == "text" and title["font"] == "small"
    assert title["text"] == "STANDUP" and title["color"] == TITLE_COLOR[STATE_NORMAL]
    assert title["x"] == 1 and title["y"] == -2 and title["width"] == 70

    track = by_id["track"]
    assert track["type"] == "rectangle" and track["fill"] == "solid"
    assert track["fill_colors"] == [TRACK_COLOR]
    assert track["x"] == 0 and track["y"] == 5 and track["width"] == PANEL_WIDTH and track["height"] == 1
    # RectangleElement's default 1px white border would swallow this 1px-tall
    # track entirely (the same gotcha found on the v1.1 progress bar) --
    # must be explicitly disabled.
    assert track["border_width"] == 0

    track_fill = by_id["track_fill"]
    assert track_fill["fill"] == "gradient_h"
    assert track_fill["fill_colors"] == TRACK_FILL_GRADIENT[STATE_NORMAL]
    assert track_fill["x"] == 0 and track_fill["y"] == 5 and track_fill["height"] == 1
    assert track_fill["border_width"] == 0

    time_card = by_id["time_card"]
    assert time_card["type"] == "rectangle" and time_card["radius"] == 1
    assert time_card["x"] == 0 and time_card["y"] == 6 and time_card["width"] == 34 and time_card["height"] == 10
    assert time_card["fill"] == "solid" and time_card["fill_colors"] == [TIME_CARD_COLOR]
    assert time_card["border_width"] == 0

    time_el = by_id["time"]
    assert time_el["type"] == "text" and time_el["font"] == "extra_large"
    assert time_el["color"] == TIME_TEXT_COLOR and time_el["x"] == 1 and time_el["y"] == 4
    assert time_el["text"] == f"{e.start.astimezone():%H:%M}"

    divider = by_id["divider"]
    assert divider["x"] == 34 and divider["y"] == 6 and divider["width"] == 2 and divider["height"] == 10
    assert divider["fill_colors"] == [DIVIDER_COLOR[STATE_NORMAL]]
    assert divider["border_width"] == 0

    cd_card = by_id["cd_card"]
    assert cd_card["x"] == 36 and cd_card["y"] == 6 and cd_card["width"] == 36 and cd_card["height"] == 10
    assert cd_card["radius"] == 1
    assert cd_card["fill_colors"] == [CD_CARD_COLOR[STATE_NORMAL]]
    assert cd_card["border_width"] == 0

    cd = by_id["cd_text"]
    assert cd["type"] == "text" and cd["font"] == "extra_large"
    assert cd["text"] == _format_countdown((e.start - NOW).total_seconds() / 60)
    assert cd["color"] == DIGIT_COLOR[STATE_NORMAL]
    assert cd["x"] == CD_TEXT_X == 38 and cd["y"] == 4
    assert "align" not in cd  # align was implicated in the v1.3 render mash; not used

def test_build_elements_state_palettes_by_urgency():
    notice_event = ev(10)   # within notice_minutes=15, outside warn_minutes=5
    els = build_elements(notice_event, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["bg"]["fill_colors"] == BG_GRADIENT[STATE_NOTICE]
    assert by_id["title"]["color"] == TITLE_COLOR[STATE_NOTICE]
    assert by_id["track_fill"]["fill_colors"] == TRACK_FILL_GRADIENT[STATE_NOTICE]
    assert by_id["divider"]["fill_colors"] == [DIVIDER_COLOR[STATE_NOTICE]]
    assert by_id["cd_card"]["fill_colors"] == [CD_CARD_COLOR[STATE_NOTICE]]
    assert by_id["cd_text"]["color"] == DIGIT_COLOR[STATE_NOTICE]

    warning_event = ev(3)   # within warn_minutes=5
    els = build_elements(warning_event, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["bg"]["fill_colors"] == BG_GRADIENT[STATE_WARNING]
    assert by_id["title"]["color"] == TITLE_COLOR[STATE_WARNING]
    assert by_id["track_fill"]["fill_colors"] == TRACK_FILL_GRADIENT[STATE_WARNING]
    assert by_id["divider"]["fill_colors"] == [DIVIDER_COLOR[STATE_WARNING]]
    assert by_id["cd_card"]["fill_colors"] == [CD_CARD_COLOR[STATE_WARNING]]
    assert by_id["cd_text"]["color"] == DIGIT_COLOR[STATE_WARNING]

def test_build_elements_track_fill_width_matches_drain_math():
    e = ev(30)   # half of the 60-min progress_window_minutes
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["track_fill"]["width"] == _track_fill_width(30, 60) == 36

def test_build_elements_track_fill_clamps_at_full_and_min():
    far_event = ev(120)   # well beyond the window -> full width
    els = build_elements(far_event, NOW, CFG, timeout_s=90, in_progress=False)
    assert {el["id"]: el for el in els}["track_fill"]["width"] == PANEL_WIDTH

    imminent_event = ev(0)   # starting now -> minimum sliver
    els = build_elements(imminent_event, NOW, CFG, timeout_s=90, in_progress=False)
    assert {el["id"]: el for el in els}["track_fill"]["width"] == 1

def test_build_elements_title_is_uppercased():
    e = ev(23, title="Gym pyjama day")
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    # Uppercasing after ascii_safe kills descenders (g, y, p, j, y) -- the
    # root cause of the v1.3 title/track collision (see logic.py TITLE_Y).
    assert title["text"] == "GYM PYJAMA DAY"

def test_build_elements_long_title_scrolls_short_title_static():
    long_e = ev(23, title="A Very Long Meeting Title That Will Definitely Not Fit On Screen Width")
    els = build_elements(long_e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    assert title.get("scroll_rate") == 2000
    assert title.get("scroll_start_delay") == 800
    assert title.get("scroll_repeat_delay") == 800

    short_e = ev(23, title="Sync")
    els = build_elements(short_e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    assert "scroll_rate" not in title


# --- build_elements: in-progress event ---------------------------------------

def test_build_elements_in_progress_shape():
    e = ev(-5, dur_min=30, title="Standup")
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=True)
    by_id = {el["id"]: el for el in els}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "ends", "divider", "cd_card", "cd_text"}
    assert "time_card" not in by_id and "time" not in by_id  # no start-time label while in progress
    # Draw order is z-order (first = behind); see the upcoming-shape test
    # for why set-membership alone isn't enough to catch a reorder.
    assert [el["id"] for el in els] == [
        "bg", "title", "track", "track_fill", "ends", "divider", "cd_card", "cd_text",
    ]

    bg = by_id["bg"]
    assert bg["fill_colors"] == BG_GRADIENT[STATE_IN_PROGRESS]

    title = by_id["title"]
    assert title["color"] == TITLE_COLOR[STATE_IN_PROGRESS]
    assert title["text"] == "STANDUP"

    track_fill = by_id["track_fill"]
    assert track_fill["fill"] == "solid"
    assert track_fill["fill_colors"] == [TRACK_FILL_IN_PROGRESS]
    assert track_fill["width"] == PANEL_WIDTH   # no drain while in progress

    ends = by_id["ends"]
    assert ends["type"] == "text" and ends["font"] == "bold"
    assert ends["text"] == ENDS_TEXT and ends["color"] == ENDS_TEXT_COLOR
    assert ends["x"] == 3 and ends["y"] == 6

    divider = by_id["divider"]
    assert divider["fill_colors"] == [DIVIDER_COLOR[STATE_IN_PROGRESS]]

    cd_card = by_id["cd_card"]
    assert cd_card["fill_colors"] == [CD_CARD_COLOR[STATE_IN_PROGRESS]]

    cd = by_id["cd_text"]
    assert cd["text"] == _format_countdown((e.end - NOW).total_seconds() / 60)  # counts down to END
    assert cd["color"] == DIGIT_COLOR[STATE_IN_PROGRESS]

def test_build_elements_in_progress_ignores_urgency_thresholds():
    # An in-progress event must render as in_progress even when minutes-left
    # (to its end) would fall inside the warn/notice bands by coincidence.
    e = ev(-58, dur_min=60, title="Long Meeting")  # 2 min left until end
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=True)
    by_id = {el["id"]: el for el in els}
    assert by_id["bg"]["fill_colors"] == BG_GRADIENT[STATE_IN_PROGRESS]
    assert by_id["cd_text"]["color"] == DIGIT_COLOR[STATE_IN_PROGRESS]
