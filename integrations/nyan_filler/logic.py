"""Pure helpers for the nyan_filler integration: quiet-hours parsing/gating and
the animation element payload. No I/O -- fully unit-tested."""
from __future__ import annotations

import re
from datetime import datetime

FILLER_APP = "nyan_filler"
ASSET_NAME = "nyan_72x16.anim"
ELEMENT_ID = "nyan"
PROBE_ELEMENT_ID = "probe"
PROBE_COLOR = "#040614FF"   # the animation's own backdrop, so the pixel is not seen

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")


def parse_quiet_hours(s: str) -> tuple[int, int] | None:
    """'HH:MM-HH:MM' -> (start_min, end_min) minutes-since-midnight. '' -> None
    (quiet hours disabled). Raises ValueError on any other malformed input."""
    if s == "":
        return None
    m = _HHMM.match(s.strip())
    if not m:
        raise ValueError(f"invalid quiet_hours {s!r}; expected 'HH:MM-HH:MM' or ''")
    sh, sm, eh, em = (int(g) for g in m.groups())
    return sh * 60 + sm, eh * 60 + em


def in_quiet_hours(now: datetime, window: tuple[int, int] | None) -> bool:
    """True iff `now`'s local wall-clock falls in the window. Inclusive start,
    exclusive end. Supports a window that wraps midnight (start > end). A window
    with start == end is treated as 'never quiet'."""
    if window is None:
        return False
    start, end = window
    if start == end:
        return False
    cur = now.hour * 60 + now.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end   # wraps midnight


def build_filler_elements(asset: str, timeout_s: int) -> list[dict]:
    """The single looping animation element drawn at PRIORITY_FILLER."""
    return [{"id": ELEMENT_ID, "type": "animation", "path": asset,
             "x": 0, "y": 0, "loop": True, "timeout": timeout_s}]


def build_probe_elements() -> list[dict]:
    """One backdrop-coloured pixel used to ask whether the panel is free.

    While a higher-priority app owns the panel the bar takes about 1.5s to
    reject an animation draw but only milliseconds to reject a rectangle
    (measured on firmware 1.4.1), so the question is asked with this instead.
    """
    return [{"id": PROBE_ELEMENT_ID, "type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1,
             "fill": "solid", "fill_colors": [PROBE_COLOR], "border_width": 0, "timeout": 1}]
