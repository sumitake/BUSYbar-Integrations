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

from .logic import RepoState, build_ci_payload, evaluate_runs

APP = "ci_status"
log = logging.getLogger(APP)


def run_once(client, poller, cfg: dict, now: datetime,
             state_cache: dict[str, RepoState], dry_run: bool) -> str:
    c = cfg["ci_status"]
    timeout_s = int(c["poll_seconds"] * 1.5)
    for repo in c["repos"]:
        runs = poller.fetch_runs(repo)
        if runs is not None:  # None = 304/no-change/error -> keep cached state
            state_cache[repo] = evaluate_runs(repo, runs, now,
                                              c["stale_queued_minutes"])
    payload = build_ci_payload(list(state_cache.values()), c["show_green"], timeout_s)
    if dry_run:
        return f"DRY-RUN payload: {payload!r}"
    if payload is None:
        client.clear(APP)
        return "all green; cleared"
    result = client.draw(APP, payload["elements"], priority=payload["priority"],
                         led_notification_color=payload["led"])
    text = next(e["text"] for e in payload["elements"] if e["type"] == "text")
    return f"{text[:40]!r} -> {result.value}"


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
    backoff = 5
    while True:
        summary = run_once(client, poller, cfg, datetime.now(timezone.utc),
                           state_cache, args.dry_run)
        log.info(summary)
        if args.once:
            return 0
        if summary.endswith("unreachable"):  # device offline: back off, not full poll
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5
            time.sleep(cfg["ci_status"]["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
