from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import CalEvent
from calendar_countdown.main import run_once
from busybar.client import DrawResult

TZ = timezone.utc
NOW = datetime(2026, 8, 3, 13, 37, tzinfo=TZ)
CFG = {"calendar_countdown": {"poll_seconds": 60, "lookahead_hours": 12,
                              "warn_minutes": 5, "include_all_day": False,
                              "auto_busy": False, "calendars": []}}


def make_event(offset_min: int) -> CalEvent:
    start = NOW + timedelta(minutes=offset_min)
    return CalEvent("Standup", start, start + timedelta(minutes=30), False)


def test_draws_countdown_for_upcoming_event():
    client = Mock()
    client.draw.return_value = DrawResult.DRAWN
    summary = run_once(client, lambda hours: [make_event(23)], CFG, NOW, dry_run=False)
    client.draw.assert_called_once()
    kwargs = client.draw.call_args.kwargs
    assert kwargs["priority"] == 20
    assert "Standup in 23m" in kwargs["elements"][0]["text"]
    assert "drew" in summary


def test_clears_when_no_event():
    client = Mock()
    run_once(client, lambda hours: [], CFG, NOW, dry_run=False)
    client.clear.assert_called_once_with("calendar_countdown")
    client.draw.assert_not_called()


def test_dry_run_never_touches_device():
    client = Mock()
    summary = run_once(client, lambda hours: [make_event(3)], CFG, NOW, dry_run=True)
    client.draw.assert_not_called()
    client.clear.assert_not_called()
    assert "DRY-RUN" in summary
