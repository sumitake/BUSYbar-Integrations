from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from busybar.client import DrawResult
from busybar.display import PRIORITY_OVERLAY, OVERLAY_DWELL_SECONDS
from ci_status.logic import FailingRun, RepoState
from ci_status.github import FETCH_DENIED, FETCH_FAILED, RestPoller
from ci_status.main import (run_once, next_poll_seconds, config_requires_repos,
                            should_log_info, STALE_STATE_SECONDS,
                            DEFAULT_BRANCH_RETRY_SECONDS, DISPLAY_TICK_SECONDS)

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)
CFG = {"ci_status": {"poll_seconds": 120, "repos": ["o/r"],
                     "show_green": False, "stale_queued_minutes": 0}}
# Full config including the v1.5 running-badge keys, for tests that exercise
# that path (the bare CFG above deliberately predates those keys, to prove
# run_once stays backward compatible with callers/configs that omit them --
# see test_running_detection_skipped_when_running_cache_omitted).
CFG_RUNNING = {"ci_status": {"poll_seconds": 120, "running_poll_seconds": 20,
                             "repos": ["o/r"], "show_green": False,
                             "stale_queued_minutes": 0, "show_running": True,
                             "show_quota": False}}
CFG_QUOTA = {"ci_status": {**CFG_RUNNING["ci_status"], "show_quota": True}}
CFG_GREEN = {"ci_status": {**CFG_RUNNING["ci_status"], "show_green": True}}


def _run(conclusion: str) -> dict:
    return {"workflow_id": 1, "name": "tests", "status": "completed",
            "conclusion": conclusion, "created_at": "2026-08-03T13:30:00Z"}


def _drawn_texts(client) -> list[str]:
    els = client.draw.call_args.kwargs.get("elements") or client.draw.call_args.args[1]
    return [e["text"] for e in els if e.get("type") == "text"]


def test_draws_red_on_failure():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY  # 21, not 60
    assert client.draw.call_args.kwargs["led_notification_color"] == "#FF0000FF"
    assert "CI FAIL" in summary
    assert CFG["ci_status"]["repos"][0] in summary


def test_failure_rotates_with_no_running_job():
    # No running_cache at all: a failure must still draw (Request B) -- the old
    # code drew a priority-60 alert here; now it's an overlay-tier frame.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state={})
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY


def test_failure_frame_respects_dwell_silence():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    overlay_state: dict = {}
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state=overlay_state)
    client.draw.assert_called_once()
    client.reset_mock()
    soon = NOW + timedelta(seconds=OVERLAY_DWELL_SECONDS - 1)
    run_once(client, poller, CFG, soon, {}, dry_run=False, overlay_state=overlay_state)
    client.draw.assert_not_called(); client.clear.assert_not_called()


def test_led_turns_off_explicitly_when_failure_clears():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    overlay_state: dict = {}
    # Poll 1: failing -> LED red, led_was_on committed True
    poller.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state=overlay_state)
    assert overlay_state["led_was_on"] is True
    client.reset_mock()
    # Poll 2: now green, nothing else to draw -> explicit off via LED_OFF_ELEMENTS
    poller.fetch_runs.return_value = [_run("success")]
    later = NOW + timedelta(seconds=OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG, later, {}, dry_run=False, overlay_state=overlay_state)
    assert client.draw.call_args.kwargs["led_notification_color"] == "#00000000"
    assert overlay_state["led_was_on"] is False


def test_led_stays_off_omitted_when_already_clear():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, overlay_state={})
    client.clear.assert_called_once_with("ci_status")  # nothing to draw, LED never was on
    client.draw.assert_not_called()


def test_green_folds_into_rotation_at_overlay_tier():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG["ci_status"], "show_green": True}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False, overlay_state={})
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY
    assert "CI OK" in _drawn_texts(client)
    assert "ALL CLEAR" in _drawn_texts(client)


def test_clears_when_green():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False)
    client.clear.assert_called_once_with("ci_status")


def test_304_keeps_previous_state():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = None  # 304 / error
    cache = {}
    # Seed cache via an initial failing poll, then a 304 poll must still draw red.
    poller_seed = Mock(); poller_seed.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller_seed, CFG, NOW, cache, dry_run=False)
    client.reset_mock(); client.draw.return_value = DrawResult.DRAWN
    run_once(client, poller, CFG, NOW, cache, dry_run=False)
    client.draw.assert_called_once()


def test_dry_run_touches_nothing():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=True)
    client.draw.assert_not_called(); client.clear.assert_not_called()
    assert "DRY-RUN" in summary


# --- running badge wiring -------------------------------------------------------

def _running_run(started_min_ago: float = 3, workflow_id: int = 1, name: str = "tests") -> dict:
    started = (NOW - timedelta(minutes=started_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"workflow_id": workflow_id, "name": name, "status": "in_progress",
           "run_started_at": started, "head_branch": "main", "pull_requests": []}


def test_draws_running_badge_at_priority_21_when_run_active():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]   # no failure/stuck
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = 10.0
    running_cache: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache=running_cache)
    # 21 (PRIORITY_OVERLAY), not the literal priority=20 the feature brief
    # specified -- see busybar/display.py's PRIORITY_OVERLAY docstring for
    # the empirical (probe-verified) reason a strictly-higher priority is
    # required.
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY == 21
    assert running_cache["o/r"] == [_running_run()]

def test_running_badge_fetches_median_for_selected_runs_workflow():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run(workflow_id=99)]
    poller.fetch_median_eta.return_value = None
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache={})
    poller.fetch_median_eta.assert_called_once_with("o/r", 99)

def test_no_running_badge_when_show_running_false():
    cfg = {"ci_status": {**CFG_RUNNING["ci_status"], "show_running": False}}
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    run_once(client, poller, cfg, NOW, {}, dry_run=False, running_cache={})
    poller.fetch_running_runs.assert_not_called()
    client.clear.assert_called_once_with("ci_status")   # falls through to "all green"

def test_running_detection_skipped_when_running_cache_omitted():
    # Backward compatible: a caller (or an older-shaped cfg dict, like the
    # bare CFG above) that doesn't pass running_cache never touches
    # show_running/running_poll_seconds/show_quota -- no KeyError even
    # though CFG predates those keys.
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False)
    poller.fetch_running_runs.assert_not_called()
    assert "cleared" in summary

def test_without_overlay_state_gate_always_open_draws_every_poll():
    # overlay_state omitted entirely -- run_once can't remember a previous
    # dwell, so the gate can't meaningfully close; every poll with an
    # active run draws. (This is the pre-dwell-gate behavior, preserved
    # for callers that don't care about the alternation mechanics.)
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache={})
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False, running_cache={})
    assert client.draw.call_count == 2


# --- overlay dwell gate ----------------------------------------------------------

def test_first_overlay_draw_is_never_gated():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    summary = run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
                       running_cache={}, overlay_state={})
    client.draw.assert_called_once()
    assert "silent" not in summary

