import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:  # bare clone / broken editable install: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import load_config
from busybar.display import OVERLAY_DWELL_SECONDS, overlay_gap_elapsed

from .logic import (
    RepoState, RunningInfo, QuotaInfo,
    build_ci_payload, build_overlay_payload, evaluate_runs,
    overlay_frame_sequence, parse_rate_limit, select_running_run,
)

APP = "ci_status"
log = logging.getLogger(APP)

QUOTA_LABELS = {"graphql": "GITHUB GRAPHQL", "core": "GITHUB REST"}
QUOTA_STALE_SECONDS = 300  # never show rate_limit data older than 5 minutes


def _refresh_quota(poller, quota_cache: dict | None, now: datetime) -> dict[str, QuotaInfo] | None:
    """Fetch /rate_limit fresh (it's exempt from GitHub's own rate
    limiting, so there's no cost to calling it every poll) and update
    `quota_cache` on success. Returns the current bucket->QuotaInfo mapping
    if `quota_cache` holds data no older than QUOTA_STALE_SECONDS (whether
    from this fetch or an earlier one that succeeded when this one
    didn't), else None -- callers must treat None as "no quota frames this
    cycle," never fall back to stale numbers.
    """
    if quota_cache is None:
        return None
    raw = poller.fetch_rate_limit()
    if raw is not None:
        parsed = parse_rate_limit(raw)
        if parsed is not None:
            quota_cache["buckets"] = parsed
            quota_cache["fetched_at"] = now
    fetched_at = quota_cache.get("fetched_at")
    if fetched_at is None or (now - fetched_at).total_seconds() > QUOTA_STALE_SECONDS:
        return None
    buckets = quota_cache.get("buckets") or {}
    return {
        key: QuotaInfo(label=QUOTA_LABELS[key], limit=buckets[key]["limit"],
                      remaining=buckets[key]["remaining"], used=buckets[key]["used"],
                      reset_epoch=buckets[key]["reset"], now=now)
        for key in buckets if key in QUOTA_LABELS
    }


