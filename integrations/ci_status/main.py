import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:  # bare clone / broken editable install: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from busybar.client import BusyBarClient, DrawResult
from busybar.config import device_kwargs, load_config
from busybar.display import OVERLAY_DWELL_SECONDS, PRIORITY_OVERLAY, overlay_gap_elapsed
from busybar.presentation import modern_display, prepare_frame, commit_frame, ci_bitmap_accent

from .github import FETCH_DENIED, FETCH_FAILED, RestPoller, get_token
from .logic import (
    RepoState, RunningInfo, QuotaInfo,
    build_overlay_payload, build_overlay_sequence, default_branch_verdicts, evaluate_runs,
    LED_OFF_COLOR, LED_OFF_ELEMENTS, OVERLAY_FRAME_QUOTA_GQL, OVERLAY_FRAME_QUOTA_REST,
    parse_rate_limit, repo_labels, resolve_ci_led_value, resolve_repo_list, running_runs_usable,
    select_running_run,
)

APP = "ci_status"
log = logging.getLogger(APP)

QUOTA_LABELS = {"graphql": "GITHUB GRAPHQL", "core": "GITHUB REST"}
QUOTA_STALE_SECONDS = 300  # never show rate_limit data older than 5 minutes
# How long GitHub must keep refusing a repo (github.FETCH_DENIED: 401, a 403
# that is not a rate limit, 404, 410) before its cached failure/stuck state
# or running badge is dropped, so a deleted or renamed repo or a revoked
# token cannot leave "CI FAIL" on the display for good. "Could not ask"
# (outage, rate limit, server error, unreadable body) never counts: cached
# state then stays for as long as the outage lasts.
STALE_STATE_SECONDS = 900
HEARTBEAT_SECONDS = 600  # each distinct summary is logged at INFO once per this window
# How often main() gives the display its turn. GitHub is asked far less often
# (next_poll_seconds); this only decides how promptly a card that has become
# due is drawn, so the 10 s on / 10 s off rotation is the same at any poll
# cadence. A tick with nothing due sends nothing anywhere.
DISPLAY_TICK_SECONDS = 1
# One rotation slot: a dwell on screen plus the dwell of silence after it. A
# draw the device refused is retried after this long, never once per tick.
OVERLAY_SLOT_SECONDS = 2 * OVERLAY_DWELL_SECONDS
# How long to wait before asking again for a default branch name that could
# not be read (a known name is re-checked every repo_refresh_minutes).
DEFAULT_BRANCH_RETRY_SECONDS = 300


def _denied_too_long(fetch_health: dict, key: tuple[str, str], result,
                     now: datetime) -> bool:
    """Track one (kind, repo) fetch's run of refusals in `fetch_health`
    (time of the first refusal). FETCH_DENIED starts or continues the run;
    FETCH_FAILED ("could not ask") leaves it as it is, neither starting nor
    resetting it; any usable answer, a 304 included, clears it. True once a
    refusal arrives STALE_STATE_SECONDS or more after the run began."""
    if result is FETCH_FAILED:
        return False
    if result is not FETCH_DENIED:
        fetch_health.pop(key, None)
        return False
    since = fetch_health.setdefault(key, now)
    return (now - since).total_seconds() >= STALE_STATE_SECONDS