def test_second_poll_within_dwell_stays_silent():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.draw.reset_mock()
    soon_after = NOW + timedelta(seconds=OVERLAY_DWELL_SECONDS - 1)
    summary = run_once(client, poller, CFG_RUNNING, soon_after, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.draw.assert_not_called()
    client.clear.assert_not_called()
    assert "silent" in summary

def test_poll_after_dwell_elapsed_draws_again():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.draw.reset_mock()
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.draw.assert_called_once()

def test_dwell_state_does_not_commit_on_failed_draw():
    # If draw() doesn't land, overlay_state must not advance -- otherwise
    # the next poll would wait a full dwell for a "dwell" that never
    # actually rendered anything (same discipline as calendar_countdown's
    # transition-state DRAWN gate).
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}

    client.draw.return_value = DrawResult.UNREACHABLE
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    assert "last_dwell_end" not in overlay_state

    client.draw.return_value = DrawResult.DRAWN
    soon_after = NOW + timedelta(seconds=1)
    run_once(client, poller, CFG_RUNNING, soon_after, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    # No dwell was ever successfully committed, so this immediate retry
    # (only 1s later) must still draw, not be gated.
    assert client.draw.call_count == 2

def test_overlay_state_resets_when_run_ends():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    assert overlay_state.get("last_dwell_end") is not None

    poller.fetch_running_runs.return_value = []   # run finished
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={"o/r": [_running_run()]}, overlay_state=overlay_state)
    # Only the rotation bookkeeping (frame_index/last_dwell_end) resets
    # here -- the "run ended, nothing else to show" branch happens to
    # also reach the explicit client.clear()/"all green" path this same
    # poll (show_green is False in CFG_RUNNING), so last_shape correctly
    # becomes None too: the device really is blank now, this poll. The
    # critical-bug regression coverage -- last_shape surviving a
    # bookkeeping-only reset that does NOT clear the device this same
    # poll (e.g. an alert preempting the overlay without falling through
    # to the "nothing to show" branch) -- lives in
    # test_overlay_then_alert_clears_stale_overlay_shape below.
    assert "frame_index" not in overlay_state
    assert "last_dwell_end" not in overlay_state
    assert overlay_state["last_shape"] is None


# --- unified shape tracking across alert / quiet-green / overlay tiers ----------

def test_overlay_then_alert_clears_stale_overlay_shape():
    # Running badge draws first (shape {bg,title,track,track_fill,track_tip,eta});
    # the next poll turns up a failure. Once the rotation's next dwell slot
    # lands on the failure frame (shape {bg,ci_header,ci_rule,ci}), the stale
    # title/track/track_fill/track_tip/eta ink from the badge must clear first --
    # not left to linger until its own ~1.5x-poll timeout. Both the failure
    # frame and the running badge are now part of the SAME dwell-gated
    # rotation (Request B), so `frame_index` is pinned to 0 to deterministically
    # land on the (newly added) failure slot rather than depend on where the
    # rotation's cursor happens to sit after the composition changed.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()   # nothing on screen before -- no clear needed yet

    poller.fetch_runs.return_value = [_run("failure")]
    overlay_state["frame_index"] = 0
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    summary = run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")
    assert "FAIL" in summary
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY

def test_alert_then_overlay_clears_stale_alert_shape():
    # Symmetric direction: an alert draws first (shape {bg,ci_header,ci_rule,ci}); once it
    # resolves and a run is active, the running badge's shape ({bg,title,
    # track,track_fill,track_tip,eta}) differs and must clear the alert's stale
    # elements first. The second poll must land at or beyond a full dwell
    # (Request B: the failure frame is dwell-gated like every other overlay
    # frame now, unlike the old unconditionally-drawing alert-tier path).
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()   # first-ever draw -- nothing to clear yet
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY

    poller.fetch_runs.return_value = [_run("success")]   # alert resolves
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")

def test_quiet_green_then_overlay_clears_stale_green_shape():
    # The green card draws first; once a run starts, the badge's shape
    # differs and must clear before drawing its own full frame.
    # Green now draws at the overlay tier too, so the second poll must
    # land at or beyond a full dwell for its draw to even be attempted.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = []   # nothing running yet
    overlay_state: dict = {}
    summary = run_once(client, poller, CFG_GREEN, NOW, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()
    assert overlay_state["last_shape"] == frozenset({"bg", "ci_header", "ci_rule", "ci"})
    assert client.draw.call_args.kwargs["priority"] == PRIORITY_OVERLAY

    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_GREEN, later, {}, dry_run=False,
            running_cache={"o/r": []}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")


# --- overlay rotation (quota frames) ---------------------------------------------

def _quota_body(gql_remaining=2600, core_remaining=100):
    return {"resources": {
        "core": {"limit": 5000, "remaining": core_remaining, "reset": 2000000000, "used": 5000 - core_remaining},
        "graphql": {"limit": 5000, "remaining": gql_remaining, "reset": 2000000000, "used": 5000 - gql_remaining},
    }}

def test_rotation_cycles_ci_badge_then_quota_frames():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = _quota_body()
    overlay_state: dict = {}
    quota_cache: dict = {}
    seen = []
    t = NOW
    for _ in range(3):
        run_once(client, poller, CFG_QUOTA, t, {}, dry_run=False,
                running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
        elements = client.draw.call_args.args[1]   # elements is positional, not a kwarg
        by_id = {e["id"]: e for e in elements}
        if "eta" in by_id:
            seen.append("ci_badge")
        else:
            seen.append("quota_gql" if by_id["title"]["text"] == "GQL LEFT" else "quota_rest")
        t += timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    assert seen == ["ci_badge", "quota_gql", "quota_rest"]

def test_rotation_shape_change_clears_first():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = _quota_body()
    overlay_state: dict = {}
    quota_cache: dict = {}
    # First dwell: ci_badge (shape "badge") -- no prior shape, no clear.
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    client.clear.assert_not_called()
    # Second dwell: quota_gql (shape "quota") -- shape changed, must clear first.
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    client.clear.assert_called_once_with("ci_status")
    # Third dwell: quota_rest -- same shape as quota_gql, no clear needed.
    client.clear.reset_mock()
    later2 = later + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_QUOTA, later2, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    client.clear.assert_not_called()

def test_quota_frame_skipped_without_crashing_when_fetch_fails():
    # No successful rate_limit fetch ever (and quota_cache starts empty),
    # so _quota_infos keeps returning None -- build_overlay_sequence never
    # includes a quota frame in the rotation at all (it only folds in frames
    # whose data is already on hand), so the CI badge just keeps redrawing
    # in its place, no crash, no stale/placeholder quota numbers ever shown.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = None   # fetch fails, no cached data ever
    overlay_state: dict = {}
    quota_cache: dict = {}
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)   # ci_badge
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    summary = run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    assert client.draw.call_count == 2   # ci_badge drawn again -- no quota frame ever entered rotation
    assert "eta" in {e["id"] for e in client.draw.call_args.args[1]}
    assert "drawn" in summary

def test_quota_stale_data_not_shown_after_5_minutes():
    # Cached quota data older than QUOTA_STALE_SECONDS is treated as
    # unavailable by _quota_infos -- same as a fetch failure -- so it
    # never surfaces in the rotation; the CI badge draws again instead of
    # showing minutes-old numbers.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    quota_cache: dict = {"buckets": {"graphql": {"limit": 5000, "remaining": 2600, "used": 2400, "reset": 2000000000}},
                         "fetched_at": NOW - timedelta(minutes=6)}   # stale
    poller.fetch_rate_limit.return_value = None   # this poll's fetch also fails
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)   # ci_badge dwell
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    assert client.draw.call_count == 2   # ci_badge drawn again -- stale quota data never shown
    assert "eta" in {e["id"] for e in client.draw.call_args.args[1]}


# --- cadence switch (next_poll_seconds) -----------------------------------------

def test_next_poll_seconds_shortens_while_a_run_is_active():
    running_cache = {"o/r": [_running_run()]}
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running_cache) == 20

def test_next_poll_seconds_reverts_when_idle():
    running_cache = {"o/r": []}
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running_cache) == 120

def test_next_poll_seconds_reverts_when_repo_never_polled():
    assert next_poll_seconds(CFG_RUNNING["ci_status"], {}) == 120

def test_next_poll_seconds_checks_across_all_configured_repos():
    cfg = {**CFG_RUNNING["ci_status"], "repos": ["o/r1", "o/r2"]}
    running_cache = {"o/r1": [], "o/r2": [_running_run()]}
    assert next_poll_seconds(cfg, running_cache) == 20


# --- account-wide repo watching (v1.5.1) -----------------------------------------

CFG_ACCOUNT = {"ci_status": {**CFG_RUNNING["ci_status"], "watch_account_repos": True,
                             "repos": [], "repos_exclude": [], "active_within_days": 30,
                             "repo_refresh_minutes": 60}}

