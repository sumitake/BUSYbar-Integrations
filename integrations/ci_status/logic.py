from dataclasses import dataclass
from datetime import datetime, timezone

FAILING = {"failure", "timed_out", "startup_failure"}


@dataclass
class RepoState:
    repo: str
    failing: list[str]
    stuck: list[str]


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def evaluate_runs(repo: str, runs: list[dict], now: datetime,
                  stale_queued_minutes: int) -> RepoState:
    latest: dict[int, dict] = {}
    for r in runs:  # API returns newest first; keep first seen per workflow
        latest.setdefault(r["workflow_id"], r)
    failing, stuck = [], []
    for r in latest.values():
        if r.get("conclusion") in FAILING:
            failing.append(r["name"])
        elif r.get("status") == "queued" and stale_queued_minutes > 0:
            age_min = (now - _parse_ts(r["created_at"])).total_seconds() / 60
            if age_min >= stale_queued_minutes:
                stuck.append(r["name"])
    return RepoState(repo=repo, failing=sorted(failing), stuck=sorted(stuck))


def _element(text: str, color: str, timeout_s: int) -> dict:
    return {"id": "ci", "type": "text", "text": text, "font": "normal",
            "x": 0, "y": 4, "width": 72, "color": color,
            "scroll_rate": 2000, "scroll_start_delay": 1000,
            "scroll_repeat_delay": 2000, "timeout": timeout_s}


def build_ci_payload(states: list[RepoState], show_green: bool,
                     timeout_s: int) -> dict | None:
    failures = [(s.repo, name) for s in states for name in s.failing]
    stuck = [(s.repo, name) for s in states for name in s.stuck]
    if failures:
        text = "CI FAIL " + " ".join(f"{repo}:{name}" for repo, name in failures)
        return {"elements": [_element(text, "#FF0000FF", timeout_s)],
                "priority": 60, "led": "#FF0000FF"}
    if stuck:
        text = "CI stuck " + " ".join(f"{repo}:{name}" for repo, name in stuck)
        return {"elements": [_element(text, "#FFFF00FF", timeout_s)],
                "priority": 60, "led": None}
    if show_green:
        return {"elements": [_element("CI ok", "#00FF00FF", timeout_s)],
                "priority": 60, "led": None}
    return None
