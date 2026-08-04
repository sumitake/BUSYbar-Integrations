import sys
from pathlib import Path

try:
    import busybar  # noqa: F401
except ImportError:  # bare clone / broken editable install: use the repo's src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import argparse
import logging
import time
from datetime import datetime, timezone

from busybar.client import BusyBarClient
from busybar.config import load_config

from .logic import RepoState, RunningInfo, build_ci_payload, evaluate_runs, select_running_run

APP = "ci_status"
log = logging.getLogger(APP)


def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool,
             running_cache: dict[str, list[dict]] | None = None) -> str:
    """`running_cache`, when passed, is a caller-owned dict this function
    mutates in place with each repo's currently-running runs (mirroring
    `state_cache`'s existing pattern) -- `main()` inspects it afterward to
    decide whether to shorten its poll interval to `running_poll_seconds`
    (see `next_poll_seconds`). Omitting it (the default) skips running-job
    detection entirely, e.g. for callers that only care about failure/stuck
    status.
    """
    c = cfg["ci_status"]
    timeout_s = int(c["poll_seconds"] * 1.5)
    for repo in c["repos"]:
        runs = poller.fetch_runs(repo)
        if runs is not None:  # None = 304/no-change/error -> keep cached state
            state_cache[repo] = evaluate_runs(repo, runs, now,
                                              c["stale_queued_minutes"])

    running_info = None
    # running_cache check first: short-circuits before touching c["show_running"],
    # so callers/tests using an older, fully-spelled-out cfg dict that predates
    # this key (and never pass running_cache) don't KeyError.
    if running_cache is not None and c["show_running"]:
        for repo in c["repos"]:
            running_runs = poller.fetch_running_runs(repo)
            if running_runs is not None:  # None = 304/no-change/error -> keep cached
                running_cache[repo] = running_runs
        selected = select_running_run(running_cache)
        if selected is not None:
            run, repo, other_count = selected
            median = poller.fetch_median_eta(repo, run["workflow_id"])
            running_info = RunningInfo(run=run, repo=repo, other_count=other_count,
                                       median_minutes=median, now=now)

    payload = build_ci_payload(list(state_cache.values()), c["show_green"], timeout_s,
                               running=running_info)
    if dry_run:
        return f"DRY-RUN payload: {payload!r}"
    if payload is None:
        client.clear(APP)
        return "all green; cleared"
    result = client.draw(APP, payload["elements"], priority=payload["priority"],
                         led_notification_color=payload["led"])
    text = next(e["text"] for e in payload["elements"] if e["type"] == "text")
    return f"{text[:40]!r} -> {result.value}"


def next_poll_seconds(cfg_ci: dict, running_cache: dict[str, list[dict]]) -> int:
    """Cadence switch: `running_poll_seconds` while any configured repo has
    a currently-running run, `poll_seconds` otherwise. A pure function of
    `running_cache`'s post-`run_once` state so it's testable without
    mocking `time.sleep`.
    """
    any_running = any(running_cache.get(repo) for repo in cfg_ci["repos"])
    return cfg_ci["running_poll_seconds"] if any_running else cfg_ci["poll_seconds"]


def main() -> int:
    parser = argparse.ArgumentParser(description="BUSY Bar CI status")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()
    if not cfg["ci_status"]["repos"]:
        log.error("No repos configured. Copy config.example.toml to config.toml "
                  "and set [ci_status] repos.")
        return 1
    from .github import RestPoller, get_token
    try:
        poller = RestPoller(get_token())
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    client = BusyBarClient(host=cfg["device"]["host"])
    client.clear(APP)  # drop any stale elements from a previous process (type collisions 400)

    state_cache: dict[str, RepoState] = {}
    running_cache: dict[str, list[dict]] = {}
    backoff = 5
    while True:
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run, running_cache=running_cache)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith("unreachable"):  # device offline: back off, not full poll
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(next_poll_seconds(cfg["ci_status"], running_cache))


if __name__ == "__main__":
    raise SystemExit(main())
