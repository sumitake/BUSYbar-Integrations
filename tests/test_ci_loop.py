from datetime import datetime, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from busybar.client import DrawResult
from ci_status.main import run_once

NOW = datetime(2026, 8, 3, 13, 37, tzinfo=timezone.utc)
CFG = {"ci_status": {"poll_seconds": 120, "repos": ["o/r"],
                     "show_green": False, "stale_queued_minutes": 0}}


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