def _refresh_account_repos(poller, repo_cache: dict | None, now: datetime,
                           repo_refresh_minutes: int) -> list[dict] | None:
    """Re-enumerate the account's owned repos (v1.5.1) when `repo_cache`
    is stale or has never been populated, keeping the previous list on an
    enumeration failure -- never crash, never silently fall back to an
    empty watch list (an empty result from `fetch_account_repos` is
    indistinguishable from "genuinely zero repos," so `None` is the only
    signal treated as "keep what we had"; see that method's docstring).
    Mirrors the quota cache's freshness pattern (`_fetch_quota` / `_quota_infos`).
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
            # Repeats every poll until enumeration succeeds, so it goes
            # through the poller's gate rather than straight to the log.
            poller.warn("account repos",
                        "account repo enumeration failed and no previous list is "
                        "cached yet -- watch_account_repos contributes nothing this poll")
    return repo_cache.get("repos")


def _fetch_quota(poller, quota_cache: dict | None, now: datetime) -> None:
    """Fetch /rate_limit fresh (it's exempt from GitHub's own rate
    limiting, so there's no cost to calling it every poll) and update
    `quota_cache` on success. A failed fetch leaves the cache as it was;
    `_quota_infos` decides whether what it holds is still fresh enough."""
    if quota_cache is None:
        return
    raw = poller.fetch_rate_limit()
    if raw is not None:
        parsed = parse_rate_limit(raw)
        if parsed is not None:
            quota_cache["buckets"] = parsed
            quota_cache["fetched_at"] = now


def _quota_infos(quota_cache: dict | None, now: datetime) -> dict[str, QuotaInfo] | None:
    """The bucket->QuotaInfo mapping if `quota_cache` holds data no older
    than QUOTA_STALE_SECONDS, else None -- callers must treat None as "no
    quota frames this cycle," never fall back to stale numbers. Makes no
    request, so display ticks between polls can call it."""
    if quota_cache is None:
        return None
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


def _default_branches(poller, repos: list[str], account_repos: list[dict] | None,
                      branch_cache: dict | None, now: datetime,
                      refresh_minutes: int) -> dict[str, str]:
    """repo -> default branch name, for every repo whose name is known.

    The account enumeration already carries it, so a repo listed there costs
    nothing; with `branch_cache` given, that name is also remembered. Any other repo (one named in `repos` that the account does not
    own) is looked up with `poller.fetch_default_branch` -- only when
    `branch_cache` is given -- and re-checked every `refresh_minutes` to
    notice a rename. While the name is unknown the lookup is retried every
    DEFAULT_BRANCH_RETRY_SECONDS, never once per poll. A failed re-check
    keeps the name already learned.

    A repo missing from the result is judged without default-branch
    tracking: the newest run of each workflow across all branches decides,
    as it did before that tracking existed.
    """
    from_account = {
        r["full_name"].lower(): r["default_branch"] for r in account_repos or []
        if isinstance(r, dict) and isinstance(r.get("full_name"), str)
        and isinstance(r.get("default_branch"), str) and r["default_branch"]
    }
    names: dict[str, str] = {}
    for repo in repos:
        name = from_account.get(repo.lower())
        if name is not None and branch_cache is not None:
            # Remembered, so a repo that later drops out of the enumeration
            # (it stays watched through `repos`) keeps a known name.
            branch_cache[repo] = {"name": name, "checked_at": now}
        if name is None and branch_cache is not None:
            entry = branch_cache.get(repo)
            wait = refresh_minutes * 60 if entry and entry["name"] else DEFAULT_BRANCH_RETRY_SECONDS
            if entry is None or (now - entry["checked_at"]).total_seconds() >= wait:
                fetched = poller.fetch_default_branch(repo)
                known = fetched if isinstance(fetched, str) else (entry["name"] if entry else None)
                entry = branch_cache[repo] = {"name": known, "checked_at": now}
                if known is None:
                    poller.warn(f"default branch {repo}",
                                f"default branch of {repo} is unknown; until it can be read, "
                                "its failures are judged by the newest run on any branch")
            name = entry["name"]
        if name:
            names[repo] = name
    return names


@dataclass
class _View:
    """What one GitHub poll decided belongs on screen. Display ticks between
    polls rotate from this without asking GitHub again; time-dependent text
    (the running badge's countdown, quota freshness) is rebuilt from it
    against the tick's own `now`."""
    states: list[RepoState]
    labels: dict[str, str]
    show_green: bool
    # (run, repo label, other_count, median minutes) of the selected running run.
    running: tuple[dict, str, int, float | None] | None
    show_quota: bool


def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool,
             running_cache: dict[str, list[dict]] | None = None,
             overlay_state: dict | None = None,
             quota_cache: dict | None = None,
             repo_cache: dict | None = None,
             fetch_health: dict | None = None,
             branch_cache: dict | None = None,
             view_cache: dict | None = None,
             poll: bool = True,
             clock=None) -> str:
    """One loop iteration: ask GitHub if this iteration is a poll, then give
    the display its turn.

    `running_cache`, `overlay_state`, `quota_cache`, `repo_cache`,
    `fetch_health`, `branch_cache` and `view_cache`, when passed, are
    caller-owned dicts this function mutates in place (mirroring
    `state_cache`'s existing pattern) so `main()` can hold one instance of
    each across loop iterations while `run_once` itself stays a pure
    function of its arguments plus those dicts. Omitting `running_cache`
    (the default) skips running-job/overlay detection entirely; omitting
    `repo_cache` (the default) skips account-wide discovery entirely and
    falls back to the pre-v1.5.1 behavior of polling exactly
    `cfg["ci_status"]["repos"]` every cycle; omitting `fetch_health` (the
    default) means failure runs are not remembered between calls, so cached
    state never expires; omitting `branch_cache` (the default) means a
    default branch is known only for repos the account enumeration lists.

    Poll and display are separate so cards rotate on the display's own
    rhythm whatever the GitHub cadence is. `poll=False` with a `view_cache`
    that holds an earlier poll's result is a display tick: it sends nothing
    to GitHub and rotates from that result. Every other call polls first
    (see `_poll_github`) and stores the result in `view_cache` when given.
    `main()` calls this every DISPLAY_TICK_SECONDS and sets `poll` only when
    `next_poll_seconds` has elapsed. See `_show` for the display half.

    `clock`, when given, returns the current time; the display half then
    uses it instead of `now`, so a card's dwell is timed from when it is
    actually drawn rather than from when a slow poll before it began.
    """
    c = cfg["ci_status"]
    view = view_cache.get("view") if view_cache is not None and not poll else None
    polled = view is None
    if polled:
        view = _poll_github(poller, c, now, state_cache, running_cache, quota_cache,
                            repo_cache, fetch_health, branch_cache)
        if view_cache is not None:
            view_cache["view"] = view
        if clock is not None:
            now = clock()
    return _show(client, c, now, view, overlay_state, quota_cache, dry_run, polled)


def _poll_github(poller, c: dict, now: datetime, state_cache: dict[str, RepoState],
                 running_cache: dict[str, list[dict]] | None, quota_cache: dict | None,
                 repo_cache: dict | None, fetch_health: dict | None,
                 branch_cache: dict | None) -> _View:
    """Ask GitHub about every watched repo and update the caller's caches.

    GitHub answers four ways per repo (see `github.FETCH_FAILED`): a list
    (new body), None (304: the cached copy is current), FETCH_FAILED (could
    not ask), or FETCH_DENIED (a refusal). Cached state survives the last
    three, and is re-evaluated against `now` every poll so a queued run
    keeps ageing toward "stuck" even while GitHub answers 304. A body this
    code cannot read skips that repo for the poll with one gated warning and
    counts as "could not ask". Only refusals spanning `STALE_STATE_SECONDS`
    drop the repo's cached state (and, separately, its running badge) along
    with that slot's ETag, so a later 304 cannot vouch for state that is no
    longer held; one warning per poll lists every repo dropped.
    While any watched repo has no state, the green "ALL CLEAR" frame is
    withheld: it is a claim about every repo, not about the ones that
    happened to answer.

    Default branch: a repo whose default branch is known (`_default_branches`)
    gets a second conditional request, for that branch only. Its verdicts
    (`logic.default_branch_verdicts`) are remembered in the repo's state and
    decide the default-branch failure cards, so a failure there stays until
    that workflow passes on the default branch -- whatever runs elsewhere,
    and however many newer runs there are. That request's 304, failure or
    refusal leaves the remembered verdicts standing; they are dropped only
    with the rest of the repo's state. A repo with no known default branch
    makes only the first request and is judged across all branches.

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
    """
    account_repos = None
    if repo_cache is not None:
        account_repos = (_refresh_account_repos(poller, repo_cache, now,
                                                 c.get("repo_refresh_minutes", 60))
                         if c.get("watch_account_repos") else None)
        effective_repos = resolve_repo_list(
            c["repos"], c.get("repos_exclude", []), bool(c.get("watch_account_repos")),
            account_repos, c.get("active_within_days", 30), now)
    else:
        effective_repos = c["repos"]
    if fetch_health is None:
        fetch_health = {}

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
    for key in [k for k in fetch_health if k[1] not in effective_repos]:
        del fetch_health[key]
    if branch_cache is not None:
        for repo in set(branch_cache) - set(effective_repos):
            del branch_cache[repo]
            poller.forget_repo(repo)

    labels = repo_labels(list(effective_repos))  # display names only; API calls keep the full name
    branches = _default_branches(poller, effective_repos, account_repos, branch_cache, now,
                                 c.get("repo_refresh_minutes", 60))
    unreadable = (LookupError, TypeError, ValueError, AttributeError)

    dropped: set[str] = set()   # repos whose cached state expired this poll
    for repo in effective_repos:
        fetched = poller.fetch_runs(repo)
        answered = fetched is not FETCH_FAILED and fetched is not FETCH_DENIED
        cached = state_cache.get(repo)
        # A new body, else the runs behind the cached state (304 or failure).
        runs = fetched if answered and fetched is not None else (cached.runs if cached else None)

        branch = branches.get(repo)
        verdicts = cached.default_verdicts if cached else None
        if branch is not None and verdicts is not None and isinstance(fetched, list):
            # A new all-branches page is the newest view there is: its newest
            # default-branch run of a workflow is that workflow's newest
            # default-branch run. Folding it in first keeps a fresh failure
            # showing while the default-branch page itself cannot be read; the
            # default-branch page, read after it, then has the last word. A
            # cached (304) page is never folded again, since it may be older
            # than the default-branch page already applied.
            try:
                verdicts = default_branch_verdicts(repo, fetched, branch, verdicts)
            except unreadable:
                pass   # evaluate_runs below reports this body as unreadable
        if branch is not None:
            default_runs = poller.fetch_default_runs(repo, branch)
            if isinstance(default_runs, list):
                try:
                    verdicts = default_branch_verdicts(repo, default_runs, branch, verdicts)
                except unreadable as exc:
                    # Same ETag reasoning as below: re-read it next poll.
                    poller.forget_default_runs(repo)
                    poller.warn(f"default runs {repo}",
                                f"skipping {repo}'s {branch} branch this poll: "
                                f"unreadable run data ({exc!r})")
                else:
                    if runs is None:
                        # No state yet to hold these verdicts. Forget the ETag
                        # so the page is read again rather than answered 304.
                        poller.forget_default_runs(repo)

        if runs is not None:
            try:
                state_cache[repo] = evaluate_runs(repo, runs, now, c["stale_queued_minutes"],
                                                  default_branch=branch,
                                                  default_verdicts=verdicts)
            except unreadable as exc:
                fetched = FETCH_FAILED   # could not read it: says nothing about the repo
                # The poller already committed this body's ETag; forget it so
                # the next poll re-reads the body instead of getting a 304.
                poller.forget_repo(repo, running=False)
                poller.warn(f"runs {repo}", f"skipping {repo} this poll: "
                                            f"unreadable run data ({exc!r})")
        if _denied_too_long(fetch_health, ("runs", repo), fetched, now) \
                and state_cache.pop(repo, None) is not None:
            poller.forget_repo(repo, running=False)
            dropped.add(repo)

    # Running detection (unchanged gating): a live run enables the CI badge and,
    # with show_quota, the quota frames. Independent of failures, which rotate
    # regardless of whether anything is running.
    running = None
    if running_cache is not None and c["show_running"]:
        for repo in effective_repos:
            running_runs = poller.fetch_running_runs(repo)
            if isinstance(running_runs, list):
                if running_runs_usable(running_runs):
                    running_cache[repo] = running_runs
                else:
                    running_runs = FETCH_FAILED
                    poller.forget_repo(repo, runs=False)   # same ETag reasoning as above
                    poller.warn(f"running {repo}", f"skipping {repo} this poll: "
                                                   "unreadable running-run data")
            if _denied_too_long(fetch_health, ("running", repo), running_runs, now) \
                    and running_cache.pop(repo, None):
                poller.forget_repo(repo, runs=False)
                dropped.add(repo)
        selected = select_running_run(running_cache)
        if selected is not None:
            run, repo, other_count = selected
            median = poller.fetch_median_eta(repo, run["workflow_id"])
            running = (run, labels.get(repo, repo), other_count, median)
            if c["show_quota"]:
                _fetch_quota(poller, quota_cache, now)

    if dropped:
        # One line per poll, not per repo: a revoked token refuses every repo
        # at once. Each repo is dropped once; it reappears only after a good
        # answer, so this cannot repeat every poll.
        log.warning("GitHub has refused %s for %d min; dropped cached CI state",
                    ", ".join(sorted(dropped)), STALE_STATE_SECONDS // 60)

    return _View(
        states=list(state_cache.values()), labels=labels,
        # "ALL CLEAR" only when every watched repo has actually been read.
        show_green=c["show_green"] and all(repo in state_cache for repo in effective_repos),
        running=running, show_quota=bool(running and c["show_quota"]))


def _show(client, c: dict, now: datetime, view: _View, overlay_state: dict | None,
          quota_cache: dict | None, dry_run: bool, polled: bool) -> str:
    """Give the display its turn: draw the next card if one is due.

    Overlay rotation: the overlay tier draws one frame per dwell slot,
    cycling through `build_overlay_sequence(...)`'s ordered frame list (see
    the "Failure/stuck/green rotation" paragraph below for what populates
    it). A dwell slot only fires once `overlay_gap_elapsed(last_dwell_end, now) >=
    OVERLAY_DWELL_SECONDS` (busybar.display's contract: stay silent at
    least one full dwell so the ambient calendar has a real chance to
    reclaim the screen in between -- see busybar/display.py and the spec
    doc's v1.5 section for why). `overlay_state`'s `frame_index` and
    `last_dwell_end` only commit once `client.draw` actually returns
    DRAWN, same discipline as calendar_countdown's transition-state fix: a
    failed draw must not be mistaken for a completed dwell, or the
    rotation would silently skip frames / wait a dwell for nothing.

    Called every DISPLAY_TICK_SECONDS, so the rhythm is one card for a
    dwell, then a dwell of silence, at any GitHub poll cadence. Most ticks
    fall inside a dwell or its silence and touch nothing. Two rules keep
    the faster tick from sending the device more requests than one per
    slot: with nothing to show, the device is cleared on polls only
    (`polled`), as it was before the tick existed; and a draw the device
    refused or failed (REJECTED while the calendar holds a higher tier, or
    ERROR) is retried after OVERLAY_SLOT_SECONDS, not on the next tick. An
    UNREACHABLE draw is not held back here: `main()` already sleeps its
    backoff before calling again.

    The shared presentation helper remembers the last accepted shape, types
    and priority. Modern local firmware removes obsolete IDs at same-priority
    transitions; legacy or incompatible transitions clear this app's canvas.
    Rotation bookkeeping resets must retain that display state until the
    next successful draw or clear. Complete payloads still renew all TTLs and
    recover evicted content. CI bitmap icons are optional cosmetic additions.

    Failure/stuck/green rotation (v1.6, Request B): the overlay tier's
    rotation is no longer gated on a run being active -- `build_overlay_sequence`
    folds in one frame per failing run, one per stuck run, the running CI
    badge (only while a run is active), each available quota frame, and a
    single quiet-green frame (only when nothing else is present and
    `show_green` is on), and the dwell/silence gate above applies uniformly
    across all of them. This means a failure rotates into view and keeps
    rotating even with no CI run currently in progress, instead of only
    showing while `running_cache`/`show_running` happened to have something
    active. `overlay_state["led_was_on"]` tracks the failure-driven LED
    across calls (see `resolve_ci_led_value`): it commits to
    `led_should_be_on` only once a draw actually lands (DRAWN), same DRAWN
    discipline as `frame_index`/`last_dwell_end`.
    """
    states = view.states
    running_info = None
    quota_by_bucket = None
    quota_frames: list[str] = []
    if view.running is not None:
        run, repo_label, other_count, median = view.running
        running_info = RunningInfo(run=run, repo=repo_label, other_count=other_count,
                                   median_minutes=median, now=now)
        if view.show_quota:
            quota_by_bucket = _quota_infos(quota_cache, now)
            if quota_by_bucket:
                if "graphql" in quota_by_bucket:
                    quota_frames.append(OVERLAY_FRAME_QUOTA_GQL)
                if "core" in quota_by_bucket:
                    quota_frames.append(OVERLAY_FRAME_QUOTA_REST)

    sequence = build_overlay_sequence(states, running_present=running_info is not None,
                                      quota_frames=quota_frames, show_green=view.show_green)

    # Failure-driven LED (Request B keeps a gentle cue). led_should_be_on is
    # driven by FAILURES only -- stuck keeps its historical LED-None behavior.
    led_should_be_on = any(s.failing for s in states)
    led_was_on = bool(overlay_state.get("led_was_on")) if overlay_state is not None else False
    led_value = resolve_ci_led_value(led_should_be_on, led_was_on)

    # Empty sequence -> nothing to show. Honor an explicit LED-off transition
    # (there is no failure now, so led_value is either LED_OFF_COLOR or None).
    if not sequence:
        if overlay_state is not None:
            overlay_state.pop("frame_index", None)
            overlay_state.pop("last_dwell_end", None)
            overlay_state.pop("retry_at", None)
        if dry_run:
            return "DRY-RUN: nothing to show"
        if not polled:
            return "nothing to show"   # the poll already cleared; leave the device alone
        if led_value == LED_OFF_COLOR:
            result = client.draw(APP, LED_OFF_ELEMENTS, priority=PRIORITY_OVERLAY,
                                 led_notification_color=LED_OFF_COLOR)
            if result == DrawResult.DRAWN and overlay_state is not None:
                overlay_state["led_was_on"] = False
                commit_frame(overlay_state, LED_OFF_ELEMENTS, PRIORITY_OVERLAY, result)
            return f"led off; {result.value}"
        client.clear(APP)
        if overlay_state is not None:
            overlay_state["last_shape"] = None
        return "nothing to show; cleared"

    # Dwell gate: one frame per dwell, then silent one dwell so the ambient
    # calendar can reclaim the gap. frame_index/last_dwell_end commit only on DRAWN.
    seq_len = len(sequence)
    frame_index = (overlay_state.get("frame_index", 0) if overlay_state is not None else 0) % seq_len
    last_dwell_end = overlay_state.get("last_dwell_end") if overlay_state is not None else None
    if overlay_gap_elapsed(last_dwell_end, now) < OVERLAY_DWELL_SECONDS:
        return "overlay dwell gap; staying silent (letting the ambient app reclaim the screen)"
    retry_at = overlay_state.get("retry_at") if overlay_state is not None else None
    if retry_at is not None and now < retry_at:
        return "overlay draw was refused; waiting for the next slot"

    payload = build_overlay_payload(sequence[frame_index], OVERLAY_DWELL_SECONDS,
                                    running=running_info, quota_by_bucket=quota_by_bucket,
                                    show_spinner=c.get("running_spinner", False),
                                    repo_labels=view.labels)

    if dry_run:
        return f"DRY-RUN payload: {payload!r} led={led_value}"

    modern = modern_display(client)
    elements = payload["elements"]
    if modern and c.get("bitmap_icons", True):
        elements = ci_bitmap_accent(elements, sequence[frame_index]["kind"],
                                    OVERLAY_DWELL_SECONDS)
    payload["elements"] = prepare_frame(client, APP, elements, payload["priority"],
                                         overlay_state, modern=modern)

    result = client.draw(APP, payload["elements"], priority=payload["priority"],
                         led_notification_color=led_value)
    if overlay_state is not None:
        if result == DrawResult.DRAWN:
            commit_frame(overlay_state, payload["elements"], payload["priority"], result)
            overlay_state["led_was_on"] = led_should_be_on
            overlay_state["frame_index"] = frame_index + 1
            overlay_state["last_dwell_end"] = now + timedelta(seconds=OVERLAY_DWELL_SECONDS)
            overlay_state.pop("retry_at", None)
        elif result != DrawResult.UNREACHABLE:
            overlay_state["retry_at"] = now + timedelta(seconds=OVERLAY_SLOT_SECONDS)

    text = next(e["text"] for e in payload["elements"]
                if e["type"] == "text" and e["id"] != "ci_header")
    header = next((e["text"] for e in payload["elements"] if e["id"] == "ci_header"), "")
    text = f"{header} {text}".strip()
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
    This is the interval between GitHub polls only; main() gives the display
    a turn every DISPLAY_TICK_SECONDS regardless.
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


def should_log_info(summary: str, logged: set[str], seconds_since_window_start: float,
                    heartbeat_seconds: int = HEARTBEAT_SECONDS) -> bool:
    """Log-noise control: INFO for a summary not yet logged in the current
    heartbeat window, or once the window has run out (so a long unchanging
    run still leaves a periodic "yes, I'm alive" trail) -- DEBUG otherwise.

    calendar_countdown and nyan_filler compare against the last line only.
    Here cards alternate with silence every few seconds, so "changed since
    the last line" would log every card every time it came round; `logged`
    holds every summary logged this window instead. main() empties it when
    the window runs out, which also bounds its size.
    """
    return summary not in logged or seconds_since_window_start >= heartbeat_seconds


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
    try:
        poller = RestPoller(get_token())
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    client = BusyBarClient(**device_kwargs(cfg))
    if not args.dry_run:
        client.clear(APP)  # drop stale elements from a previous process

    state_cache: dict[str, RepoState] = {}
    running_cache: dict[str, list[dict]] = {}
    overlay_state: dict = {}
    quota_cache: dict = {}
    repo_cache: dict = {}
    fetch_health: dict = {}
    branch_cache: dict = {}
    view_cache: dict = {}
    backoff = 5
    logged: set[str] = set()
    window_start = time.monotonic()
    next_poll_at = window_start   # the first iteration polls
    while True:
        # The display gets a turn every iteration; GitHub only when a poll is due.
        poll = time.monotonic() >= next_poll_at
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run, running_cache=running_cache,
                           overlay_state=overlay_state, quota_cache=quota_cache,
                           repo_cache=repo_cache, fetch_health=fetch_health,
                           branch_cache=branch_cache, view_cache=view_cache, poll=poll,
                           clock=lambda: datetime.now(timezone.utc))
        now_monotonic = time.monotonic()
        if poll:
            next_poll_at = now_monotonic + next_poll_seconds(cfg["ci_status"], running_cache)
        in_window = now_monotonic - window_start
        if args.once or should_log_info(summary, logged, in_window):
            if in_window >= HEARTBEAT_SECONDS:
                logged.clear()
                window_start = now_monotonic
            log.info(summary)
            logged.add(summary)
        else:
            log.debug(summary)
        if args.once:
            return 0
        if summary.endswith("unreachable"):  # device offline: back off, not every tick
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(DISPLAY_TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
