import copy
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "device": {"host": "10.0.4.20"},
    "calendar_countdown": {
        # 10s matches busybar.display.AMBIENT_REDRAW_SECONDS -- the ambient
        # tier's redraw contract, tuned (after on-device re-measurement
        # showed a 15s poll only recovering the screen in 2 of 6 dwell
        # cycles) to match the overlay's 10s dwell gap exactly, so this
        # app's own redraws land inside those gaps far more often for
        # near-true alternation (v1.5; see the spec doc's v1.5 section for
        # both measurement rounds). Existing user configs that set
        # poll_seconds explicitly are unaffected -- this only changes the
        # out-of-the-box default.
        "poll_seconds": 10,
        "lookahead_hours": 12,
        "warn_minutes": 5,
        "notice_minutes": 15,
        "progress_window_minutes": 60,
        "include_all_day": False,
        "auto_busy": False,
        "calendars": [],
    },
    "ci_status": {
        "poll_seconds": 120,
        "repos": [],
        "show_green": False,
        "stale_queued_minutes": 0,  # 0 = disabled
        "show_running": True,
        "running_poll_seconds": 20,
        "show_quota": True,  # GraphQL/REST quota frames join the overlay
                            # rotation while a run is active (no effect if
                            # show_running is false -- see ci_status/main.py)
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def find_config() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "config.toml"
    return candidate if candidate.exists() else None


def load_config(path: Path | None = None) -> dict:
    if path is None:
        path = find_config()
    cfg = copy.deepcopy(DEFAULTS)
    if path is not None and Path(path).exists():
        with open(path, "rb") as fh:
            cfg = _merge(cfg, tomllib.load(fh))
    env_host = os.environ.get("BUSYBAR_HOST")
    if env_host:
        cfg = _merge(cfg, {"device": {"host": env_host}})
    return cfg
