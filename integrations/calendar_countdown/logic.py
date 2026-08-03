from dataclasses import dataclass
from datetime import datetime, timedelta

# Urgency / state colors (#RRGGBBAA).
COLOR_WHITE = "#FFFFFFFF"
COLOR_GRAY = "#B4B2A9FF"
COLOR_AMBER = "#EF9F27FF"
COLOR_RED = "#E24B4AFF"
COLOR_TEAL = "#1D9E75FF"

PANEL_WIDTH = 72
PANEL_HEIGHT = 16
BAR_WIDTH = 2

# Title x-position: right of the "HH:MM" tiny-font time label (upcoming events),
# or flush left when no time label is drawn (in-progress events). Verified
# on-device against the panel's tiny/small bitmap fonts.
TITLE_X_WITH_TIME = 26
TITLE_X_NO_TIME = 4

# Bottom-left countdown row. The countdown element uses a firmware-fixed font
# (no `font` field on CountdownElement) — this y was chosen to sit clear of the
# title row and verified on-device.
COUNTDOWN_Y = 9

# Approximate px-per-character for the "small" bitmap font, used only to decide
# whether a title needs to scroll. Deliberately conservative (slightly wide) so
# borderline titles scroll rather than clip; refined against on-device renders.
SMALL_FONT_CHAR_PX = 5


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


def _bar_height(minutes_left: float, window_minutes: int) -> int:
    """Progress-accent-bar height in px, anchored to the bottom (drains downward).

    >= window -> full height. <= 0 -> minimum visible sliver (1px). A
    non-positive window (misconfiguration) defensively reads as "always full"
    rather than raising or dividing by zero.
    """
    if window_minutes <= 0 or minutes_left >= window_minutes:
        return PANEL_HEIGHT
    if minutes_left <= 0:
        return 1
    height = round(PANEL_HEIGHT * minutes_left / window_minutes)
    return max(1, min(PANEL_HEIGHT, height))


def _urgency_color(minutes_left: float, notice_minutes: int, warn_minutes: int) -> str:
    if minutes_left <= warn_minutes:
        return COLOR_RED
    if minutes_left <= notice_minutes:
        return COLOR_AMBER
    return COLOR_WHITE


def _title_fits(title: str, width_px: int) -> bool:
    return len(title) * SMALL_FONT_CHAR_PX <= width_px


def build_elements(event: CalEvent, now: datetime, cfg: dict, timeout_s: int,
                   in_progress: bool) -> list[dict]:
    """Build the v1.1 layout: progress accent bar + title row + native countdown.

    `cfg` is the `[calendar_countdown]` config sub-dict (needs
    progress_window_minutes, notice_minutes, warn_minutes). `in_progress`
    selects between the "upcoming" layout (countdown to event.start, time
    label shown) and the "in-progress" layout (countdown to event.end, full
    teal bar, no time label).
    """
    title = ascii_safe(event.title)
    time_element = None

    if in_progress:
        color = COLOR_TEAL
        bar_height = PANEL_HEIGHT
        target = event.end
        title_x = TITLE_X_NO_TIME
    else:
        minutes_left = (event.start - now).total_seconds() / 60
        color = _urgency_color(minutes_left, cfg["notice_minutes"], cfg["warn_minutes"])
        bar_height = _bar_height(minutes_left, cfg["progress_window_minutes"])
        target = event.start
        title_x = TITLE_X_WITH_TIME
        time_element = {
            "id": "time",
            "type": "text",
            "text": f"{event.start.astimezone():%H:%M}",
            "font": "tiny",
            "color": COLOR_GRAY,
            "x": 4,
            "y": 0,
            "timeout": timeout_s,
        }

    bar_element = {
        "id": "bar",
        "type": "rectangle",
        "x": 0,
        "y": PANEL_HEIGHT - bar_height,
        "width": BAR_WIDTH,
        "height": bar_height,
        "fill": "solid",
        "fill_colors": [color],
        # RectangleElement defaults to a 1px white border; at this width that
        # border alone fills the whole bar, hiding the fill color entirely
        # (found via on-device verification). Must be disabled explicitly.
        "border_width": 0,
        "timeout": timeout_s,
    }

    title_width = max(1, PANEL_WIDTH - title_x)
    title_element = {
        "id": "title",
        "type": "text",
        "text": title,
        "font": "small",
        "color": COLOR_WHITE,
        "x": title_x,
        "y": 0,
        "width": title_width,
        "timeout": timeout_s,
    }
    if not _title_fits(title, title_width):
        title_element.update({
            "scroll_rate": 2000,
            "scroll_start_delay": 1000,
            "scroll_repeat_delay": 2000,
        })

    countdown_element = {
        "id": "countdown",
        "type": "countdown",
        "timestamp": str(int(target.timestamp())),
        "direction": "time_left",
        "show_hours": "when_non_zero",
        "color": color,
        "x": 4,
        "y": COUNTDOWN_Y,
        "timeout": timeout_s,
    }

    elements = [bar_element]
    if time_element is not None:
        elements.append(time_element)
    elements.append(title_element)
    elements.append(countdown_element)
    return elements
