from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from busybar.client import DrawResult
from busybar.display import PRIORITY_OVERLAY, OVERLAY_DWELL_SECONDS
from ci_status.main import run_once, next_poll_seconds

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


def test_draws_red_on_failure():
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock(); poller.fetch_runs.return_value = [_run("failure")]
    summary = run_once(client, poller, CFG, NOW, {}, dry_run=False)
    assert client.draw.call_args.kwargs["priority"] == 60
    assert client.draw.call_args.kwargs["led_notification_color"] == "#FF0000FF"
    assert "FAIL" in summary


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
    # Running badge draws first (shape {bg,title,track,track_fill,eta});
    # the next poll turns up a failure. The alert payload's shape
    # ({bg,ci}) differs, so the stale title/track/track_fill/eta ink from
    # the badge must be cleared before the alert draws -- not left to
    # linger until its own ~1.5x-poll timeout.
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
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    summary = run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")
    assert "FAIL" in summary

def test_alert_then_overlay_clears_stale_alert_shape():
    # Symmetric direction: an alert draws first (shape {bg,ci}); once it
    # resolves and a run is active, the running badge's shape ({bg,title,
    # track,track_fill,eta}) differs and must clear the alert's stale
    # elements first.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("failure")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    overlay_state: dict = {}
    run_once(client, poller, CFG_RUNNING, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()   # first-ever draw -- nothing to clear yet

    poller.fetch_runs.return_value = [_run("success")]   # alert resolves
    later = NOW + timedelta(seconds=1)
    run_once(client, poller, CFG_RUNNING, later, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state)
    client.clear.assert_called_once_with("ci_status")

def test_quiet_green_then_overlay_clears_stale_green_shape():
    # Quiet "CI ok" text (shape {ci}, no bg) draws first when show_green
    # is on and nothing is running; once a run starts, the badge's shape
    # differs (it has a bg + several more ids) and must clear first, or
    # the old green text -- drawn with a ~1.5x-poll timeout, e.g. 180s at
    # the default -- would linger behind/around the badge for minutes.
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = []   # nothing running yet
    overlay_state: dict = {}
    summary = run_once(client, poller, CFG_GREEN, NOW, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state)
    client.clear.assert_not_called()
    assert overlay_state["last_shape"] == frozenset({"ci"})

    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    run_once(client, poller, CFG_GREEN, NOW, {}, dry_run=False,
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
            seen.append("quota_gql" if by_id["title"]["text"] == "GITHUB GRAPHQL" else "quota_rest")
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
    client = Mock(); client.draw.return_value = DrawResult.DRAWN
    poller = Mock()
    poller.fetch_runs.return_value = [_run("success")]
    poller.fetch_running_runs.return_value = [_running_run()]
    poller.fetch_median_eta.return_value = None
    poller.fetch_rate_limit.return_value = None   # fetch fails
    overlay_state: dict = {}
    quota_cache: dict = {}
    run_once(client, poller, CFG_QUOTA, NOW, {}, dry_run=False,
            running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)   # ci_badge, fine
    later = NOW + timedelta(seconds=2 * OVERLAY_DWELL_SECONDS + 1)
    summary = run_once(client, poller, CFG_QUOTA, later, {}, dry_run=False,
                       running_cache={}, overlay_state=overlay_state, quota_cache=quota_cache)
    # quota_gql's turn, but no data -- must not crash, must not draw stale
    # data, and must advance so the next call doesn't wait a dwell. The
    # skip contract is "no draw, no clear": the previously-drawn ci_badge
    # is still within its own dwell timeout and must be left exactly as
    # it is, not evicted by an unnecessary clear() call.
    assert client.draw.call_count == 1   # only the earlier ci_badge draw
    assert client.clear.call_count == 0   # skip path never clears
    assert overlay_state["frame_index"] == 2   # advanced past quota_gql
    assert "no draw, no clear" in summary

def test_quota_stale_data_not_shown_after_5_minutes():
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
    # quota_gql's turn: cached data exists but is 6 minutes old -- must be
    # treated as unavailable, not shown, and (skip contract) not cleared.
    assert client.draw.call_count == 1   # only the ci_badge draw landed
    assert client.clear.call_count == 0


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
