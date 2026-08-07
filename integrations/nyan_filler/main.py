import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime

from busybar.client import BusyBarClient, DrawResult
from busybar.config import device_kwargs, load_config
from busybar.display import PRIORITY_FILLER

from .logic import (FILLER_APP, ASSET_NAME, build_filler_elements,
                    in_quiet_hours, parse_quiet_hours)

APP = FILLER_APP
HEARTBEAT_SECONDS = 600
log = logging.getLogger(APP)

ASSET_PATH = Path(__file__).resolve().parents[2] / "assets" / "nyan" / ASSET_NAME


def run_once(client, cfg: dict, now: datetime, state: dict, dry_run: bool = False) -> str:
    """One poll cycle. `state` is a caller-owned dict mutated in place:
    `quiet_cleared` records whether we've already released the panel for the
    current quiet window (so we clear once on entry, not every poll)."""
    c = cfg["nyan_filler"]
    if not c["enabled"]:
        return "disabled; no-op"

    window = parse_quiet_hours(c["quiet_hours"])
    if in_quiet_hours(now, window):
        if not state.get("quiet_cleared"):
            if not dry_run:
                client.clear(APP)
            state["quiet_cleared"] = True
            return "quiet hours: released panel"
        return "quiet hours: silent"
    state["quiet_cleared"] = False

    timeout_s = max(2, int(c["poll_seconds"]) * 2)  # self-clears if the poller dies
    elements = build_filler_elements(ASSET_NAME, timeout_s)
    if dry_run:
        return f"DRY-RUN draw @ {PRIORITY_FILLER}: {elements!r}"
    result = client.draw(APP, elements, priority=PRIORITY_FILLER)
    if result == DrawResult.UNREACHABLE:
        return "device unreachable"
    return f"nyan @ {PRIORITY_FILLER} -> {result.value}"


def should_log_info(summary: str, last_logged_summary: str | None,
                    seconds_since_heartbeat: float,
                    heartbeat_seconds: int = HEARTBEAT_SECONDS) -> bool:
    """Log-noise control, mirroring calendar_countdown's should_log_info: at
    nyan_filler's default poll_seconds=1, logging every summary at INFO
    would produce ~86,400 near-identical lines/day to an un-rotated log for
    no new information on most polls (the summary is almost always
    identical poll to poll). INFO only when the summary actually changed
    since the last INFO line, or a heartbeat interval has elapsed (so a
    long unchanging run still leaves a periodic "yes, I'm alive" trail) --
    DEBUG otherwise.
    """
    return summary != last_logged_summary or seconds_since_heartbeat >= heartbeat_seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar Nyan dark-filler")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()

    # Clean-startup validation (mirrors ci_status's config_requires_repos):
    # a malformed quiet_hours would otherwise raise ValueError from inside
    # run_once() on every poll, and main()'s while True loop has no guard --
    # under launchd KeepAlive that's a silent crash-loop instead of one
    # clear, actionable failure at startup. Parsed once here and discarded;
    # run_once's own parse_quiet_hours call is unaffected.
    try:
        parse_quiet_hours(cfg["nyan_filler"]["quiet_hours"])
    except ValueError as exc:
        log.error("invalid [nyan_filler] quiet_hours config: %s", exc)
        return 1

    client = BusyBarClient(**device_kwargs(cfg))

    # Startup clear + self-healing asset (re)upload -- both real device
    # writes, so both are gated behind --dry-run (no device writes at all
    # in dry-run mode). Upload: one ~83 KB POST per process start, never
    # per poll.
    if not args.dry_run:
        client.clear(APP)  # drop any stale element from a previous process
        if ASSET_PATH.exists():
            client.upload_asset(APP, ASSET_NAME, ASSET_PATH.read_bytes())
        else:
            log.warning("asset %s missing; run `uv run python -m tools.build_nyan_anim`", ASSET_PATH)

    state: dict = {}
    backoff = 5
    last_logged_summary: str | None = None
    last_heartbeat = time.monotonic()
    while True:
        summary = run_once(client, cfg, datetime.now(), state, args.dry_run)
        now_monotonic = time.monotonic()
        if args.once or should_log_info(summary, last_logged_summary, now_monotonic - last_heartbeat):
            log.info(summary)
            last_logged_summary = summary
            last_heartbeat = now_monotonic
        else:
            log.debug(summary)
        if args.once:
            return 0
        if summary == "device unreachable":
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(cfg["nyan_filler"]["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
