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

# Fixed regardless of state -- only drawn for upcoming events. No card
# behind it as of v1.4 ("airy" refinement): per operator design principle,
# a card/panel surface is only acceptable with strong luminance contrast
# against both the background and its own text (near-black surface + bright
# text, or an inverse chip -- bright surface + near-black text, as the CI
# failure badge does). The v1.3.1 cards (#062238/#062A22, etc.) were
# mid-luminance dim fills that read as glow-mud on emissive LEDs, so v1.4
# removes cards entirely rather than trying to re-tune their luminance.
TIME_TEXT_COLOR = "#8DDEFFFF"

# Fixed -- only drawn for the in-progress state (replaces time + ends).
ENDS_TEXT_COLOR = "#8CFFF4FF"
ENDS_TEXT = "ENDS"

DIVIDER_COLOR = {
    STATE_NORMAL: "#643B8FFF",
    STATE_NOTICE: "#C37A0CFF",
    STATE_WARNING: "#E02A4CFF",
    STATE_IN_PROGRESS: "#178C88FF",
}

DIGIT_COLOR = {
    STATE_NORMAL: "#6BFFD0FF",
    STATE_NOTICE: "#FFC247FF",
    STATE_WARNING: "#FF4B68FF",
    STATE_IN_PROGRESS: "#64FFEAFF",
}

# --- geometry -----------------------------------------------------------------
#
# Firmware ink-offset gotcha (found via on-device frame captures after the
# first v1.3 pass mashed the layout): every font renders its ink ~2px below
# the element's `y`, uniformly across every font tested so far (tiny/small/
# large/extra_large/bold). The `_Y` constants below are already
# offset-corrected so ink lands where the name/row-comment implies -- do not
# "fix" them back to the visually-obvious value without re-verifying ink
# rows on-device. Rectangles have no such offset; a rectangle's ink starts
# exactly at its `y`.
#
# v1.4 "airy" refinement: more breathing room, no card surfaces (removed
# per the operator's luminance-contrast design principle -- see the
# TIME_TEXT_COLOR comment above), both numerals drop from extra_large (10px
# bold) to `large` (9px) so they stay equal-sized (never independently
# resized -- another operator design principle). Freeing the card rows
# opened up row 5 as a deliberate blank buffer row between the title (ink
# 0-4) and the track, which moved from y=5 to y=6.

TITLE_X = 2
TITLE_Y = -2   # ink rows 0-4 (small font, 5px); top-flush is deliberate (ribbon)
TITLE_WIDTH = 68   # 2px margin each side (x=2..70 of a 72px panel)

# Row 5 is a deliberate blank buffer -- nothing may ink it. Verified
# on-device (see the spec doc's v1.4 section and the report).
TRACK_Y = 6   # was 5 in v1.3.1; full width edge-to-edge is deliberate (horizon line)
TRACK_HEIGHT = 1

TIME_X = 2
TIME_Y = 5     # ink rows 7-15 (large font, 9px); bottom-flush at row 15 is deliberate

ENDS_X = 2
ENDS_Y = 6     # ink rows 8-14 (bold font, 7px); numerically shares y with track,
               # but text's +2 ink offset means ink starts at row 8, 1px clear of
               # track's ink at row 6 -- verify no contact on-device, not just by
               # the numbers matching.

DIVIDER_X = 34
DIVIDER_Y = 8   # ink rows 8-14 (rectangle, no offset); 1px clear of the track
DIVIDER_WIDTH = 2
DIVIDER_HEIGHT = 7   # ... above (row 7 blank) and the panel's bottom edge below (row 15 blank)

# The native `countdown` element's digits render only 5px tall in every mode
# (MM:SS and H:MM:SS both) -- confirmed with fresh-id on-device probes during
# the v1.3.1 correction. Large numerals are the operator's core requirement,
# so the native countdown element is unusable here; a plain `text` element
# replaces it (id "cd_text" -- not "countdown", since its element `type`
# differs and the draw endpoint's id-reuse-type-change quirk can serve stale
# pixel data across a type change), formatted by `_format_countdown` below.
# No `align` field: align re-anchors screen-relative, not layout-relative,
# and was implicated in the v1.3 mash. Fixed left position instead.
CD_TEXT_X = 39
CD_TEXT_Y = 5   # ink rows 7-15 (large font, 9px) -- same offset pattern as `time`,
                # since both are the same font size per the "numerals track together" rule

# Available width for cd_text's rendered string, right up to the panel's
# edge (there's nothing to its right in v1.4 -- no card, no divider on that
# side).
CD_TEXT_MAX_WIDTH = PANEL_WIDTH - CD_TEXT_X   # 33px

