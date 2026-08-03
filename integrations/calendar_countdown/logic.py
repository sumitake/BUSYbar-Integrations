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