def _account_repo(full_name, pushed_days_ago=1, archived=False):
    pushed = (NOW - timedelta(days=pushed_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"full_name": full_name, "archived": archived, "pushed_at": pushed}

def test_account_mode_polls_discovered_repos():
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = [_account_repo("o/discovered")]
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_runs.assert_called_once_with("o/discovered")

def test_account_mode_off_never_calls_discovery():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {}
    cfg = {"ci_status": {**CFG_RUNNING["ci_status"], "watch_account_repos": False}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_account_repos.assert_not_called()

def test_repo_cache_omitted_falls_back_to_pre_v1_5_1_behavior():
    # No repo_cache passed at all -- exactly cfg["ci_status"]["repos"] is
    # polled, discovery never runs, even with watch_account_repos true.
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG_ACCOUNT["ci_status"], "repos": ["o/explicit"]}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False)
    poller.fetch_account_repos.assert_not_called()
    poller.fetch_runs.assert_called_once_with("o/explicit")


# --- account repo list refresh timing ---------------------------------------------

def test_stale_repo_cache_re_enumerates():
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = [_account_repo("o/a")]
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {"repos": [_account_repo("o/old")],
                        "fetched_at": NOW - timedelta(minutes=61)}   # older than 60min default
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_account_repos.assert_called_once()
    assert repo_cache["fetched_at"] == NOW

def test_fresh_repo_cache_does_not_re_enumerate():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {"repos": [_account_repo("o/a")], "fetched_at": NOW - timedelta(minutes=5)}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    poller.fetch_account_repos.assert_not_called()
    poller.fetch_runs.assert_called_once_with("o/a")   # still uses the cached list

def test_enumeration_failure_keeps_previous_list():
    client = Mock()
    poller = Mock()
    poller.fetch_account_repos.return_value = None   # enumeration fails this poll
    poller.fetch_runs.return_value = [_run("success")]
    repo_cache: dict = {"repos": [_account_repo("o/previously_known")],
                        "fetched_at": NOW - timedelta(minutes=61)}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    # Still watching the previously-cached repo -- never fell back to empty.
    poller.fetch_runs.assert_called_once_with("o/previously_known")
    assert repo_cache["repos"] == [_account_repo("o/previously_known")]

def test_enumeration_failure_with_no_prior_list_logs_warning_and_watches_nothing(caplog):
    # A real RestPoller, so the test proves the warning is suppressed on the
    # second poll, not merely that it was routed somewhere.
    import logging
    client = Mock()
    poller = RestPoller("tok")
    repo_cache: dict = {}   # never successfully populated
    failing = Mock(status_code=500, headers={})
    with patch("ci_status.github.requests.get", return_value=failing) as get, \
            caplog.at_level(logging.WARNING, logger="ci_status"):
        run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
        run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False, repo_cache=repo_cache)
    assert all("/user/repos" in c.args[0] for c in get.call_args_list)   # nothing else polled
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert sum("enumeration failed" in w for w in warnings) == 1
    assert sum("github 500" in w for w in warnings) == 1


# --- state_cache / running_cache pruning for dropped repos -----------------------

def test_state_cache_pruned_when_repo_excluded():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG_ACCOUNT["ci_status"], "repos_exclude": ["o/gone"]}}
    poller.fetch_account_repos.return_value = [_account_repo("o/gone"), _account_repo("o/stays")]
    state_cache = {"o/gone": RepoState("o/gone", ["tests"], [])}   # stale alert from before
    repo_cache: dict = {}
    run_once(client, poller, cfg, NOW, state_cache, dry_run=False, repo_cache=repo_cache)
    assert "o/gone" not in state_cache
    assert "o/stays" in state_cache

def test_running_cache_pruned_when_repo_ages_out():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = []
    poller.fetch_median_eta.return_value = None
    poller.fetch_account_repos.return_value = [_account_repo("o/fresh", pushed_days_ago=1)]
    running_cache = {"o/aged_out": [_running_run()]}   # from a repo no longer in the window
    repo_cache: dict = {}
    run_once(client, poller, CFG_ACCOUNT, NOW, {}, dry_run=False,
            running_cache=running_cache, repo_cache=repo_cache)
    assert "o/aged_out" not in running_cache

def test_dropped_repo_forgets_poller_etag_state():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    cfg = {"ci_status": {**CFG_ACCOUNT["ci_status"], "repos_exclude": ["o/gone"]}}
    poller.fetch_account_repos.return_value = [_account_repo("o/stays")]
    state_cache = {"o/gone": RepoState("o/gone", [], [])}
    repo_cache: dict = {}
    run_once(client, poller, cfg, NOW, state_cache, dry_run=False, repo_cache=repo_cache)
    poller.forget_repo.assert_called_once_with("o/gone")

def test_no_pruning_when_effective_list_is_unchanged():
    # Sanity check: the pruning step must not evict a repo that's still
    # in the effective list just because it was cached from an earlier poll.
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    state_cache = {"o/r": RepoState("o/r", ["tests"], [])}
    run_once(client, poller, CFG_RUNNING, NOW, state_cache, dry_run=False)
    assert "o/r" in state_cache
    poller.forget_repo.assert_not_called()


# --- next_poll_seconds sees auto-discovered repos ---------------------------------

def test_next_poll_seconds_shortens_for_auto_discovered_repo_not_in_explicit_repos():
    # o/discovered was never in cfg_ci["repos"] at all -- next_poll_seconds
    # must still see it via running_cache's own keys, not cfg_ci["repos"].
    running_cache = {"o/discovered": [_running_run()]}
    assert next_poll_seconds(CFG_ACCOUNT["ci_status"], running_cache) == 20

def test_next_poll_seconds_reverts_when_all_running_cache_entries_empty():
    running_cache = {"o/a": [], "o/b": []}
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running_cache) == 120


# --- config_requires_repos: main()'s startup validation, extracted for testability

def test_config_requires_repos_errors_when_empty_and_account_mode_off():
    cfg = {"ci_status": {"repos": [], "watch_account_repos": False}}
    err = config_requires_repos(cfg)
    assert err is not None
    assert "No repos configured" in err

def test_config_requires_repos_ok_when_empty_but_account_mode_on():
    cfg = {"ci_status": {"repos": [], "watch_account_repos": True}}
    assert config_requires_repos(cfg) is None

def test_config_requires_repos_ok_when_nonempty_and_account_mode_off():
    cfg = {"ci_status": {"repos": ["o/r"], "watch_account_repos": False}}
    assert config_requires_repos(cfg) is None

def test_config_requires_repos_ok_when_watch_account_repos_key_absent():
    # Backward compat: an old-style cfg dict without the v1.5.1 key at
    # all (predates .get's default) must not crash, and behaves like
    # watch_account_repos=False.
    cfg = {"ci_status": {"repos": ["o/r"]}}
    assert config_requires_repos(cfg) is None
    cfg_empty = {"ci_status": {"repos": []}}
    assert config_requires_repos(cfg_empty) is not None


# --- v1.5.2: REJECTED handling during calendar priority elevation ---------------
#
# calendar_countdown can now draw at PRIORITY_AMBIENT_RAISED (25, inside its
# approach window) or PRIORITY_AMBIENT_URGENT (65, inside its notice/warn
# window, above the top alert-priority tier (60)), evicting ci_status's own
# elements. ci_status's own next redraw attempt at its own priority then gets
# a 409 (DrawResult.REJECTED) while the calendar holds the higher tier --
# expected and silent per busybar.client's own DrawResult.REJECTED
# docstring. This test confirms ci_status's run_once tolerates that cleanly:
# no crash, no state/shape committed on a REJECTED draw, and a full recovery
# once the calendar drops back down and the next draw actually lands.

