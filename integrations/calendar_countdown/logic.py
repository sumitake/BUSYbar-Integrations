from dataclasses import dataclass
from datetime import datetime, timedelta

PANEL_WIDTH = 72
PANEL_HEIGHT = 16

# --- v1.3 "Color Horizon" state palette --------------------------------------
#
# Four states, ordered least -> most urgent, with `in_progress` orthogonal to
# the other three (an event currently happening, regardless of how it got
# there). Every per-state table below is keyed by the same four strings so a
# lookup miss (typo, new state forgotten in one table) raises KeyError loudly
# instead of silently drawing the wrong color.
STATE_NORMAL = "normal"
STATE_NOTICE = "notice"
STATE_WARNING = "warning"
STATE_IN_PROGRESS = "in_progress"
STATES = (STATE_NORMAL, STATE_NOTICE, STATE_WARNING, STATE_IN_PROGRESS)

BG_GRADIENT = {
    STATE_NORMAL: ["#160A2EFF", "#03040DFF"],
    STATE_NOTICE: ["#291300FF", "#070301FF"],
    STATE_WARNING: ["#30040BFF", "#080103FF"],
    STATE_IN_PROGRESS: ["#032B2CFF", "#010809FF"],
}

TITLE_COLOR = {
    STATE_NORMAL: "#FFD166FF",
    STATE_NOTICE: "#FFE3A3FF",
    STATE_WARNING: "#FFE7ECFF",
    STATE_IN_PROGRESS: "#83FFF3FF",
}

# The drain track's groove is a single fixed color in every state; only the
# fill on top of it (TRACK_FILL_GRADIENT / TRACK_FILL_IN_PROGRESS) changes.
TRACK_COLOR = "#24193BFF"

TRACK_FILL_GRADIENT = {
    STATE_NORMAL: ["#1ED6FFFF", "#5CFFB1FF"],
    STATE_NOTICE: ["#FF9F1CFF", "#FFE66DFF"],
    STATE_WARNING: ["#FF204EFF", "#FF7A22FF"],
}
TRACK_FILL_IN_PROGRESS = "#24D6C5FF"

# Fixed regardless of state -- only drawn for upcoming events.
TIME_CARD_COLOR = "#062238FF"
TIME_TEXT_COLOR = "#8DDEFFFF"

# Fixed -- only drawn for the in-progress state (replaces time_card + time).
ENDS_TEXT_COLOR = "#8CFFF4FF"
ENDS_TEXT = "ENDS"

DIVIDER_COLOR = {
    STATE_NORMAL: "#643B8FFF",
    STATE_NOTICE: "#C37A0CFF",
    STATE_WARNING: "#E02A4CFF",
    STATE_IN_PROGRESS: "#178C88FF",
}

CD_CARD_COLOR = {
    STATE_NORMAL: "#062A22FF",
    STATE_NOTICE: "#3A2000FF",
    STATE_WARNING: "#3A0711FF",
    STATE_IN_PROGRESS: "#063238FF",
}

DIGIT_COLOR = {
    STATE_NORMAL: "#6BFFD0FF",
    STATE_NOTICE: "#FFC247FF",
    STATE_WARNING: "#FF4B68FF",
    STATE_IN_PROGRESS: "#64FFEAFF",
}

# --- geometry -----------------------------------------------------------------

TITLE_X = 1
TITLE_Y = 0
TITLE_WIDTH = 70

TRACK_Y = 5
TRACK_HEIGHT = 1

TIME_CARD_X = 0
TIME_CARD_Y = 6
TIME_CARD_WIDTH = 34
TIME_CARD_HEIGHT = 10
TIME_CARD_RADIUS = 1
TIME_X = 1
TIME_Y = 6

ENDS_X = 3
ENDS_Y = 8

DIVIDER_X = 34
DIVIDER_Y = 6
DIVIDER_WIDTH = 2
DIVIDER_HEIGHT = 10

