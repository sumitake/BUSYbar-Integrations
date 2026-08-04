from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from ci_status.logic import (
    RepoState, RunningInfo, evaluate_runs, build_ci_payload,
    _pr_or_branch, select_running_run, compute_median_duration_minutes,
    _format_eta_text, _progress_width, _build_running_title,
    RUNNING_PRIORITY, RUNNING_BADGE_TIMEOUT_S,
)

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)


def run(workflow_id: int, name: str, status: str, conclusion: str | None,
        created_min_ago: int = 5) -> dict:
    created = (NOW - timedelta(minutes=created_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"workflow_id": workflow_id, "name": name, "status": status,
            "conclusion": conclusion, "created_at": created}


def _text_element(elements: list[dict]) -> dict:
    return next(e for e in elements if e["type"] == "text")


def _bg_element(elements: list[dict]) -> dict:
    return next(e for e in elements if e["type"] == "rectangle")


def _by_id(elements: list[dict]) -> dict:
    return {e["id"]: e for e in elements}


def running_run(workflow_id: int = 1, name: str = "tests", pr_number: int | None = 42,
                head_branch: str = "main", started_min_ago: float = 3) -> dict:
    started = (NOW - timedelta(minutes=started_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "workflow_id": workflow_id, "name": name, "status": "in_progress",
        "run_started_at": started, "head_branch": head_branch,
        "pull_requests": [{"number": pr_number}] if pr_number is not None else [],
    }


def success_run(started: str, updated: str) -> dict:
    return {"run_started_at": started, "updated_at": updated}


def test_failure_detected_on_latest_run_only():
    runs = [run(1, "tests", "completed", "success"),          # newest for wf 1
            run(1, "tests", "completed", "failure", 60),      # older failure — ignore
            run(2, "lint", "completed", "failure")]
    state = evaluate_runs("o/r", runs, NOW, 0)
    assert state.failing == ["lint"] and state.stuck == []


def test_stuck_queued_detection_respects_threshold():
    runs = [run(1, "tests", "queued", None, created_min_ago=20)]
    assert evaluate_runs("o/r", runs, NOW, 15).stuck == ["tests"]
    assert evaluate_runs("o/r", runs, NOW, 0).stuck == []       # disabled
    assert evaluate_runs("o/r", runs, NOW, 30).stuck == []      # under threshold


def test_payload_none_when_green_and_quiet():
    assert build_ci_payload([RepoState("o/r", [], [])], False, 180) is None


def test_payload_shows_green_glyph_when_enabled():
    payload = build_ci_payload([RepoState("o/r", [], [])], True, 180)
    assert payload["priority"] == 60
    text_el = _text_element(payload["elements"])
    assert text_el["color"] == "#00FF00FF"
    # quiet green case has no full-panel background badge
    assert not any(e["type"] == "rectangle" for e in payload["elements"])


def test_payload_red_badge_on_failure():
    payload = build_ci_payload([RepoState("o/r", ["tests"], [])], False, 180)
    assert payload["priority"] == 60 and payload["led"] == "#FF0000FF"

    bg = _bg_element(payload["elements"])
    assert bg["x"] == 0 and bg["y"] == 0 and bg["width"] == 72 and bg["height"] == 16
    assert bg["radius"] == 2 and bg["fill"] == "solid"
    assert bg["fill_colors"] == ["#A32D2DFF"]
    # default 1px white border would outline the badge; must be disabled
    assert bg["border_width"] == 0

    text_el = _text_element(payload["elements"])
    assert "o/r" in text_el["text"] and "tests" in text_el["text"]
    assert text_el["color"] == "#FFFFFFFF"
    assert text_el["font"] == "bold"


def test_payload_amber_badge_on_stuck_only():
    payload = build_ci_payload([RepoState("o/r", [], ["tests"])], False, 180)
    assert payload["led"] is None

    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#BA7517FF"]

    text_el = _text_element(payload["elements"])
    assert "stuck" in text_el["text"]
    assert text_el["color"] == "#0B0B0BFF"
    assert text_el["font"] == "bold"


def test_failure_badge_takes_priority_over_stuck():
    payload = build_ci_payload([RepoState("o/r", ["tests"], ["lint"])], False, 180)
    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#A32D2DFF"]  # red failure badge wins


# --- PR number / branch fallback ----------------------------------------------

def test_pr_or_branch_uses_pr_number_when_present():
    assert _pr_or_branch({"pull_requests": [{"number": 42}], "head_branch": "feature-x"}) == "#42"

def test_pr_or_branch_falls_back_to_head_branch_when_no_pr():
    # Fork/push-triggered runs have an empty pull_requests array.
    assert _pr_or_branch({"pull_requests": [], "head_branch": "main"}) == "main"
    assert _pr_or_branch({"head_branch": "main"}) == "main"   # key absent entirely

def test_pr_or_branch_uses_first_pr_when_multiple():
    assert _pr_or_branch({"pull_requests": [{"number": 7}, {"number": 8}]}) == "#7"


# --- select_running_run: multi-repo, most-recent, +N --------------------------

def test_select_running_run_none_when_nothing_running():
    assert select_running_run({}) is None
    assert select_running_run({"o/r": []}) is None

def test_select_running_run_single_candidate_no_others():
    run_ = running_run(started_min_ago=5)
    result = select_running_run({"o/r": [run_]})
    assert result == (run_, "o/r", 0)

def test_select_running_run_picks_most_recently_started_across_repos():
    older = running_run(workflow_id=1, started_min_ago=10)
    newer = running_run(workflow_id=2, started_min_ago=2)
    result = select_running_run({"o/r1": [older], "o/r2": [newer]})
    assert result[0] is newer and result[1] == "o/r2"

def test_select_running_run_counts_others_across_all_repos():
    a = running_run(workflow_id=1, started_min_ago=1)   # most recent -> selected
    b = running_run(workflow_id=2, started_min_ago=5)
    c = running_run(workflow_id=3, started_min_ago=8)
    result = select_running_run({"o/r1": [a, b], "o/r2": [c]})
    assert result[0] is a and result[2] == 2   # +2 others

def test_select_running_run_ignores_non_in_progress_entries():
    stale = {**running_run(), "status": "completed"}
    live = running_run(started_min_ago=1)
    result = select_running_run({"o/r": [stale, live]})
    assert result[0] is live and result[2] == 0


# --- compute_median_duration_minutes -------------------------------------------

def test_median_duration_odd_count():
    runs = [success_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z"),   # 4 min
           success_run("2026-08-03T09:00:00Z", "2026-08-03T09:06:00Z"),   # 6 min
           success_run("2026-08-03T08:00:00Z", "2026-08-03T08:05:00Z")]  # 5 min
    assert compute_median_duration_minutes(runs) == 5.0

def test_median_duration_even_count_averages_middle_two():
    runs = [success_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z"),   # 4
           success_run("2026-08-03T09:00:00Z", "2026-08-03T09:06:00Z")]   # 6
    assert compute_median_duration_minutes(runs) == 5.0   # (4+6)/2

def test_median_duration_none_when_no_runs():
    assert compute_median_duration_minutes([]) is None

def test_median_duration_skips_runs_missing_timestamps():
    runs = [{"run_started_at": None, "updated_at": None},
           success_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z")]
    assert compute_median_duration_minutes(runs) == 4.0

def test_median_duration_caps_at_first_5():
    # 6 runs of varying duration; only the first 5 (per_page=5 upstream,
    # but this stays defensive) should count.
    runs = [success_run("2026-08-03T10:00:00Z", f"2026-08-03T10:{m:02d}:00Z")
           for m in (1, 2, 3, 4, 5, 99)]
    assert compute_median_duration_minutes(runs) == 3.0   # median of [1,2,3,4,5]


# --- ETA text formatting --------------------------------------------------------

def test_eta_text_with_history_uses_tilde_prefix():
    run_ = running_run(started_min_ago=10)
    assert _format_eta_text(run_, median_minutes=14, now=NOW) == "~4m"

def test_eta_text_reuses_format_countdown_for_hours():
    run_ = running_run(started_min_ago=5)
    assert _format_eta_text(run_, median_minutes=70, now=NOW) == "~1h05m"

def test_eta_text_shows_soon_when_floored_to_zero():
    run_ = running_run(started_min_ago=14)
    assert _format_eta_text(run_, median_minutes=14, now=NOW) == "soon"   # exactly at median
    run_over = running_run(started_min_ago=20)
    assert _format_eta_text(run_over, median_minutes=14, now=NOW) == "soon"   # overrun
    run_almost = running_run(started_min_ago=13.5)
    assert _format_eta_text(run_almost, median_minutes=14, now=NOW) == "soon"   # 0.5 min left

def test_eta_text_no_history_shows_elapsed_with_in_suffix():
    run_ = running_run(started_min_ago=3)
    assert _format_eta_text(run_, median_minutes=None, now=NOW) == "3m in"

def test_eta_text_no_history_reuses_format_countdown_for_hours():
    run_ = running_run(started_min_ago=65)
    assert _format_eta_text(run_, median_minutes=None, now=NOW) == "1h05m in"


# --- track progress width -------------------------------------------------------

def test_progress_width_full_when_median_unknown():
    assert _progress_width(elapsed_minutes=5, median_minutes=None) == 72

def test_progress_width_scales_with_elapsed_over_median():
    assert _progress_width(elapsed_minutes=7, median_minutes=14) == 36   # half -> half width

def test_progress_width_clamps_at_full_when_overrun():
    assert _progress_width(elapsed_minutes=20, median_minutes=14) == 72

def test_progress_width_clamped_min_one():
    assert _progress_width(elapsed_minutes=0, median_minutes=14) == 1
    assert _progress_width(elapsed_minutes=-1, median_minutes=14) == 1

def test_progress_width_full_when_median_non_positive():
    assert _progress_width(elapsed_minutes=5, median_minutes=0) == 72


# --- running badge title --------------------------------------------------------

def test_running_title_with_pr_number():
    run_ = running_run(name="tests", pr_number=42, head_branch="feature-x")
    assert _build_running_title(run_, "acme/widgets", 0) == "ACME/WIDGETS #42 TESTS"

def test_running_title_falls_back_to_branch():
    run_ = running_run(name="deploy", pr_number=None, head_branch="release-2.0")
    assert _build_running_title(run_, "acme/widgets", 0) == "ACME/WIDGETS RELEASE-2.0 DEPLOY"

def test_running_title_appends_plus_n_when_others_active():
    run_ = running_run(name="tests", pr_number=42)
    assert _build_running_title(run_, "acme/widgets", 3) == "ACME/WIDGETS #42 TESTS +3"

def test_running_title_no_suffix_when_alone():
    run_ = running_run(name="tests", pr_number=42)
    assert "+0" not in _build_running_title(run_, "acme/widgets", 0)


# --- build_ci_payload: running badge shape + precedence ------------------------

def test_payload_running_badge_shape():
    run_ = running_run(name="tests", pr_number=42, started_min_ago=3)
    info = RunningInfo(run=run_, repo="acme/widgets", other_count=0, median_minutes=14, now=NOW)
    payload = build_ci_payload([RepoState("o/r", [], [])], False, 180, running=info)

    assert payload["priority"] == RUNNING_PRIORITY == 21
    assert payload["led"] is None

    by_id = _by_id(payload["elements"])
    assert set(by_id) == {"bg", "title", "track", "track_fill", "eta"}
    assert [e["id"] for e in payload["elements"]] == ["bg", "title", "track", "track_fill", "eta"]

    bg = by_id["bg"]
    assert bg["fill"] == "gradient_v" and bg["border_width"] == 0
    assert bg["timeout"] == RUNNING_BADGE_TIMEOUT_S == 10   # fixed, NOT the caller's timeout_s=180

    title = by_id["title"]
    assert title["text"] == "ACME/WIDGETS #42 TESTS"
    assert title["font"] == "small" and title["y"] == -2

    track = by_id["track"]
    assert track["y"] == 6 and track["width"] == 72 and track["border_width"] == 0

    track_fill = by_id["track_fill"]
    assert track_fill["fill"] == "solid"   # spec: "solid cyan", no gradient
    assert track_fill["width"] == _progress_width(3, 14)

    eta = by_id["eta"]
    assert eta["font"] == "large" and eta["y"] == 5
    assert eta["text"] == _format_eta_text(run_, 14, NOW)

def test_payload_running_badge_title_scrolls_when_long():
    run_ = running_run(name="a-very-long-workflow-name-that-will-not-fit", pr_number=12345, started_min_ago=1)
    info = RunningInfo(run=run_, repo="acme/some-long-widgets-repo-name", other_count=0,
                       median_minutes=None, now=NOW)
    payload = build_ci_payload([], False, 180, running=info)
    title = _by_id(payload["elements"])["title"]
    assert title.get("scroll_rate") == 2000

def test_payload_failure_takes_priority_over_running():
    run_ = running_run()
    info = RunningInfo(run=run_, repo="o/r", other_count=0, median_minutes=None, now=NOW)
    payload = build_ci_payload([RepoState("o/r", ["tests"], [])], False, 180, running=info)
    assert payload["priority"] == 60   # failure wins, not the running badge
    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#A32D2DFF"]

def test_payload_stuck_takes_priority_over_running():
    run_ = running_run()
    info = RunningInfo(run=run_, repo="o/r", other_count=0, median_minutes=None, now=NOW)
    payload = build_ci_payload([RepoState("o/r", [], ["tests"])], False, 180, running=info)
    assert payload["priority"] == 60
    bg = _bg_element(payload["elements"])
    assert bg["fill_colors"] == ["#BA7517FF"]

def test_payload_running_takes_priority_over_quiet_green():
    run_ = running_run()
    info = RunningInfo(run=run_, repo="o/r", other_count=0, median_minutes=None, now=NOW)
    payload = build_ci_payload([RepoState("o/r", [], [])], True, 180, running=info)
    assert payload["priority"] == RUNNING_PRIORITY   # running beats show_green

def test_payload_no_running_falls_through_to_quiet_or_green_as_before():
    assert build_ci_payload([RepoState("o/r", [], [])], False, 180, running=None) is None
    payload = build_ci_payload([RepoState("o/r", [], [])], True, 180, running=None)
    assert payload["priority"] == 60