def test_overlay_dwell_rejected_during_calendar_elevation_resumes_after():
    client = Mock()
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}

    # Poll 1: calendar is in its approach window (PRIORITY_AMBIENT_RAISED=25
    # > overlay's 21) -- the running-badge dwell draw is rejected.
    client.draw.return_value = DrawResult.REJECTED
    summary1 = run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
                        running_cache={}, overlay_state=overlay_state)
    assert "rejected" in summary1
    assert "last_dwell_end" not in overlay_state   # dwell never actually started
    assert "last_shape" not in overlay_state

    # Poll 2: calendar has dropped back down -- the SAME dwell slot (never
    # consumed, since the rejected attempt didn't commit) draws successfully.
    # This is the "no crash, rotation resumes after" requirement. The retry
    # comes one rotation slot later, not on the next 1 s display tick: see
    # test_refused_draw_is_retried_once_per_slot_not_once_per_tick.
    client.draw.return_value = DrawResult.DRAWN
    soon_after = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS)   # the next rotation slot
    summary2 = run_once(client, poller, CFG_RUNNING, soon_after, {}, dry_run=False,
                        running_cache={}, overlay_state=overlay_state)
    assert "drawn" in summary2
    assert overlay_state.get("last_dwell_end") is not None
    assert overlay_state["last_shape"] == frozenset({"bg", "title", "track", "track_fill", "track_tip", "eta"})
    assert client.draw.call_count == 2   # both attempts drew (1st rejected, 2nd landed) -- no crash anywhere


def test_modern_ci_draws_complete_green_card_after_failure():
    client = Mock()
    client.supports_display_v2 = True
    client.remove_elements.return_value = True
    client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    state = {}
    cfg = {"ci_status": {**CFG["ci_status"], "show_green": True}}
    run_once(client, poller, cfg, NOW, {}, False, overlay_state=state)
    first = client.draw.call_args.args[1]
    assert any(e["type"] == "xpmbitmap" for e in first)
    assert state["last_priority"] == PRIORITY_OVERLAY
    poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, cfg, NOW + timedelta(seconds=21), {}, False, overlay_state=state)
    client.remove_elements.assert_not_called()
    client.clear.assert_not_called()
    frame = client.draw.call_args.args[1]
    assert {e["id"] for e in frame} == {"bg", "ci_header", "ci_rule", "ci", "ci_status_icon"}
    by_id = {e["id"]: e for e in frame}
    assert by_id["ci"]["text"] == "ALL CLEAR"
    assert all(e["timeout"] == 10 for e in frame)


# --- GitHub errors expire cached state instead of freezing it --------------------

STALE = timedelta(seconds=STALE_STATE_SECONDS)


def test_persistent_refusal_expires_a_cached_failure():
    # A repo that was failing and is then deleted/renamed (404), or a revoked
    # token (401): the CI FAIL frame must not stay on the display forever.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    cache: dict = {}; health: dict = {}; overlay: dict = {}
    run_once(client, poller, CFG, NOW, cache, False, overlay_state=overlay, fetch_health=health)
    assert cache["o/r"].failing

    poller.fetch_runs.return_value = FETCH_DENIED
    first_failure = NOW + timedelta(minutes=2)
    run_once(client, poller, CFG, first_failure, cache, False,
             overlay_state=overlay, fetch_health=health)
    just_before = first_failure + STALE - timedelta(seconds=1)
    summary = run_once(client, poller, CFG, just_before, cache, False,
                       overlay_state=overlay, fetch_health=health)
    assert cache["o/r"].failing and "CI FAIL" in summary     # a blip keeps the frame
    poller.forget_repo.assert_not_called()

    client.reset_mock()
    summary = run_once(client, poller, CFG, first_failure + STALE, cache, False,
                       overlay_state=overlay, fetch_health=health)
    assert "o/r" not in cache
    # The ETag goes too: a later 304 must not vouch for state we dropped.
    poller.forget_repo.assert_called_once_with("o/r", running=False)
    assert client.draw.call_args.kwargs["led_notification_color"] == "#00000000"
    assert "led off" in summary


def test_not_modified_never_expires_cached_state():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    cache: dict = {}; health: dict = {}
    run_once(client, poller, CFG, NOW, cache, False, fetch_health=health)
    poller.fetch_runs.return_value = None                     # 304, for hours
    for minutes in (2, 30, 600):
        run_once(client, poller, CFG, NOW + timedelta(minutes=minutes), cache, False,
                 fetch_health=health)
    assert cache["o/r"].failing
    poller.forget_repo.assert_not_called()


def test_a_good_answer_resets_the_failure_clock():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    cache: dict = {}; health: dict = {}
    run_once(client, poller, CFG, NOW, cache, False, fetch_health=health)
    step = STALE - timedelta(seconds=1)
    for i, answer in enumerate((FETCH_DENIED, None, FETCH_DENIED), start=1):
        poller.fetch_runs.return_value = answer
        run_once(client, poller, CFG, NOW + step * i, cache, False, fetch_health=health)
    assert cache["o/r"].failing   # never a full STALE of consecutive failures


def test_persistent_running_poll_failure_expires_the_running_badge():
    # Otherwise the badge's timer grows forever and next_poll_seconds stays
    # on the fast cadence against a repo that no longer answers.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    running: dict = {}; health: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, False, running_cache=running,
             fetch_health=health)
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running) == 20

    poller.fetch_runs.return_value = FETCH_DENIED
    poller.fetch_running_runs.return_value = FETCH_DENIED
    run_once(client, poller, CFG_RUNNING, NOW + timedelta(minutes=1), {}, False,
             running_cache=running, fetch_health=health)
    assert running["o/r"]                                      # a blip keeps the badge
    run_once(client, poller, CFG_RUNNING, NOW + timedelta(minutes=1) + STALE, {}, False,
             running_cache=running, fetch_health=health)
    assert not running.get("o/r")
    assert next_poll_seconds(CFG_RUNNING["ci_status"], running) == 120


def test_fetch_health_pruned_with_the_repo():
    client = Mock()
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    health = {("runs", "o/gone"): NOW, ("running", "o/gone"): NOW}
    run_once(client, poller, CFG, NOW, {}, False, fetch_health=health)
    assert health == {}


def test_no_all_clear_while_a_watched_repo_is_unknown():
    # "ALL CLEAR" is a claim about every watched repo. With no answer from
    # GitHub (at startup, or after cached state expired) it would be a false
    # success, so the green frame waits until every repo has been read.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = FETCH_FAILED
    cfg = {"ci_status": {**CFG["ci_status"], "show_green": True}}
    cache: dict = {}
    summary = run_once(client, poller, cfg, NOW, cache, False, overlay_state={})
    client.draw.assert_not_called()
    assert "nothing to show" in summary

    poller.fetch_runs.return_value = [_run("success")]
    run_once(client, poller, cfg, NOW, cache, False, overlay_state={})
    assert "ALL CLEAR" in _drawn_texts(client)


# --- stuck-queue ageing does not depend on GitHub sending a new body -------------

def _queued(created_min_ago: int) -> dict:
    created = (NOW - timedelta(minutes=created_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"workflow_id": 1, "name": "tests", "status": "queued", "conclusion": None,
            "created_at": created, "head_branch": "main", "pull_requests": []}


def test_queued_run_becomes_stuck_while_github_answers_304():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_queued(created_min_ago=5)]
    cfg = {"ci_status": {**CFG["ci_status"], "stale_queued_minutes": 15}}
    cache: dict = {}
    run_once(client, poller, cfg, NOW, cache, False, overlay_state={})
    assert cache["o/r"].stuck == []

    poller.fetch_runs.return_value = None      # a queued run that never changes: 304
    summary = run_once(client, poller, cfg, NOW + timedelta(minutes=10), cache, False,
                       overlay_state={})
    assert [f.workflow for f in cache["o/r"].stuck] == ["tests"]
    assert "CI WAIT" in summary


def test_queued_run_keeps_ageing_through_a_failed_fetch():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_queued(created_min_ago=5)]
    cfg = {"ci_status": {**CFG["ci_status"], "stale_queued_minutes": 15}}
    cache: dict = {}
    run_once(client, poller, cfg, NOW, cache, False)
    poller.fetch_runs.return_value = FETCH_FAILED
    run_once(client, poller, cfg, NOW + timedelta(minutes=10), cache, False)
    assert [f.workflow for f in cache["o/r"].stuck] == ["tests"]


