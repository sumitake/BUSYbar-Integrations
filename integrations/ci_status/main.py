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
from busybar.config import device_kwargs, load_config
from busybar.display import OVERLAY_DWELL_SECONDS, overlay_gap_elapsed

from .logic import (
    RepoState, RunningInfo, QuotaInfo,
    build_ci_payload, build_overlay_payload, compute_alert_fingerprint, evaluate_runs,
    overlay_frame_sequence, parse_rate_limit, resolve_repo_list, select_running_run,
    update_snooze,
)

APP = "ci_status"
log = logging.getLogger(APP)

QUOTA_LABELS = {"graphql": "GITHUB GRAPHQL", "core": "GITHUB REST"}
QUOTA_STALE_SECONDS = 300  # never show rate_limit data older than 5 minutes


def _refresh_account_repos(poller, repo_cache: dict | None, now: datetime,
                           repo_refresh_minutes: int) -> list[dict] | None:
    """Re-enumerate the account's owned repos (v1.5.1) when `repo_cache`
    is stale or has never been populated, keeping the previous list on an
    enumeration failure -- never crash, never silently fall back to an
    empty watch list (an empty result from `fetch_account_repos` is
    indistinguishable from "genuinely zero repos," so `None` is the only
    signal treated as "keep what we had"; see that method's docstring).
    Mirrors `_refresh_quota`'s cache-freshness pattern.
    """
    if repo_cache is None:
        return None
    fetched_at = repo_cache.get("fetched_at")
    stale = fetched_at is None or (now - fetched_at).total_seconds() > repo_refresh_minutes * 60
    if stale:
        fetched = poller.fetch_account_repos()
        if fetched is not None:
            repo_cache["repos"] = fetched
            repo_cache["fetched_at"] = now
        elif repo_cache.get("repos") is None:
            log.warning("account repo enumeration failed and no previous list is "
                       "cached yet -- watch_account_repos contributes nothing this poll")
    return repo_cache.get("repos")


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
             quota_cache: dict | None = None,
             repo_cache: dict | None = None,
             snooze_state: dict | None = None) -> str:
    """`running_cache`, `overlay_state`, `quota_cache`, and `repo_cache`,
    when passed, are caller-owned dicts this function mutates in place
    (mirroring `state_cache`'s existing pattern) so `main()` can hold one
    instance of each across loop iterations while `run_once` itself stays
    a pure function of its arguments plus those dicts. Omitting
    `running_cache` (the default) skips running-job/overlay detection
    entirely; omitting `repo_cache` (the default) skips account-wide
    discovery entirely and falls back to the pre-v1.5.1 behavior of
    polling exactly `cfg["ci_status"]["repos"]` every cycle.

    Account-wide watching (v1.5.1): when `repo_cache` is given, the
    effective repo list for this poll is resolved fresh each call via
    `resolve_repo_list` (cheap -- it's a set operation over already-cached
    data, not a network call) from `repos`/`repos_exclude`/
    `watch_account_repos`/`active_within_days`, re-enumerating the
    account's repos via `_refresh_account_repos` only when that cache is
    older than `repo_refresh_minutes` (or empty). Any repo present in
    `state_cache`/`running_cache` but absent from the freshly-resolved
    list -- excluded, aged out of the active window, or deleted upstream
    -- has its cached state dropped (and the poller's own per-repo ETag
    slots forgotten via `forget_repo`) so a stale failure/stuck alert or
    running badge can't linger for a repo that's no longer being watched.

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

    Alert snooze (v1.5.2, `snooze_state`): omitting it (the default) skips
    the snooze subsystem entirely -- every poll behaves exactly as if
    nothing were ever snoozed. When given, see
    `ci_status.logic.update_snooze`'s docstring for the full state
    machine; `client.get_busy()` is polled only while there's something
    to track (an alert currently showing, or an existing pending/timed
    snooze), never on a fully idle poll.
    """
    c = cfg["ci_status"]
    timeout_s = int(c["poll_seconds"] * 1.5)

    if repo_cache is not None:
        account_repos = (_refresh_account_repos(poller, repo_cache, now,
                                                 c.get("repo_refresh_minutes", 60))
                         if c.get("watch_account_repos") else None)
        effective_repos = resolve_repo_list(
            c["repos"], c.get("repos_exclude", []), bool(c.get("watch_account_repos")),
            account_repos, c.get("active_within_days", 30), now)
    else:
        effective_repos = c["repos"]

    # Drop cached state for any repo that left the effective list this
    # poll (excluded, aged out, deleted upstream) so a stale alert or
    # running badge can't linger for a repo no longer being watched.
    for repo in set(state_cache) - set(effective_repos):
        state_cache.pop(repo, None)
        poller.forget_repo(repo)
    if running_cache is not None:
        for repo in set(running_cache) - set(effective_repos):
            running_cache.pop(repo, None)
            poller.forget_repo(repo)

    for repo in effective_repos:
        runs = poller.fetch_runs(repo)
        if runs is not None:  # None = 304/no-change/error -> keep cached state
            state_cache[repo] = evaluate_runs(repo, runs, now,
                                              c["stale_queued_minutes"])
    states = list(state_cache.values())
    has_alert = any(s.failing or s.stuck for s in states)

    # Alert snooze via the device's native start button (v1.5.2) -- see
    # ci_status.logic.update_snooze's docstring for the full state
    # machine. get_busy() is polled only while there's something to
    # track (an alert showing, or an existing pending/timed snooze),
    # never on a fully idle poll, to keep idle cycles lean.
    suppress_alert = False
    suppress_led = False
    if snooze_state is not None:
        snooze_minutes = c.get("snooze_minutes", 0)
        alert_fingerprint = compute_alert_fingerprint(states)
        should_poll_busy = not dry_run and snooze_minutes > 0 and (
            bool(alert_fingerprint) or bool(snooze_state.get("fingerprint")))
        if should_poll_busy:
            busy = client.get_busy() or {}
            busy_active = busy.get("type") not in (None, "NOT_STARTED")
        else:
            # None, not False -- "not polled this cycle," distinct from a
            # confirmed-inactive observation. update_snooze only commits
            # its session_was_active tracking when given a real bool; see
            # its docstring for the exact bug a dummy False here caused
            # (a false-positive auto-snooze for a session that predated
            # the alert).
            busy_active = None
        suppress_alert, suppress_led = update_snooze(
            alert_fingerprint, busy_active, now, snooze_minutes, snooze_state)

    # While snoozed, ci_status behaves as if nothing is failing/stuck at
    # all for every OTHER precedence purpose too -- "running/quota
    # rotation and green behavior unaffected" is the explicit design
    # intent, not just the alert badge's own rendering (build_ci_payload,
    # below, gets the same suppress_alert). The overlay rotation's own
    # "an alert takes precedence" check needs the identical view.
    effective_has_alert = has_alert and not suppress_alert

    overlay_payload = None
    frame_index = 0
    stay_silent = False
    frame_data_unavailable = False

    # running_cache check first: short-circuits before touching c["show_running"],
    # so callers/tests using an older, fully-spelled-out cfg dict that predates
    # this key (and never pass running_cache) don't KeyError.
    if running_cache is not None and c["show_running"]:
        for repo in effective_repos:
            running_runs = poller.fetch_running_runs(repo)
            if running_runs is not None:  # None = 304/no-change/error -> keep cached
                running_cache[repo] = running_runs
        selected = select_running_run(running_cache)

        if selected is None or effective_has_alert:
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

    payload = build_ci_payload(states, c["show_green"], timeout_s, overlay=overlay_payload,
                               suppress_alert=suppress_alert, suppress_led=suppress_led)
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
    """Cadence switch: `running_poll_seconds` while any *currently
    watched* repo has a running run, `poll_seconds` otherwise. Checks
    every key `running_cache` actually holds (not `cfg_ci["repos"]`) --
    account-wide watching (v1.5.1) means the set of repos with entries in
    `running_cache` can include auto-discovered repos that were never in
    the explicit `repos` list at all; iterating `cfg_ci["repos"]` would
    silently miss an active run on any of those and never shorten the
    poll interval for it. A pure function of `running_cache`'s
    post-`run_once` state so it's testable without mocking `time.sleep`.
    """
    any_running = any(running_cache.values())
    return cfg_ci["running_poll_seconds"] if any_running else cfg_ci["poll_seconds"]


