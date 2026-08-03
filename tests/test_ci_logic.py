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


def _text_element(elements: list[dict]) -> dict:
    return next(e for e in elements if e["type"] == "text")


def _bg_element(elements: list[dict]) -> dict:
    return next(e for e in elements if e["type"] == "rectangle")


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
