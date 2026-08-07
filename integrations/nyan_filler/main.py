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


def ensure_asset_uploaded(client, state: dict) -> None:
    """Idempotently make sure the .anim asset is on the device before we draw
    an element that references it. `state` is the same caller-owned dict
    run_once threads through the loop.

    The upload is conceptually a one-shot install step (~83 KB, not per-poll).
    But a single startup attempt that happens to land while the device is
    transiently unreachable used to be discarded silently (upload_asset returns
    False and nothing retried it), leaving every subsequent draw for the life
    of the process referencing an asset that was never uploaded. So we latch on
    success instead: attempt the upload on each active poll until it lands once
    (`state["asset_uploaded"]`), then never upload again -- no per-poll uploads
    in steady state. In the in-scope transient-unreachable case these retries
    are naturally spaced out by main()'s exponential UNREACHABLE backoff, and
    upload_asset logs its own failure reason on each attempt.

    A locally missing build artifact is a different failure -- polling can't
    fix it -- so it's warned once (naming the rebuild command) and skipped
    without retry bookkeeping; if the file later appears it uploads on the next
    poll."""
    if state.get("asset_uploaded"):
        return
    if not ASSET_PATH.exists():
        if not state.get("asset_missing_warned"):
            log.warning("asset %s missing; run `uv run python -m tools.build_nyan_anim`", ASSET_PATH)
            state["asset_missing_warned"] = True
        return
    if client.upload_asset(APP, ASSET_NAME, ASSET_PATH.read_bytes()):
        state["asset_uploaded"] = True


def run_once(client, cfg: dict, now: datetime, state: dict, dry_run: bool = False) -> str:
    """One poll cycle. `state` is a caller-owned dict mutated in place:
    `quiet_cleared` records whether we've already released the panel for the
    current quiet window (so we clear once on entry, not every poll);
    `asset_uploaded` latches once the .anim asset has landed on the device
    (see ensure_asset_uploaded)."""
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
    ensure_asset_uploaded(client, state)  # retries until it lands once; then a no-op
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

    # Startup clear -- a real device write, so gated behind --dry-run (no
    # device writes at all in dry-run mode). The asset (re)upload is no longer
    # done here: run_once owns it now (ensure_asset_uploaded), attempting the
    # ~83 KB POST on each active poll only until it lands once, so a device
    # that is transiently unreachable at process start self-heals within the
    # same process instead of never uploading for its lifetime.
    if not args.dry_run:
        client.clear(APP)  # drop any stale element from a previous process

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