def config_requires_repos(cfg: dict) -> str | None:
    """Validates that this config gives ci_status *something* to watch --
    either an explicit `repos` list, or `watch_account_repos = true`
    (which discovers repos at runtime, so an empty `repos` list is valid
    in that mode -- see v1.5.1's account-wide watching). Returns the
    error message to log if neither is satisfied, else None. Pulled out
    of main() as a pure function of `cfg` so this validation is testable
    without exercising the rest of main()'s side effects (argparse,
    logging setup, gh auth, device connection).
    """
    if not cfg["ci_status"]["repos"] and not cfg["ci_status"].get("watch_account_repos"):
        return ("No repos configured. Copy config.example.toml to config.toml "
               "and set [ci_status] repos, or set watch_account_repos = true.")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar CI status")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    error = config_requires_repos(cfg)
    if error is not None:
        log.error(error)
        return 1
    from .github import RestPoller, get_token
    try:
        poller = RestPoller(get_token())
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    client = BusyBarClient(**device_kwargs(cfg))
    client.clear(APP)  # drop any stale elements from a previous process (type collisions 400)

    state_cache: dict[str, RepoState] = {}
    running_cache: dict[str, list[dict]] = {}
    overlay_state: dict = {}
    quota_cache: dict = {}
    repo_cache: dict = {}
    snooze_state: dict = {}
    backoff = 5
    while True:
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run, running_cache=running_cache,
                           overlay_state=overlay_state, quota_cache=quota_cache,
                           repo_cache=repo_cache, snooze_state=snooze_state)
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
