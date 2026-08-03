import copy
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "device": {"host": "10.0.4.20"},
    "calendar_countdown": {
        "poll_seconds": 60,
        "lookahead_hours": 12,
        "warn_minutes": 5,
        "include_all_day": False,
        "auto_busy": False,
        "calendars": [],
    },
    "ci_status": {
        "poll_seconds": 120,
        "repos": [],
        "show_green": False,
        "stale_queued_minutes": 0,  # 0 = disabled
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