CD_CARD_X = 36
CD_CARD_Y = 6
CD_CARD_WIDTH = 36
CD_CARD_HEIGHT = 10
CD_CARD_RADIUS = 1

COUNTDOWN_Y = 6
# Right-anchor the countdown within cd_card using the firmware's `align`
# field, 1px in from the card's right edge (x=72). Verified reliable
# on-device (firmware 1.1.1): drawing both a long (H:MM:SS, 7 chars) and a
# short (M:SS, 4 chars) countdown with align="top_right" kept the right edge
# pinned at the same column while the left edge moved -- true right-anchoring,
# not a fixed left position. If a future firmware makes this unreliable, the
# approved fallback is a fixed x=40 (H:MM:SS = 31px fits x40-71).
COUNTDOWN_ALIGN_X = 71
COUNTDOWN_FALLBACK_X = 40

# Approximate px-per-character for the "small" bitmap font, used only to
# decide whether a title needs to scroll. Deliberately conservative (slightly
# wide) so borderline titles scroll rather than clip; carried over from v1.1
# where it was refined against on-device renders of the same font.
SMALL_FONT_CHAR_PX = 5
SCROLL_RATE = 2000
SCROLL_DELAY_MS = 800


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


def _track_fill_width(minutes_left: float, window_minutes: int) -> int:
    """Drain-track fill width in px, anchored to the left (the filled portion
    shrinks from the right as the event approaches -- same "drains as it
    approaches" metaphor as the v1.1 vertical progress bar, ported to a
    horizontal track). >= window -> full width. <= 0 -> minimum visible
    sliver (1px). A non-positive window (misconfiguration) defensively reads
    as "always full" rather than raising or dividing by zero.
    """
    if window_minutes <= 0 or minutes_left >= window_minutes:
        return PANEL_WIDTH
    if minutes_left <= 0:
        return 1
    width = round(PANEL_WIDTH * minutes_left / window_minutes)
    return max(1, min(PANEL_WIDTH, width))


def _state_for(minutes_left: float, notice_minutes: int, warn_minutes: int,
               in_progress: bool) -> str:
    if in_progress:
        return STATE_IN_PROGRESS
    if minutes_left <= warn_minutes:
        return STATE_WARNING
    if minutes_left <= notice_minutes:
        return STATE_NOTICE
    return STATE_NORMAL


def _title_fits(title: str, width_px: int) -> bool:
    return len(title) * SMALL_FONT_CHAR_PX <= width_px