def test_hand_built_state_without_runs_survives_a_304():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = None
    cache = {"o/r": RepoState("o/r", [FailingRun("tests", "")], [])}
    run_once(client, poller, CFG, NOW, cache, False)
    assert cache["o/r"].failing == [FailingRun("tests", "")]


# --- malformed run data skips the repo for one poll, never the process -----------

def test_unusable_run_data_skips_the_repo_and_keeps_previous_state():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    cfg = {"ci_status": {**CFG["ci_status"], "stale_queued_minutes": 15}}
    cache: dict = {}
    run_once(client, poller, cfg, NOW, cache, False)
    for bad in ([{"name": "no workflow id"}],
                [{**_queued(5), "created_at": "not a timestamp"}],
                [{**_queued(5), "created_at": None}],
                ["not a run"]):
        poller.reset_mock()
        poller.fetch_runs.return_value = bad
        summary = run_once(client, poller, cfg, NOW + timedelta(minutes=1), cache, False)
        assert cache["o/r"].failing and "CI FAIL" in summary
        poller.warn.assert_called_once()
        assert "skipping o/r this poll" in poller.warn.call_args.args[1]
        # The body's ETag was already committed by the poller; forget it so
        # the next poll re-reads the body instead of getting a 304 for it.
        poller.forget_repo.assert_called_once_with("o/r", running=False)


def test_unusable_run_data_never_expires_cached_state():
    # An unreadable body is "could not read", not "the repo is gone".
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    cache: dict = {}; health: dict = {}
    run_once(client, poller, CFG, NOW, cache, False, fetch_health=health)
    poller.fetch_runs.return_value = [{"name": "no workflow id"}]
    for hours in (1, 2, 3):
        run_once(client, poller, CFG, NOW + timedelta(hours=hours), cache, False,
                 fetch_health=health)
    assert cache["o/r"].failing


def test_unusable_running_data_skips_the_repo_and_keeps_previous_runs():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    running: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, False, running_cache=running)
    for bad in ([{**_running_run(), "run_started_at": None}],
                [{"status": "in_progress", "run_started_at": "2026-08-03T13:30:00Z"}],
                ["not a run"]):
        poller.reset_mock()
        poller.fetch_running_runs.return_value = bad
        run_once(client, poller, CFG_RUNNING, NOW + timedelta(minutes=1), {}, False,
                 running_cache=running)
        assert running["o/r"] == [_running_run()]
        poller.warn.assert_called_once()
        # Only the running slot: the runs ETag was just validated by fetch_runs.
        poller.forget_repo.assert_called_once_with("o/r", runs=False)


# --- log-noise control (the other two agents' change-or-heartbeat gate) ----------

def test_should_log_info_true_when_summary_changes():
    assert should_log_info("'CI FAIL o/r' -> drawn", {"nothing to show; cleared"}, 0) is True

def test_should_log_info_false_when_unchanged_and_no_heartbeat_due():
    assert should_log_info("nothing to show; cleared", {"nothing to show; cleared"}, 1) is False

def test_should_log_info_true_when_unchanged_past_heartbeat():
    assert should_log_info("nothing to show; cleared", {"nothing to show; cleared"}, 600) is True

def test_should_log_info_true_on_first_summary():
    assert should_log_info("nothing to show; cleared", set(), 0) is True


def test_should_log_info_false_for_any_summary_already_logged_this_window():
    # Cards alternate every 20 s; each distinct line is logged once per
    # heartbeat window, not once per appearance.
    logged = {"'CI FAIL r main tests' -> drawn", "'CI FAIL r main lint' -> drawn"}
    assert should_log_info("'CI FAIL r main tests' -> drawn", logged, 40) is False


def test_main_logs_an_unchanged_summary_once_not_every_poll(monkeypatch, caplog):
    import logging
    import ci_status.main as ci_main

    class Stop(Exception):
        pass

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 5:
            raise Stop

    monkeypatch.setattr("sys.argv", ["ci_status", "--dry-run"])
    monkeypatch.setattr(ci_main, "load_config", lambda: {
        "ci_status": {**CFG_RUNNING["ci_status"]}, "device": {}})
    monkeypatch.setattr(ci_main, "device_kwargs", lambda cfg: {})
    monkeypatch.setattr(ci_main, "BusyBarClient", lambda **kwargs: Mock())
    monkeypatch.setattr(ci_main, "get_token", lambda: "tok")
    monkeypatch.setattr(ci_main, "run_once", lambda *a, **k: "nothing to show; cleared")
    monkeypatch.setattr(ci_main.time, "sleep", fake_sleep)
    monkeypatch.setattr(ci_main.logging, "basicConfig", lambda **kwargs: None)
    with caplog.at_level(logging.DEBUG, logger="ci_status"):
        try:
            ci_main.main()
        except Stop:
            pass
    info = [r for r in caplog.records if r.levelname == "INFO"]
    debug = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(info) == 1 and len(debug) == 4


# --- shared-owner labels reach the drawn cards --------------------------------

def test_cards_omit_an_owner_shared_by_all_watched_repos():
    cfg = {"ci_status": {**CFG_RUNNING["ci_status"], "repos": ["acme/widgets", "acme/gears"]}}
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.side_effect = lambda repo: [_run("failure")] if repo == "acme/widgets" else []
    poller.fetch_running_runs.return_value = []
    summary = run_once(client, poller, cfg, NOW, {}, dry_run=False, running_cache={}, overlay_state={})
    body = [t for t in _drawn_texts(client) if t != "CI FAIL"]
    assert body == ["widgets tests"]
    assert "acme/" not in summary

    # A run in progress on the other repo: its title drops the owner too.
    client.draw.reset_mock()
    poller.fetch_runs.side_effect = lambda repo: []
    poller.fetch_running_runs.side_effect = lambda repo: (
        [{"workflow_id": 7, "name": "build", "status": "in_progress",
          "run_started_at": "2026-08-03T13:35:00Z", "pull_requests": [{"number": 9}]}]
        if repo == "acme/gears" else [])
    poller.fetch_median_eta.return_value = 4.0
    run_once(client, poller, cfg, NOW, {}, dry_run=False, running_cache={}, overlay_state={})
    assert "GEARS #9 BUILD" in _drawn_texts(client)
    poller.fetch_median_eta.assert_called_once_with("acme/gears", 7)   # API calls keep the full name


def test_cards_keep_owners_when_watched_repos_have_different_owners():
    cfg = {"ci_status": {**CFG["ci_status"], "repos": ["acme/widgets", "globex/gears"]}}
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.side_effect = lambda repo: [_run("failure")] if repo == "acme/widgets" else []
    run_once(client, poller, cfg, NOW, {}, dry_run=False)
    assert "acme/widgets tests" in _drawn_texts(client)



# --- "could not ask" never expires cached state (review of PR #25) ---------------

def test_rate_limit_pause_with_a_real_poller_keeps_the_failure_and_its_led():
    # GitHub's primary limit can reset an hour out, longer than both the
    # pause cap and STALE_STATE_SECONDS. While we cannot ask, a real CI FAIL
    # card and its LED must stay; the next real answer decides.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = RestPoller("tok")
    cache: dict = {}; health: dict = {}; overlay: dict = {}
    failing = Mock(status_code=200, headers={"ETag": 'W/"f"'})
    failing.json.return_value = {"workflow_runs": [_run("failure")]}
    limited = Mock(status_code=403, headers={"X-RateLimit-Remaining": "0",
                                             "X-RateLimit-Reset": str(int(NOW.timestamp()) + 3600)})
    clock = {"mono": 0.0}
    with patch("ci_status.github.requests.get") as get, \
            patch("ci_status.github.time") as gtime:
        gtime.monotonic.side_effect = lambda: clock["mono"]
        gtime.time.return_value = NOW.timestamp()
        get.return_value = failing
        run_once(client, poller, CFG, NOW, cache, False, overlay_state=overlay,
                 fetch_health=health)
        get.return_value = limited
        for minutes in range(2, 62, 2):          # an hour of polls, limited or paused
            clock["mono"] = minutes * 60.0
            summary = run_once(client, poller, CFG, NOW + timedelta(minutes=minutes), cache,
                               False, overlay_state=overlay, fetch_health=health)
        assert get.call_count < 10               # paused: probes, not one request per poll
    assert cache["o/r"].failing
    assert health == {}
    assert overlay["led_was_on"] is True
    assert "led off" not in summary


