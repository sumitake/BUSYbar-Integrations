from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from ci_status.logic import RepoState, evaluate_runs, build_ci_payload

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)


def run(workflow_id: int, name: str, status: str, conclusion: str | None,
        created_min_ago: int = 5) -> dict:
    created = (NOW - timedelta(minutes=created_min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"workflow_id": workflow_id, "name": name, "status": status,
            "conclusion": conclusion, "created_at": created}


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


def test_payload_red_on_failure():
    payload = build_ci_payload([RepoState("o/r", ["tests"], [])], False, 180)
    assert payload["priority"] == 60 and payload["led"] == "#FF0000FF"
    assert "o/r" in payload["elements"][0]["text"]
    assert payload["elements"][0]["color"] == "#FF0000FF"


def test_payload_yellow_on_stuck_only():
    payload = build_ci_payload([RepoState("o/r", [], ["tests"])], False, 180)
    assert payload["led"] is None and payload["elements"][0]["color"] == "#FFFF00FF"
    assert "stuck" in payload["elements"][0]["text"]