# Per-glyph advance width in px, `large` font (9px), measured on-device by
# differencing (e.g. width("001") - width("00") = the '1' glyph's advance).
# This font is NOT fixed-width: '1' measures 5px vs 7px for every other
# digit -- a ~30% difference that a single flat "px per char" constant
# cannot represent accurately enough to safely decide whether a given
# countdown string fits (see _format_countdown's docstring for why this
# matters here specifically). Values are rounded up from the raw
# differencing measurements (which came out ~1px lower than an additive
# reconstruction predicts, consistently, likely a one-time string-start
# left-bearing the differencing technique double-counts) -- i.e. this table
# deliberately over-estimates width slightly, the safe direction for a
# fits/doesn't-fit decision.
GLYPH_ADVANCE_PX = {
    "0": 7, "1": 5, "2": 7, "3": 7, "4": 7, "5": 7, "6": 7, "7": 7, "8": 7, "9": 7,
    "h": 6, "m": 8,
    # v1.5.1: extended for ci_status's running-badge ETA label feature --
    # "~" (the remaining-estimate prefix, e.g. "~4m") plus every letter
    # needed to also measure "remain"/"left" through this same table (see
    # ci_status/logic.py's _eta_label docstring for why the label -- which
    # actually renders in the *small* font -- is deliberately measured via
    # this *large*-font table anyway: on-device calibration found it's a
    # sizeable but safe overestimate of the label's real small-font width).
    # Same on-device successive-prefix differencing methodology as the
    # digits/h/m above; self-validated by re-measuring "0" via this
    # technique and getting the same 7px already in this table.
    "~": 7, "r": 5, "a": 6, "e": 6, "i": 4, "n": 6, "l": 4, "f": 5, "t": 5,
}


def _text_width_px(s: str) -> int:
    return sum(GLYPH_ADVANCE_PX[ch] for ch in s)


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


def _format_countdown(minutes_left: float) -> str:
    """Minutes-granular countdown text for the `cd_text` element.

    Floored (not rounded) to whole minutes, so the display always reads "at
    least this much time remains" rather than rounding up past the actual
    remaining time -- matches how a countdown is conventionally read ("1m"
    means just under 2 minutes left, not "close to 1 minute"). Negative
    input (defensive only; callers shouldn't produce it) clamps to 0.

    Under 60 minutes: "<M>m" (e.g. "54m"). At/above 1 hour: tries the full
    "<H>h<MM>m" form (e.g. "1h05m", minutes zero-padded to 2 digits) and
    falls back to "<H>h" (minutes dropped) only if the full form's *actual*
    rendered width (via GLYPH_ADVANCE_PX) would exceed CD_TEXT_MAX_WIDTH.

    This is a per-string width check, not a fixed hour cutoff (e.g. "only
    hours >= 10 drop minutes"), because on-device measurement (v1.4
    re-verification) found a fixed cutoff is unsafe: this font's '1' glyph
    is ~30% narrower than every other digit (5px vs 7px advance), so
    whether a given single-digit-hour string fits depends on which actual
    digits appear, not just the hour count. Measured examples: "1h05m" and
    "1h59m" fit (the hours digit's own "1" is enough headroom); "9h11m"
    fits too (two "1"s in the minutes make up for a non-"1" hours digit);
    but "2h05m", "5h55m", "9h59m", and even "0h00m" do NOT fit (34px
    measured against a 33px budget) despite all being single-digit-hour,
    5-glyph strings -- the same nominal "shape" the original fixed-cutoff
    design assumed was uniformly safe. Two-digit hours (10+) always fail
    the width check on their own digits alone and correctly fall through to
    the hour-only form as a natural consequence, with no special case
    needed.
    """
    total_minutes = max(0, int(minutes_left))
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    full_form = f"{hours}h{minutes:02d}m"
    if _text_width_px(full_form) <= CD_TEXT_MAX_WIDTH:
        return full_form
    return f"{hours}h"


def build_elements(event: CalEvent, now: datetime, cfg: dict, timeout_s: int,
                   in_progress: bool) -> list[dict]:
    """Build the v1.4 "airy" Color Horizon layout.

    `cfg` is the `[calendar_countdown]` config sub-dict (needs
    progress_window_minutes, notice_minutes, warn_minutes). `in_progress`
    selects between the "upcoming" layout (countdown to event.start, a
    large start-time numeral) and the "in-progress" layout (countdown to
    event.end, an "ENDS" label, full-width non-draining track fill). No
    card/panel surfaces -- `time` and `cd_text` float directly on `bg` (see
    the TIME_TEXT_COLOR comment above for why cards were removed in v1.4).
    The countdown itself (`cd_text`) is a plain `text` element re-rendered
    from `minutes_left` each poll -- not the native `countdown` element (see
    the CD_TEXT_X comment above for why). Draw order below is z-order,
    first = behind.
    """
    # Uppercase kills descenders (g, y, p, ...), which is what let the title
    # collide with the track below it before the ink-offset fix -- see the
    # geometry comment above TITLE_Y.
    title = ascii_safe(event.title).upper()

    if in_progress:
        minutes_left = (event.end - now).total_seconds() / 60
    else:
        minutes_left = (event.start - now).total_seconds() / 60

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
            "id": "time",
            "type": "text",
            "text": f"{event.start.astimezone():%H:%M}",
            "font": "large",
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
        "id": "cd_text",
        "type": "text",
        "text": _format_countdown(minutes_left),
        "font": "large",
        "color": DIGIT_COLOR[state],
        "x": CD_TEXT_X,
        "y": CD_TEXT_Y,
        "timeout": timeout_s,
    })

    return elements