def build_elements(event: CalEvent, now: datetime, cfg: dict, timeout_s: int,
                   in_progress: bool) -> list[dict]:
    """Build the v1.3 "Color Horizon" layout.

    `cfg` is the `[calendar_countdown]` config sub-dict (needs
    progress_window_minutes, notice_minutes, warn_minutes). `in_progress`
    selects between the "upcoming" layout (countdown to event.start, a
    time-card label) and the "in-progress" layout (countdown to event.end, an
    "ENDS" label, full-width non-draining track fill). Draw order below is
    z-order, first = behind.
    """
    title = ascii_safe(event.title)

    if in_progress:
        minutes_left = (event.end - now).total_seconds() / 60
        target = event.end
    else:
        minutes_left = (event.start - now).total_seconds() / 60
        target = event.start

    state = _state_for(minutes_left, cfg["notice_minutes"], cfg["warn_minutes"], in_progress)

    bg_element = {
        "id": "bg",
        "type": "rectangle",
        "x": 0,
        "y": 0,
        "width": PANEL_WIDTH,
        "height": PANEL_HEIGHT,
        "fill": "gradient_v",
        "fill_colors": BG_GRADIENT[state],
        "border_width": 0,
        "timeout": timeout_s,
    }

    title_element = {
        "id": "title",
        "type": "text",
        "text": title,
        "font": "small",
        "color": TITLE_COLOR[state],
        "x": TITLE_X,
        "y": TITLE_Y,
        "width": TITLE_WIDTH,
        "timeout": timeout_s,
    }
    if not _title_fits(title, TITLE_WIDTH):
        title_element.update({
            "scroll_rate": SCROLL_RATE,
            "scroll_start_delay": SCROLL_DELAY_MS,
            "scroll_repeat_delay": SCROLL_DELAY_MS,
        })

    track_element = {
        "id": "track",
        "type": "rectangle",
        "x": 0,
        "y": TRACK_Y,
        "width": PANEL_WIDTH,
        "height": TRACK_HEIGHT,
        "fill": "solid",
        "fill_colors": [TRACK_COLOR],
        # RectangleElement defaults to a 1px white border, which at this
        # height would swallow the fill entirely (found on-device in v1.1).
        "border_width": 0,
        "timeout": timeout_s,
    }

    if in_progress:
        track_fill_width = PANEL_WIDTH
        track_fill = {"fill": "solid", "fill_colors": [TRACK_FILL_IN_PROGRESS]}
    else:
        track_fill_width = _track_fill_width(minutes_left, cfg["progress_window_minutes"])
        track_fill = {"fill": "gradient_h", "fill_colors": TRACK_FILL_GRADIENT[state]}
    track_fill_element = {
        "id": "track_fill",
        "type": "rectangle",
        "x": 0,
        "y": TRACK_Y,
        "width": track_fill_width,
        "height": TRACK_HEIGHT,
        "border_width": 0,
        "timeout": timeout_s,
        **track_fill,
    }

    elements = [bg_element, title_element, track_element, track_fill_element]

    if in_progress:
        elements.append({
            "id": "ends",
            "type": "text",
            "text": ENDS_TEXT,
            "font": "bold",
            "color": ENDS_TEXT_COLOR,
            "x": ENDS_X,
            "y": ENDS_Y,
            "timeout": timeout_s,
        })
    else:
        elements.append({
            "id": "time_card",
            "type": "rectangle",
            "x": TIME_CARD_X,
            "y": TIME_CARD_Y,
            "width": TIME_CARD_WIDTH,
            "height": TIME_CARD_HEIGHT,
            "radius": TIME_CARD_RADIUS,
            "fill": "solid",
            "fill_colors": [TIME_CARD_COLOR],
            "border_width": 0,
            "timeout": timeout_s,
        })
        elements.append({
            "id": "time",
            "type": "text",
            "text": f"{event.start.astimezone():%H:%M}",
            "font": "extra_large",
            "color": TIME_TEXT_COLOR,
            "x": TIME_X,
            "y": TIME_Y,
            "timeout": timeout_s,
        })

    elements.append({
        "id": "divider",
        "type": "rectangle",
        "x": DIVIDER_X,
        "y": DIVIDER_Y,
        "width": DIVIDER_WIDTH,
        "height": DIVIDER_HEIGHT,
        "fill": "solid",
        "fill_colors": [DIVIDER_COLOR[state]],
        "border_width": 0,
        "timeout": timeout_s,
    })

    elements.append({
        "id": "cd_card",
        "type": "rectangle",
        "x": CD_CARD_X,
        "y": CD_CARD_Y,
        "width": CD_CARD_WIDTH,
        "height": CD_CARD_HEIGHT,
        "radius": CD_CARD_RADIUS,
        "fill": "solid",
        "fill_colors": [CD_CARD_COLOR[state]],
        "border_width": 0,
        "timeout": timeout_s,
    })

    elements.append({
        "id": "countdown",
        "type": "countdown",
        "timestamp": str(int(target.timestamp())),
        "direction": "time_left",
        "show_hours": "when_non_zero",
        "color": DIGIT_COLOR[state],
        "align": "top_right",
        "x": COUNTDOWN_ALIGN_X,
        "y": COUNTDOWN_Y,
        "timeout": timeout_s,
    })

    return elements
