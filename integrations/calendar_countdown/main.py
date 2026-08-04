import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:  # bare clone / broken editable install: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import load_config
from busybar.display import ambient_timeout

from .logic import (ascii_safe, build_elements, select_active_event,
                    select_next_event, _minutes_left, select_priority,
                    select_led, should_chirp, commit_chirped,
                    next_sleep_seconds, CHIRP_STOCK_PATH)

APP = "calendar_countdown"
HEARTBEAT_SECONDS = 600
log = logging.getLogger(APP)


def run_once(client, fetch, cfg: dict, now: datetime, dry_run: bool,
            state: dict | None = None) -> str:
    """Run one poll cycle. `state`, when passed, is a caller-owned dict this
    function uses to remember the previous draw's `in_progress` value
    across calls (main() passes one shared dict across loop iterations;
    tests calling run_once standalone can omit it), plus (v1.5.2) the
    next known event's start time (`next_start`, for the T-0 sleep-
    shortening in main()'s loop) and the chirp edge-detection bookkeeping
    (`seen_upcoming`/`chirped`, maintained by should_chirp/commit_chirped
    -- see calendar_countdown.logic for the full escalation-ladder and
    chirp design).

    The upcoming and in-progress layouts use different element id sets
    (`time` vs `ends`) and the device's draw endpoint upserts by id rather
    than replacing an app's whole element set -- confirmed on-device that
    switching id sets without an explicit clear leaves the previous set's
    elements rendered on top of the new ones until their own timeout
    expires (originally found with the v1.3 `time_card`+`time` vs `ends`
    id sets; the same upsert-by-id model applies regardless of which ids
    are in play). `state` lets us clear only at the transition, not on
    every poll. Priority changes (v1.5.2's escalation ladder) do NOT need
    this same clear-on-change treatment: they're the same app_name
    upserting the same element ids at a new priority number, not a shape
    change -- see busybar.display's PRIORITY_AMBIENT_URGENT docstring for
    why a strictly-higher same-app_name draw always succeeds regardless
    of priority.
    """
    c = cfg["calendar_countdown"]
    timeout_s = ambient_timeout(c["poll_seconds"])
    events = fetch(c["lookahead_hours"])
    active = select_active_event(events, now)

    if c["auto_busy"] and not dry_run and active is not None:
        remaining_ms = int((active.end - now).total_seconds() * 1000)
        busy = client.get_busy() or {}
        if busy.get("type") in (None, "NOT_STARTED"):
            client.set_busy_simple(remaining_ms)

    # An in-progress event takes display priority over a later upcoming one.
    if active is not None:
        event, in_progress = active, True
    else:
        event, in_progress = select_next_event(
            events, now, c["lookahead_hours"], c["include_all_day"]), False

    if event is None:
        if not dry_run:
            client.clear(APP)
        if state is not None:
            state["in_progress"] = None
            state["next_start"] = None
        return "no upcoming event; cleared"

    # Recorded regardless of dry_run: this is pure bookkeeping about what
    # the calendar says, not a device action, so main()'s sleep-shortening
    # calculation stays accurate even across dry-run polls.
    if state is not None:
        state["next_start"] = None if in_progress else event.start

    label = f"{'active' if in_progress else 'upcoming'} {ascii_safe(event.title)!r}"
    if dry_run:
        return f"DRY-RUN would draw: {label} (in_progress={in_progress})"

    # Event-start chirp (v1.5.2): fires on the upcoming -> in_progress
    # transition edge only -- see should_chirp's docstring for the full
    # restart-safety and once-per-event reasoning. Placed after the
    # dry_run return so a dry run never plays real audio or touches the
    # chirp bookkeeping.
    if state is not None:
        if should_chirp(event, in_progress, now, state, c["chirp"]):
            if client.play_audio(APP, stock_path=CHIRP_STOCK_PATH):
                commit_chirped(event, state)
            # else: play_audio already logged the failure; leaving
            # "chirped" uncommitted means the next poll (still
            # in_progress, same event) retries rather than silently
            # skipping the chirp forever.

    if state is not None and state.get("in_progress") not in (None, in_progress):
        # clear()'s own success/failure is intentionally not checked here --
        # only draw()'s result (below) gates whether `state` commits. If
        # clear() silently fails but draw() then succeeds, the new element
        # set is still correctly installed via the id-upsert; any leftover
        # stale ids from before the failed clear are bounded by their own
        # original timeout, a one-off gap that self-heals, not a reason to
        # re-clear on every subsequent poll. Gating on clear() too would mean
        # a persistently-failing clear() retries forever even once draw()
        # keeps succeeding, since `state` would never converge.
        client.clear(APP)

    elements = build_elements(event, now, c, timeout_s, in_progress)
    minutes_left = _minutes_left(event, now, in_progress)
    priority = select_priority(minutes_left, c["approach_minutes"], c["notice_minutes"], in_progress)
    led = select_led(minutes_left, c["imminent_minutes"], in_progress)
    result = client.draw(APP, elements=elements, priority=priority, led_notification_color=led)
    if state is not None and result == DrawResult.DRAWN:
        # Only commit the transition once it actually lands on the device.
        # If draw() failed (UNREACHABLE/REJECTED/ERROR), leave `state`
        # unchanged so the next poll still sees the same mismatch and
        # retries the clear+draw pair, rather than assuming a transition
        # happened that never actually reached the device -- which would
        # otherwise let stale elements from the old layout persist
        # unbounded (no further poll would ever re-attempt the clear).
        state["in_progress"] = in_progress
    return f"drew {label} -> {result.value}"


