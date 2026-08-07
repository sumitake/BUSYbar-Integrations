from datetime import datetime
from busybar.client import DrawResult
from busybar.display import PRIORITY_FILLER
from integrations.nyan_filler.main import run_once, should_log_info
from integrations.nyan_filler.logic import FILLER_APP, ASSET_NAME

class FakeClient:
    def __init__(self, result=DrawResult.DRAWN):
        self.result = result; self.draws = []; self.clears = 0
    def draw(self, app, elements, priority=50, led_notification_color=None):
        self.draws.append((app, elements, priority)); return self.result
    def clear(self, app):
        self.clears += 1; return True

BASE = {"nyan_filler": {"enabled": True, "poll_seconds": 1, "quiet_hours": "00:00-07:00"}}

def test_draws_at_filler_priority_when_active():
    c = FakeClient(); st = {}
    summary = run_once(c, BASE, datetime(2026, 8, 6, 12, 0), st)  # noon: not quiet
    assert len(c.draws) == 1
    app, elements, priority = c.draws[0]
    assert app == FILLER_APP and priority == PRIORITY_FILLER
    assert elements[0]["type"] == "animation" and elements[0]["path"] == ASSET_NAME
    assert elements[0]["loop"] is True
    assert "drawn" in summary

def test_quiet_hours_clears_once_then_stays_silent():
    c = FakeClient(); st = {}
    run_once(c, BASE, datetime(2026, 8, 6, 3, 0), st)   # 3am: quiet
    run_once(c, BASE, datetime(2026, 8, 6, 3, 1), st)   # still quiet
    assert c.clears == 1        # cleared once on entry, not every poll
    assert c.draws == []

def test_leaving_quiet_hours_draws_again():
    c = FakeClient(); st = {}
    run_once(c, BASE, datetime(2026, 8, 6, 3, 0), st)   # quiet -> clears
    run_once(c, BASE, datetime(2026, 8, 6, 8, 0), st)   # active -> draws
    assert c.clears == 1 and len(c.draws) == 1

def test_disabled_is_noop():
    c = FakeClient(); st = {}
    cfg = {"nyan_filler": {**BASE["nyan_filler"], "enabled": False}}
    summary = run_once(c, cfg, datetime(2026, 8, 6, 12, 0), st)
    assert c.draws == [] and "disabled" in summary


# --- log-noise control (I-1: default poll_seconds=1 would otherwise sixfold
# calendar_countdown's own worst case -- ~86,400 near-identical lines/day at
# INFO). should_log_info mirrors calendar_countdown.main.should_log_info.

def test_should_log_info_true_when_summary_changes():
    assert should_log_info("nyan @ 5 -> drawn", "nyan @ 5 -> reused", seconds_since_heartbeat=0) is True

def test_should_log_info_false_when_summary_unchanged_and_no_heartbeat_due():
    assert should_log_info("nyan @ 5 -> drawn", "nyan @ 5 -> drawn", seconds_since_heartbeat=1) is False

def test_should_log_info_true_when_unchanged_past_heartbeat():
    assert should_log_info("nyan @ 5 -> drawn", "nyan @ 5 -> drawn",
                           seconds_since_heartbeat=600, heartbeat_seconds=600) is True
    assert should_log_info("nyan @ 5 -> drawn", "nyan @ 5 -> drawn",
                           seconds_since_heartbeat=599, heartbeat_seconds=600) is False