def test_network_outage_keeps_cached_failure_and_running_badge():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    cache: dict = {}; running: dict = {}; health: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, cache, False, running_cache=running,
             fetch_health=health)
    poller.fetch_runs.return_value = FETCH_FAILED
    poller.fetch_running_runs.return_value = FETCH_FAILED
    for minutes in (5, 30, 90):
        run_once(client, poller, CFG_RUNNING, NOW + timedelta(minutes=minutes), cache, False,
                 running_cache=running, fetch_health=health)
    assert cache["o/r"].failing and running["o/r"]
    poller.forget_repo.assert_not_called()


def test_could_not_ask_neither_resets_nor_starts_the_refusal_clock():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    cache: dict = {}; health: dict = {}
    run_once(client, poller, CFG, NOW, cache, False, fetch_health=health)
    poller.fetch_runs.return_value = FETCH_FAILED
    run_once(client, poller, CFG, NOW + timedelta(minutes=1), cache, False, fetch_health=health)
    assert health == {}                                         # did not start it
    poller.fetch_runs.return_value = FETCH_DENIED
    run_once(client, poller, CFG, NOW + timedelta(minutes=2), cache, False, fetch_health=health)
    poller.fetch_runs.return_value = FETCH_FAILED
    run_once(client, poller, CFG, NOW + timedelta(minutes=10), cache, False, fetch_health=health)
    poller.fetch_runs.return_value = FETCH_DENIED
    run_once(client, poller, CFG, NOW + timedelta(minutes=2) + STALE, cache, False,
             fetch_health=health)
    assert "o/r" not in cache                                   # and did not reset it


def test_expiry_writes_one_warning_for_every_repo_dropped_in_a_poll(caplog):
    import logging
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    cfg = {"ci_status": {**CFG_RUNNING["ci_status"], "repos": ["o/a", "o/b", "o/c"]}}
    cache: dict = {}; running: dict = {}; health: dict = {}
    run_once(client, poller, cfg, NOW, cache, False, running_cache=running, fetch_health=health)
    poller.fetch_runs.return_value = FETCH_DENIED            # e.g. the token was revoked
    poller.fetch_running_runs.return_value = FETCH_DENIED
    run_once(client, poller, cfg, NOW + timedelta(minutes=1), cache, False,
             running_cache=running, fetch_health=health)
    with caplog.at_level(logging.WARNING, logger="ci_status"):
        for minutes in (1, 3, 5):                            # expiry, then two more polls
            run_once(client, poller, cfg, NOW + timedelta(minutes=minutes) + STALE, cache, False,
                     running_cache=running, fetch_health=health)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert all(repo in warnings[0] for repo in ("o/a", "o/b", "o/c"))


def test_unusable_run_data_warns_once_with_a_real_poller(caplog):
    import logging
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = RestPoller("tok")
    bad = Mock(status_code=200, headers={})
    bad.json.return_value = {"workflow_runs": [{"name": "no workflow id"}]}
    with patch("ci_status.github.requests.get", return_value=bad), \
            caplog.at_level(logging.WARNING, logger="ci_status"):
        for minutes in (0, 2, 4):
            run_once(client, poller, CFG, NOW + timedelta(minutes=minutes), {}, False)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "skipping o/r this poll" in warnings[0]


# --- default-branch failures stay visible until the default branch is green -----

def _branch_run(conclusion, branch="main", workflow_id=1, name="tests", pr=None,
                status="completed") -> dict:
    d = {"workflow_id": workflow_id, "name": name, "status": status, "conclusion": conclusion,
         "created_at": "2026-08-03T13:30:00Z", "head_branch": branch,
         "head_repository": {"full_name": "o/r"}}
    if pr is not None:
        d["pull_requests"] = [{"number": pr}]
    return d


def _branch_poller(all_runs, default_runs, branch="main") -> Mock:
    poller = Mock()
    poller.fetch_default_branch.return_value = branch
    poller.fetch_runs.return_value = all_runs
    poller.fetch_default_runs.return_value = default_runs
    return poller


MAIN_FAIL = FailingRun("tests", "main")


def test_default_branch_failure_shows_despite_a_newer_pr_success():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([_branch_run("success", branch="feature", pr=7)],
                            [_branch_run("failure")])
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False, branch_cache={})
    assert "CI FAIL" in summary
    assert any("main tests" in t for t in _drawn_texts(client))
    poller.fetch_default_runs.assert_called_once_with("o/r", "main")


def test_default_branch_failure_outlives_newer_runs_304s_and_outages_until_green():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([_branch_run("success", branch="feature")], [_branch_run("failure")])
    state_cache: dict = {}; branch_cache: dict = {}

    def poll(minutes):
        run_once(client, poller, CFG, NOW + timedelta(minutes=minutes), state_cache,
                 dry_run=False, branch_cache=branch_cache)
        return state_cache["o/r"].failing

    assert poll(0) == [MAIN_FAIL]
    # The failing run falls off both pages: 100 newer default-branch runs of
    # other workflows, and 10 newer runs on other branches.
    poller.fetch_default_runs.return_value = [
        _branch_run("success", workflow_id=100 + i, name=f"wf{i}") for i in range(100)]
    poller.fetch_runs.return_value = [
        _branch_run("success", branch="feature", workflow_id=200 + i) for i in range(10)]
    assert poll(2) == [MAIN_FAIL]
    poller.fetch_default_runs.return_value = None      # 304
    poller.fetch_runs.return_value = None
    assert poll(4) == [MAIN_FAIL]
    poller.fetch_default_runs.return_value = FETCH_FAILED
    assert poll(6) == [MAIN_FAIL]
    poller.fetch_default_runs.return_value = [_branch_run(None, status="in_progress")]  # re-run started
    assert poll(8) == [MAIN_FAIL]
    poller.fetch_default_runs.return_value = [_branch_run("success")]
    assert poll(10) == []


def test_pr_failure_keeps_showing_next_to_default_branch_tracking():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([_branch_run("failure", branch="feature", pr=7)],
                            [_branch_run("success")])
    state_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False, branch_cache={})
    assert state_cache["o/r"].failing == [FailingRun("tests", "#7")]


def test_unknown_default_branch_keeps_the_all_branch_rule_and_retries_slowly():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_default_branch.return_value = FETCH_FAILED
    poller.fetch_runs.return_value = [_branch_run("failure")]
    state_cache: dict = {}; branch_cache: dict = {}
    for seconds in (0, 120, 240):
        run_once(client, poller, CFG, NOW + timedelta(seconds=seconds), state_cache,
                 dry_run=False, branch_cache=branch_cache)
    assert state_cache["o/r"].failing == [MAIN_FAIL]        # judged as before this feature
    poller.fetch_default_runs.assert_not_called()
    assert poller.fetch_default_branch.call_count == 1      # not once per poll
    assert any("default branch" in call.args[1] for call in poller.warn.call_args_list)

    poller.fetch_default_branch.return_value = "main"
    poller.fetch_default_runs.return_value = []
    run_once(client, poller, CFG, NOW + timedelta(seconds=DEFAULT_BRANCH_RETRY_SECONDS),
             state_cache, dry_run=False, branch_cache=branch_cache)
    assert poller.fetch_default_branch.call_count == 2
    poller.fetch_default_runs.assert_called_once_with("o/r", "main")


def test_known_default_branch_is_looked_up_once_per_refresh_interval():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([], [])
    branch_cache: dict = {}
    for minutes in range(0, 60, 2):
        run_once(client, poller, CFG, NOW + timedelta(minutes=minutes), {}, dry_run=False,
                 branch_cache=branch_cache)
    assert poller.fetch_default_branch.call_count == 1
    assert poller.fetch_default_runs.call_count == 30       # one conditional request per poll
    # A failed re-check keeps the name already learned.
    poller.fetch_default_branch.return_value = FETCH_FAILED
    run_once(client, poller, CFG, NOW + timedelta(minutes=60), {}, dry_run=False,
             branch_cache=branch_cache)
    assert poller.fetch_default_branch.call_count == 2
    assert poller.fetch_default_runs.call_args.args == ("o/r", "main")