def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool,
             running_cache: dict[str, list[dict]] | None = None,
             overlay_state: dict | None = None,
             quota_cache: dict | None = None) -> str:
    """`running_cache`, `overlay_state`, and `quota_cache`, when passed, are
    caller-owned dicts this function mutates in place (mirroring
    `state_cache`'s existing pattern) so `main()` can hold one instance of
    each across loop iterations while `run_once` itself stays a pure
    function of its arguments plus those dicts. Omitting `running_cache`
    (the default) skips running-job/overlay detection entirely.

    Overlay rotation: while a run is active (and no failure/stuck alert
    preempts it), the overlay tier draws one frame per dwell slot, cycling
    through `overlay_frame_sequence(show_quota)` (the running badge, then
    -- if `show_quota` -- the GraphQL and REST quota frames). A dwell slot
    only fires once `overlay_gap_elapsed(last_dwell_end, now) >=
    OVERLAY_DWELL_SECONDS` (busybar.display's contract: stay silent at
    least one full dwell so the ambient calendar has a real chance to
    reclaim the screen in between -- see busybar/display.py and the spec
    doc's v1.5 section for why). `overlay_state`'s `frame_index` and
    `last_dwell_end` only commit once `client.draw` actually returns
    DRAWN, same discipline as calendar_countdown's transition-state fix: a
    failed draw must not be mistaken for a completed dwell, or the
    rotation would silently skip frames / wait a dwell for nothing.

    `overlay_state["last_shape"]` is a *unified* shape tracker, not
    overlay-specific despite living in this dict: it records the element-id
    set of whatever payload was last actually drawn to `APP`, across every
    tier that can draw here -- an alert badge, the quiet-green text, or
    either overlay frame kind -- and every draw path below checks it before
    drawing and commits to it after DRAWN. The firmware upserts by element
    id within an `application_name`, and each of these payload shapes has a
    different id set (`{bg, ci}` for an alert, `{ci}` alone for quiet
    green, `{bg, title, track, track_fill, eta}` for the running badge,
    `{bg, title, track, track_fill, pct, reset}` for a quota frame) --
    switching shapes without a clear() first leaves the previous shape's
    now-orphaned ids rendered until their own timeout elapses (up to 1.5x
    `poll_seconds` for an alert/green draw), the same upsert-by-id bug
    class the v1.3.1 calendar transition-clear fix addressed, recurring at
    every seam a different payload shape can follow another -- not just
    between the two overlay-frame shapes. Critically, resetting the
    rotation bookkeeping (`frame_index`/`last_dwell_end`, e.g. when an
    alert preempts the overlay or a run ends) must NOT also reset
    `last_shape`: that field describes what is physically on the device
    right now, which a bookkeeping reset does not change, and clearing it
    prematurely was the root cause of a real bug where the clear-gate saw
    "no shape on record" and wrongly concluded no clear was needed on the
    next transition.
    """
    c = cfg["ci_status"]
    timeout_s = int(c["poll_seconds"] * 1.5)
    for repo in c["repos"]:
        runs = poller.fetch_runs(repo)
        if runs is not None:  # None = 304/no-change/error -> keep cached state
            state_cache[repo] = evaluate_runs(repo, runs, now,
                                              c["stale_queued_minutes"])
    states = list(state_cache.values())
    has_alert = any(s.failing or s.stuck for s in states)

    overlay_payload = None
    frame_index = 0
    stay_silent = False
    frame_data_unavailable = False

    # running_cache check first: short-circuits before touching c["show_running"],
    # so callers/tests using an older, fully-spelled-out cfg dict that predates
    # this key (and never pass running_cache) don't KeyError.
    if running_cache is not None and c["show_running"]:
        for repo in c["repos"]:
            running_runs = poller.fetch_running_runs(repo)
            if running_runs is not None:  # None = 304/no-change/error -> keep cached
                running_cache[repo] = running_runs
        selected = select_running_run(running_cache)

        if selected is None or has_alert:
            # Nothing running, or an alert takes precedence this poll --
            # reset only the ROTATION bookkeeping, so the next run to start
            # always begins at the CI badge. Deliberately do NOT touch
            # last_shape here -- see the docstring above.
            if overlay_state is not None:
                overlay_state.pop("frame_index", None)
                overlay_state.pop("last_dwell_end", None)
        else:
            run, repo, other_count = selected
            median = poller.fetch_median_eta(repo, run["workflow_id"])
            running_info = RunningInfo(run=run, repo=repo, other_count=other_count,
                                       median_minutes=median, now=now)
            quota_by_bucket = _refresh_quota(poller, quota_cache, now) if c["show_quota"] else None

            sequence = overlay_frame_sequence(c["show_quota"])
            frame_index = (overlay_state.get("frame_index", 0) if overlay_state is not None else 0) % len(sequence)
            last_dwell_end = overlay_state.get("last_dwell_end") if overlay_state is not None else None

            if overlay_gap_elapsed(last_dwell_end, now) >= OVERLAY_DWELL_SECONDS:
                frame_name = sequence[frame_index]
                overlay_payload = build_overlay_payload(
                    frame_name, OVERLAY_DWELL_SECONDS,
                    running=running_info, quota_by_bucket=quota_by_bucket)
                if overlay_payload is None:
                    # This frame's data wasn't available this cycle (e.g. a
                    # quota frame with no fresh rate_limit data). Advance
                    # past it without consuming a dwell -- nothing was
                    # shown, so there's no gap to protect -- and skip this
                    # poll's draw entirely (no draw, no clear): whatever was
                    # already on screen is still within its own dwell
                    # timeout and is left exactly as it is.
                    if overlay_state is not None:
                        overlay_state["frame_index"] = frame_index + 1
                    frame_data_unavailable = True
            else:
                stay_silent = True

    if stay_silent:
        return "overlay dwell gap; staying silent (letting the ambient app reclaim the screen)"
    if frame_data_unavailable:
        return "overlay frame data unavailable this cycle; skipping (no draw, no clear)"

    payload = build_ci_payload(states, c["show_green"], timeout_s, overlay=overlay_payload)
    if dry_run:
        return f"DRY-RUN payload: {payload!r}"
    if payload is None:
        client.clear(APP)
        if overlay_state is not None:
            overlay_state["last_shape"] = None  # device is now genuinely blank
        return "all green; cleared"

    # Unified shape check (see docstring): applies to this draw regardless
    # of which tier produced it -- alert, quiet-green, or an overlay frame.
    shape = frozenset(e["id"] for e in payload["elements"])
    if overlay_state is not None:
        last_shape = overlay_state.get("last_shape")
        if last_shape is not None and last_shape != shape:
            # clear()'s own success/failure is intentionally not checked
            # here, same reasoning as calendar_countdown's transition-clear:
            # only draw()'s result below gates the state commit.
            client.clear(APP)

    result = client.draw(APP, payload["elements"], priority=payload["priority"],
                         led_notification_color=payload["led"])

    if result == DrawResult.DRAWN and overlay_state is not None:
        overlay_state["last_shape"] = shape
        if overlay_payload is not None:
            overlay_state["frame_index"] = frame_index + 1
            overlay_state["last_dwell_end"] = now + timedelta(seconds=OVERLAY_DWELL_SECONDS)

    text = next(e["text"] for e in payload["elements"] if e["type"] == "text")
    return f"{text[:40]!r} -> {result.value}"


def next_poll_seconds(cfg_ci: dict, running_cache: dict[str, list[dict]]) -> int:
    """Cadence switch: `running_poll_seconds` while any configured repo has
    a currently-running run, `poll_seconds` otherwise. A pure function of
    `running_cache`'s post-`run_once` state so it's testable without
    mocking `time.sleep`.
    """
    any_running = any(running_cache.get(repo) for repo in cfg_ci["repos"])
    return cfg_ci["running_poll_seconds"] if any_running else cfg_ci["poll_seconds"]


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar CI status")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    if not cfg["ci_status"]["repos"]:
        log.error("No repos configured. Copy config.example.toml to config.toml "
                  "and set [ci_status] repos.")
        return 1
    from .github import RestPoller, get_token
    try:
        poller = RestPoller(get_token())
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    client = BusyBarClient(host=cfg["device"]["host"])
    client.clear(APP)  # drop any stale elements from a previous process (type collisions 400)

    state_cache: dict[str, RepoState] = {}
    running_cache: dict[str, list[dict]] = {}
    overlay_state: dict = {}
    quota_cache: dict = {}
    backoff = 5
    while True:
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run, running_cache=running_cache,
                           overlay_state=overlay_state, quota_cache=quota_cache)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith("unreachable"):  # device offline: back off, not full poll
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(next_poll_seconds(cfg["ci_status"], running_cache))


if __name__ == "__main__":
    raise SystemExit(main())
