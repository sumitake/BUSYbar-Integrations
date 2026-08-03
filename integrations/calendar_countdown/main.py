import argparse
import logging
import time
from datetime import datetime, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import load_config

from .logic import (build_elements, format_countdown,
                    select_active_event, select_next_event)

APP = "calendar_countdown"
PRIORITY = 20
log = logging.getLogger(APP)


def run_once(client, fetch, cfg: dict, now: datetime, dry_run: bool) -> str:
    c = cfg["calendar_countdown"]
    timeout_s = int(c["poll_seconds"] * 1.5)
    events = fetch(c["lookahead_hours"])
    nxt = select_next_event(events, now, c["lookahead_hours"], c["include_all_day"])

    if c["auto_busy"] and not dry_run:
        active = select_active_event(events, now)
        if active is not None:
            remaining_ms = int((active.end - now).total_seconds() * 1000)
            busy = client.get_busy() or {}
            if busy.get("type") in (None, "NOT_STARTED"):
                client.set_busy_simple(remaining_ms)

    if nxt is None:
        if not dry_run:
            client.clear(APP)
        return "no upcoming event; cleared"

    warning = (nxt.start - now).total_seconds() / 60 <= c["warn_minutes"]
    text = format_countdown(nxt, now)
    elements = build_elements(text, warning, timeout_s)
    if dry_run:
        return f"DRY-RUN would draw: {text!r} (warning={warning})"
    result = client.draw(APP, elements=elements, priority=PRIORITY)
    return f"drew {text!r} -> {result.value}"


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar calendar countdown")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import eventkit  # macOS-only import kept out of module scope for tests
    if not eventkit.ensure_access():
        log.error("Calendar access denied. Grant access in System Settings > "
                  "Privacy & Security > Calendars, then rerun.")
        return 1

    cfg = load_config()
    client = BusyBarClient(host=cfg["device"]["host"])
    fetch = lambda hours: eventkit.fetch_events(hours, cfg["calendar_countdown"]["calendars"])

    backoff = 5
    while True:
        summary = run_once(client, fetch, cfg, datetime.now(timezone.utc), args.dry_run)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith(DrawResult.UNREACHABLE.value):
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(cfg["calendar_countdown"]["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