def test_account_enumeration_supplies_the_default_branch_without_a_lookup():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([], [])
    poller.fetch_account_repos.return_value = [{**_account_repo("o/r"), "default_branch": "trunk"}]
    cfg = {"ci_status": {**CFG["ci_status"], "repos": [], "watch_account_repos": True}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False, repo_cache={}, branch_cache={})
    poller.fetch_default_branch.assert_not_called()
    poller.fetch_default_runs.assert_called_once_with("o/r", "trunk")


def test_no_default_branch_requests_without_a_branch_cache_or_account_list():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False)
    poller.fetch_default_branch.assert_not_called()
    poller.fetch_default_runs.assert_not_called()


def test_default_branch_page_read_before_any_state_exists_is_read_again():
    # Its verdicts have nowhere to live yet, so the ETag must not turn the
    # next poll into a 304 for a page that was never used.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller(FETCH_FAILED, [_branch_run("failure")])
    state_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False, branch_cache={})
    assert "o/r" not in state_cache
    poller.forget_default_runs.assert_called_with("o/r")


def test_unreadable_default_branch_page_keeps_the_verdicts_and_is_read_again():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([], [_branch_run("failure")])
    state_cache: dict = {}; branch_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False, branch_cache=branch_cache)
    poller.fetch_default_runs.return_value = ["not a run"]
    run_once(client, poller, CFG, NOW + timedelta(minutes=2), state_cache, dry_run=False,
             branch_cache=branch_cache)
    assert state_cache["o/r"].failing == [MAIN_FAIL]
    poller.forget_default_runs.assert_called_with("o/r")
    assert any("o/r" in call.args[1] for call in poller.warn.call_args_list)


def test_branch_cache_pruned_with_the_repo():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([], [])
    branch_cache: dict = {}
    run_once(client, poller, CFG, NOW, {}, dry_run=False, repo_cache={}, branch_cache=branch_cache)
    assert "o/r" in branch_cache
    cfg = {"ci_status": {**CFG["ci_status"], "repos": ["o/other"]}}
    run_once(client, poller, cfg, NOW, {}, dry_run=False, repo_cache={}, branch_cache=branch_cache)
    assert "o/r" not in branch_cache


# --- card rotation is independent of the GitHub poll cadence --------------------

def test_display_tick_rotates_from_the_last_poll_without_asking_github():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    state_cache: dict = {}; overlay_state: dict = {}; view_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False,
             overlay_state=overlay_state, view_cache=view_cache)
    poller.reset_mock(); client.reset_mock()
    drawn_at = []
    for second in range(1, 121):
        run_once(client, poller, CFG, NOW + timedelta(seconds=second), state_cache,
                 dry_run=False, overlay_state=overlay_state, view_cache=view_cache, poll=False)
        if client.draw.called:
            drawn_at.append(second)
        else:
            assert client.mock_calls == []      # the silent half: no draw, no clear
        client.reset_mock()
    # 10 s on, 10 s off, for the whole 120 s between two idle polls.
    assert drawn_at == [20, 40, 60, 80, 100, 120]
    assert poller.mock_calls == []


def test_display_tick_rotates_the_running_badge_with_a_live_eta():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run(started_min_ago=3)]
    poller.fetch_median_eta.return_value = 10.0
    state_cache: dict = {}; overlay_state: dict = {}; view_cache: dict = {}
    kwargs = dict(dry_run=False, running_cache={}, overlay_state=overlay_state,
                  view_cache=view_cache)
    run_once(client, poller, CFG_RUNNING, NOW, state_cache, **kwargs)
    first = _drawn_texts(client)
    poller.reset_mock()
    run_once(client, poller, CFG_RUNNING, NOW + timedelta(minutes=2), state_cache,
             poll=False, **kwargs)
    assert _drawn_texts(client) != first        # the countdown moved on without a poll
    assert poller.mock_calls == []


def test_display_tick_with_nothing_cached_polls():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    run_once(client, poller, CFG, NOW, {}, dry_run=False, view_cache={}, poll=False)
    poller.fetch_runs.assert_called_once()


def test_display_tick_with_nothing_to_show_leaves_the_device_alone():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("success")]
    state_cache: dict = {}; overlay_state: dict = {}; view_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False,
             overlay_state=overlay_state, view_cache=view_cache)
    client.clear.assert_called_once()           # on the poll, as before
    client.reset_mock()
    for second in range(1, 30):
        run_once(client, poller, CFG, NOW + timedelta(seconds=second), state_cache,
                 dry_run=False, overlay_state=overlay_state, view_cache=view_cache, poll=False)
    assert client.mock_calls == []


def test_refused_draw_is_retried_once_per_slot_not_once_per_tick():
    # While the calendar holds a higher tier every draw is refused. A 1 s
    # tick must not turn that into one device request per second.
    client = Mock(); client.draw.return_value = DrawResult.REJECTED
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    state_cache: dict = {}; overlay_state: dict = {}; view_cache: dict = {}
    for second in range(0, 61):
        run_once(client, poller, CFG, NOW + timedelta(seconds=second), state_cache,
                 dry_run=False, overlay_state=overlay_state, view_cache=view_cache,
                 poll=second == 0)
    assert client.draw.call_count == 4          # t = 0, 20, 40, 60
    assert "frame_index" not in overlay_state and "led_was_on" not in overlay_state
    client.draw.return_value = DrawResult.DRAWN
    run_once(client, poller, CFG, NOW + timedelta(seconds=80), state_cache, dry_run=False,
             overlay_state=overlay_state, view_cache=view_cache, poll=False)
    assert overlay_state["frame_index"] == 1 and overlay_state["led_was_on"] is True


def _simulate_main(monkeypatch, cfg_ci, poller, client, seconds, draw_result=DrawResult.DRAWN,
                   poll_duration=0.0):
    """Run the real main() loop against a fake clock for `seconds`; returns
    (times of draw attempts, sleeps requested). Each poll takes
    `poll_duration` seconds of fake time."""
    import ci_status.main as ci_main

    class Stop(Exception):
        pass

    clock = {"t": 0.0}
    draws: list[float] = []
    sleeps: list[float] = []

    def fake_sleep(duration):
        sleeps.append(duration)
        clock["t"] += duration
        if clock["t"] >= seconds:
            raise Stop

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW + timedelta(seconds=clock["t"])

    def draw(*args, **kwargs):
        draws.append(clock["t"])
        return draw_result

    client.draw.side_effect = draw
    runs = poller.fetch_runs.return_value

    def slow_fetch_runs(repo):
        clock["t"] += poll_duration
        return runs

    poller.fetch_runs.side_effect = slow_fetch_runs
    monkeypatch.setattr("sys.argv", ["ci_status"])
    monkeypatch.setattr(ci_main, "load_config", lambda: {"ci_status": cfg_ci, "device": {}})
    monkeypatch.setattr(ci_main, "device_kwargs", lambda cfg: {})
    monkeypatch.setattr(ci_main, "BusyBarClient", lambda **kwargs: client)
    monkeypatch.setattr(ci_main, "get_token", lambda: "tok")
    monkeypatch.setattr(ci_main, "RestPoller", lambda token: poller)
    monkeypatch.setattr(ci_main, "datetime", FakeDatetime)
    monkeypatch.setattr(ci_main.time, "sleep", fake_sleep)
    monkeypatch.setattr(ci_main.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ci_main.logging, "basicConfig", lambda **kwargs: None)
    try:
        ci_main.main()
    except Stop:
        pass
    return draws, sleeps


def _main_poller(running: bool) -> Mock:
    poller = Mock()
    poller.fetch_default_branch.return_value = FETCH_FAILED
    poller.fetch_runs.return_value = [_run("failure")]
    poller.fetch_running_runs.return_value = [_running_run()] if running else []
    poller.fetch_median_eta.return_value = None
    return poller