def should_log_info(summary: str, last_logged_summary: str | None,
                    seconds_since_heartbeat: float,
                    heartbeat_seconds: int = HEARTBEAT_SECONDS) -> bool:
    """Log-noise control for the v1.5 poll-cadence drop (poll_seconds
    60 -> 10 as the ambient-tier default): at 10s polling, logging every
    summary at INFO would sixfold the audit log's line rate versus the
    old 60s cadence for no new information on most polls (the summary is
    usually identical poll to poll). INFO only when the summary actually
    changed since the last INFO line, or a heartbeat interval has elapsed
    (so a long unchanging run still leaves a periodic "yes, I'm alive"
    trail) -- DEBUG otherwise.
    """
    return summary != last_logged_summary or seconds_since_heartbeat >= heartbeat_seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar calendar countdown")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-calendars", action="store_true",
                        help="print available calendar names and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from . import eventkit  # macOS-only import kept out of module scope for tests
    if not eventkit.ensure_access():
        log.error("Calendar access denied. Grant access in System Settings > "
                  "Privacy & Security > Calendars, then rerun.")
        return 1

    if args.list_calendars:
        for title, account in eventkit.list_calendars():
            print(f"{account}: {title}")
        print("Add the titles you want to [calendar_countdown] calendars in config.toml")
        return 0

    cfg = load_config()
    client = BusyBarClient(host=cfg["device"]["host"])
    # Drop any stale elements from a previous process. This also protects a
    # restart onto this version against every id change made across the
    # v1.3 -> v1.3.1 -> v1.4 line: v1.3.1 replaced the native "countdown"
    # element with a "cd_text" text element (a type change under upsert can
    # serve stale pixel data), and v1.4 removed "time_card"/"cd_card"
    # entirely. Neither removed id needs a drawn successor -- this startup
    # clear plus the transition-state clear above are sufficient, since a
    # deploy always restarts the process (fresh client.clear(APP) here) and
    # build_elements() simply never emits those ids again afterward.
    client.clear(APP)
    fetch = lambda hours: eventkit.fetch_events(hours, cfg["calendar_countdown"]["calendars"])

    backoff = 5
    state: dict = {}
    last_logged_summary: str | None = None
    last_heartbeat = time.monotonic()
    while True:
        summary = run_once(client, fetch, cfg, datetime.now(timezone.utc), args.dry_run, state=state)
        now_monotonic = time.monotonic()
        if args.once or should_log_info(summary, last_logged_summary, now_monotonic - last_heartbeat):
            log.info(summary)
            last_logged_summary = summary
            last_heartbeat = now_monotonic
        else:
            log.debug(summary)
        if args.once:
            return 0
        if summary.endswith(DrawResult.UNREACHABLE.value):
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            # v1.5.2 T-0 chirp precision: sleep exactly until the next
            # known event's start, not a full poll interval, when that's
            # sooner -- see next_sleep_seconds's docstring.
            next_start = state.get("next_start")
            seconds_until_start = ((next_start - datetime.now(timezone.utc)).total_seconds()
                                   if next_start is not None else None)
            time.sleep(next_sleep_seconds(cfg["calendar_countdown"]["poll_seconds"], seconds_until_start))


if __name__ == "__main__":
    raise SystemExit(main())
