from datetime import datetime
from busybar.client import DrawResult
from busybar.display import PRIORITY_FILLER
from integrations.nyan_filler.main import run_once, should_log_info
from integrations.nyan_filler.logic import FILLER_APP, ASSET_NAME

class FakeClient:
    def __init__(self, result=DrawResult.DRAWN, upload=True):
        self.result = result; self.draws = []; self.clears = 0; self.uploads = []
        # `upload` is either a constant bool (every upload_asset returns it) or
        # a list of bools consumed one-per-call, to script "fail then succeed"
        # (an exhausted list falls back to True).
        self._upload = list(upload) if isinstance(upload, (list, tuple)) else upload
    def draw(self, app, elements, priority=50, led_notification_color=None):
        self.draws.append((app, elements, priority)); return self.result
    def clear(self, app):
        self.clears += 1; return True
    def upload_asset(self, app, filename, data):
        self.uploads.append((app, filename, len(data)))
        if isinstance(self._upload, list):
            return self._upload.pop(0) if self._upload else True
        return self._upload

BASE = {"nyan_filler": {"enabled": True, "poll_seconds": 1, "quiet_hours": "00:00-07:00"}}
ACTIVE = datetime(2026, 8, 6, 12, 0)   # noon: not in the 00:00-07:00 quiet window
QUIET = datetime(2026, 8, 6, 3, 0)     # 3am: quiet

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


# --- self-healing asset upload (the fix): the .anim upload is a one-shot
# install step, but a startup attempt that lands while the device is
# transiently unreachable must not be abandoned for the life of the process --
# otherwise every subsequent draw references an asset that was never uploaded.
# run_once latches on upload success: it retries the upload on each active poll
# until it lands once, then never uploads again (no per-poll uploads in steady
# state).

def test_uploads_asset_once_before_first_active_draw():
    c = FakeClient(); st = {}
    run_once(c, BASE, ACTIVE, st)
    assert len(c.uploads) == 1
    app, filename, nbytes = c.uploads[0]
    assert app == FILLER_APP and filename == ASSET_NAME and nbytes > 0
    assert len(c.draws) == 1        # upload happened, then the draw

def test_no_reupload_in_steady_state():
    c = FakeClient(); st = {}
    for _ in range(5):              # five successful active polls, shared state
        run_once(c, BASE, ACTIVE, st)
    assert len(c.uploads) == 1      # uploaded once, never again
    assert len(c.draws) == 5

def test_upload_retried_each_poll_until_it_succeeds():
    # Device unreachable for the first two upload attempts, then reachable.
    c = FakeClient(upload=[False, False, True]); st = {}
    run_once(c, BASE, ACTIVE, st)   # attempt 1 -> False
    run_once(c, BASE, ACTIVE, st)   # attempt 2 -> False
    run_once(c, BASE, ACTIVE, st)   # attempt 3 -> True  (latches)
    run_once(c, BASE, ACTIVE, st)   # no further attempt
    assert len(c.uploads) == 3      # retried until success, then stopped
    assert st.get("asset_uploaded") is True

def test_dry_run_never_uploads():
    c = FakeClient(); st = {}
    summary = run_once(c, BASE, ACTIVE, st, dry_run=True)
    assert c.uploads == [] and c.draws == []   # no device writes at all
    assert "DRY-RUN" in summary

def test_quiet_hours_never_uploads():
    c = FakeClient(); st = {}
    run_once(c, BASE, QUIET, st)     # asset is only needed to draw; quiet -> no draw
    assert c.uploads == [] and c.draws == []

def test_missing_local_asset_warns_once_and_skips_upload(monkeypatch, caplog):
    import logging
    from pathlib import Path
    from integrations.nyan_filler import main as nyan_main
    monkeypatch.setattr(nyan_main, "ASSET_PATH", Path("/does/not/exist/nyan.anim"))
    c = FakeClient(); st = {}
    with caplog.at_level(logging.WARNING):
        run_once(c, BASE, ACTIVE, st)   # local build artifact absent
        run_once(c, BASE, ACTIVE, st)
    assert c.uploads == []                                  # nothing to upload
    assert sum("missing" in r.message for r in caplog.records) == 1  # warned once
    assert len(c.draws) == 2            # loop keeps running regardless


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