def test_main_rotates_every_20s_while_polling_github_only_every_poll_seconds(monkeypatch):
    poller = _main_poller(running=False)
    draws, sleeps = _simulate_main(monkeypatch, CFG_RUNNING["ci_status"], poller, Mock(), 600)
    assert draws == [float(t) for t in range(0, 600, 20)]           # 10 s on / 10 s off
    assert set(sleeps) == {DISPLAY_TICK_SECONDS}
    # GitHub request volume is what poll_seconds = 120 always meant.
    assert poller.fetch_runs.call_count == 600 // 120
    assert poller.fetch_running_runs.call_count == 600 // 120
    assert poller.fetch_default_branch.call_count <= 600 // DEFAULT_BRANCH_RETRY_SECONDS


def test_main_rotation_and_request_volume_at_the_running_cadence(monkeypatch):
    poller = _main_poller(running=True)
    draws, _ = _simulate_main(monkeypatch, CFG_RUNNING["ci_status"], poller, Mock(), 600)
    assert draws == [float(t) for t in range(0, 600, 20)]
    assert poller.fetch_runs.call_count == 600 // 20                # running_poll_seconds
    assert poller.fetch_running_runs.call_count == 600 // 20


def test_main_keeps_the_backoff_while_the_device_is_unreachable(monkeypatch):
    poller = _main_poller(running=False)
    draws, sleeps = _simulate_main(monkeypatch, CFG_RUNNING["ci_status"], poller, Mock(), 900,
                                   draw_result=DrawResult.UNREACHABLE)
    assert sleeps[:7] == [5, 10, 20, 40, 80, 160, 300]
    assert len(draws) == len(sleeps)                                # one attempt per backoff step
    assert poller.fetch_runs.call_count <= 900 // 120 + 1           # still one poll per poll_seconds


def test_main_logs_each_rotating_card_once_per_heartbeat_window(monkeypatch, caplog):
    import logging
    poller = _main_poller(running=False)
    poller.fetch_runs.return_value = [_run("failure"),
                                      {**_run("failure"), "workflow_id": 2, "name": "lint"}]
    with caplog.at_level(logging.DEBUG, logger="ci_status"):
        _simulate_main(monkeypatch, CFG_RUNNING["ci_status"], poller, Mock(), 500)
    info = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert len(info) == 3 and len(set(info)) == 3   # two cards and the silent gap, once each


def test_a_slow_poll_delays_the_card_but_keeps_its_silence(monkeypatch):
    # The dwell is timed from when the card is actually drawn, not from when
    # the poll that preceded it started.
    for duration in (3.0, 12.0, 25.0):
        poller = _main_poller(running=False)
        draws, _ = _simulate_main(monkeypatch, CFG_RUNNING["ci_status"], poller, Mock(), 400,
                                  poll_duration=duration)
        assert draws[0] == duration
        gaps = [b - a for a, b in zip(draws, draws[1:])]
        assert gaps and min(gaps) >= 2 * OVERLAY_DWELL_SECONDS, (duration, draws)


def test_new_default_branch_failure_shows_while_the_default_page_cannot_be_read():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([_branch_run("success")], [_branch_run("success")])
    state_cache: dict = {}; branch_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False, branch_cache=branch_cache)
    assert state_cache["o/r"].failing == []
    # The all-branches page shows a fresh default-branch failure; the
    # default-branch page fails in the same poll (a rate-limit pause began).
    poller.fetch_runs.return_value = [_branch_run("failure")]
    for result in (FETCH_FAILED, FETCH_DENIED, None):
        poller.fetch_default_runs.return_value = result
        run_once(client, poller, CFG, NOW + timedelta(minutes=2), state_cache, dry_run=False,
                 branch_cache=branch_cache)
        assert state_cache["o/r"].failing == [MAIN_FAIL], result
    # Once the all-branches page moves on, the failure is held by the verdicts.
    poller.fetch_runs.return_value = [_branch_run("success", branch="feature")]
    poller.fetch_default_runs.return_value = None
    run_once(client, poller, CFG, NOW + timedelta(minutes=4), state_cache, dry_run=False,
             branch_cache=branch_cache)
    assert state_cache["o/r"].failing == [MAIN_FAIL]


def test_a_newer_default_page_is_not_undone_by_an_older_all_branches_page():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([_branch_run("failure")], [_branch_run("failure")])
    state_cache: dict = {}; branch_cache: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False, branch_cache=branch_cache)
    poller.fetch_runs.return_value = None                       # 304: still shows the failure
    poller.fetch_default_runs.return_value = [_branch_run("success")]
    run_once(client, poller, CFG, NOW + timedelta(minutes=2), state_cache, dry_run=False,
             branch_cache=branch_cache)
    assert state_cache["o/r"].failing == []


def test_verdicts_survive_a_poll_in_which_the_default_branch_is_unknown():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([_branch_run("success", branch="feature")], [_branch_run("failure")])
    poller.fetch_default_branch.return_value = FETCH_FAILED
    owned = {**_account_repo("o/r"), "default_branch": "main"}
    poller.fetch_account_repos.return_value = [owned]
    cfg = {"ci_status": {**CFG["ci_status"], "watch_account_repos": True,
                         "repo_refresh_minutes": 1}}
    state_cache: dict = {}; branch_cache: dict = {}; repo_cache: dict = {}
    run_once(client, poller, cfg, NOW, state_cache, dry_run=False, repo_cache=repo_cache,
             branch_cache=branch_cache)
    assert state_cache["o/r"].failing == [MAIN_FAIL]
    # The enumeration stops listing the repo (it stays watched through
    # `repos`) and the lookup fails: the name learned earlier is kept.
    poller.fetch_account_repos.return_value = []
    poller.fetch_default_runs.return_value = None               # 304 for the page already read
    for minutes in (2, 4, 30):
        run_once(client, poller, cfg, NOW + timedelta(minutes=minutes), state_cache,
                 dry_run=False, repo_cache=repo_cache, branch_cache=branch_cache)
        assert state_cache["o/r"].failing == [MAIN_FAIL], minutes


def test_verdicts_are_carried_while_untracked_and_resume_when_the_branch_returns():
    from ci_status.logic import Verdict, evaluate_runs
    previous = {1: Verdict(11, MAIN_FAIL)}
    untracked = evaluate_runs("o/r", [], NOW, 0, default_branch=None, default_verdicts=previous)
    assert untracked.default_verdicts == previous


def test_unreadable_default_page_keeps_the_all_branches_etag():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = _branch_poller([], ["not a run"])
    run_once(client, poller, CFG, NOW, {}, dry_run=False, branch_cache={})
    poller.forget_default_runs.assert_called_with("o/r")
    assert all(call.kwargs.get("running", True) for call in poller.forget_repo.call_args_list)


def test_retrying_the_failed_default_branch_run_keeps_the_card_and_led():
    # The Codex review case through run_once: [11 failure, 10 success], then
    # [11 in_progress, 10 success] must keep the card and the LED.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    fail = {**_branch_run("failure"), "id": 11}
    older_ok = {**_branch_run("success"), "id": 10}
    poller = _branch_poller([fail, older_ok], [fail, older_ok])
    state_cache: dict = {}; branch_cache: dict = {}; overlay_state: dict = {}
    run_once(client, poller, CFG, NOW, state_cache, dry_run=False, branch_cache=branch_cache,
             overlay_state=overlay_state)
    retrying = {**_branch_run(None, status="in_progress"), "id": 11}
    poller.fetch_runs.return_value = [retrying, older_ok]
    poller.fetch_default_runs.return_value = [retrying, older_ok]
    run_once(client, poller, CFG, NOW + timedelta(minutes=2), state_cache, dry_run=False,
             branch_cache=branch_cache, overlay_state=overlay_state)
    assert state_cache["o/r"].failing == [MAIN_FAIL]
    assert client.draw.call_args.kwargs["led_notification_color"] == "#FF0000FF"
