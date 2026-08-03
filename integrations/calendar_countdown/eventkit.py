"""macOS EventKit adapter. Imported only on macOS, only from main()."""
import threading
from datetime import datetime, timedelta, timezone

from EventKit import (EKEventStore, EKEntityTypeEvent,
                      EKAuthorizationStatusFullAccess)
from Foundation import NSDate

from .logic import CalEvent

_store: EKEventStore | None = None


def ensure_access() -> bool:
    """Request calendar access; returns True when full access is granted."""
    global _store
    _store = EKEventStore.alloc().init()
    status = EKEventStore.authorizationStatusForEntityType_(EKEntityTypeEvent)
    if status == EKAuthorizationStatusFullAccess:
        return True
    done = threading.Event()
    result: list[bool] = [False]

    def _cb(granted, error):
        result[0] = bool(granted)
        done.set()

    _store.requestFullAccessToEventsWithCompletion_(_cb)
    done.wait(timeout=120)  # user is answering the macOS permission dialog
    return result[0]


def fetch_events(lookahead_hours: int, calendar_names: list[str]) -> list[CalEvent]:
    now = datetime.now(timezone.utc)
    calendars = _store.calendarsForEntityType_(EKEntityTypeEvent)
    if calendar_names:
        calendars = [c for c in calendars if c.title() in calendar_names]
    start_ns = NSDate.dateWithTimeIntervalSince1970_(now.timestamp())
    end_ns = NSDate.dateWithTimeIntervalSince1970_((now + timedelta(hours=lookahead_hours)).timestamp())
    predicate = _store.predicateForEventsWithStartDate_endDate_calendars_(
        start_ns, end_ns, calendars)
    events = _store.eventsMatchingPredicate_(predicate) or []
    out = []
    for e in events:
        out.append(CalEvent(
            title=str(e.title() or "event"),
            start=datetime.fromtimestamp(e.startDate().timeIntervalSince1970(), tz=timezone.utc),
            end=datetime.fromtimestamp(e.endDate().timeIntervalSince1970(), tz=timezone.utc),
            all_day=bool(e.isAllDay()),
        ))
    return out
