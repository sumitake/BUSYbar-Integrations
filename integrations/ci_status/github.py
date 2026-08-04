"""GitHub REST access. REST ONLY — GraphQL is prohibited in this repo
(see spec: operator GraphQL quota exhaustion). Conditional requests keep
steady-state REST quota usage near zero (304s don't count)."""
import logging
import subprocess

import requests

log = logging.getLogger("ci_status")
API = "https://api.github.com"


def get_token() -> str:
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True,
                              text=True, timeout=10)
    except FileNotFoundError:
        raise RuntimeError("GitHub CLI not found. Install gh, then run: gh auth login")
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"gh has no stored auth ({proc.stderr.strip()}). "
                           "Run: gh auth login")
    return proc.stdout.strip()


class RestPoller:
    def __init__(self, token: str):
        self._token = token
        self._etags: dict[str, str] = {}
        # Separate ETag slot for the running-runs poll: different URL query
        # (status=in_progress&per_page=5 vs the unfiltered per_page=10 used
        # by fetch_runs), so it gets its own conditional-request cache
        # rather than sharing (and corrupting) fetch_runs's ETag.
        self._running_etags: dict[str, str] = {}
        # ETA history is cached per workflow_id for the process's lifetime
        # (not per-poll): a workflow's recent successful-run durations don't
        # change fast enough to be worth re-fetching every poll, unlike the
        # running-runs check which must be fresh every cycle to track
        # elapsed time. Populated lazily by fetch_median_eta.
        self._eta_cache: dict[int, float | None] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def fetch_runs(self, repo: str) -> list[dict] | None:
        url = f"{API}/repos/{repo}/actions/runs"
        headers = self._headers()
        if repo in self._etags:
            headers["If-None-Match"] = self._etags[repo]
        try:
            resp = requests.get(url, headers=headers,
                                params={"per_page": 10}, timeout=(5, 15))
        except requests.RequestException as exc:
            log.debug("github unreachable: %s", exc)
            return None
        if resp.status_code == 304:
            return None
        if resp.status_code != 200:
            log.warning("github %s for %s", resp.status_code, repo)
            return None
        if "ETag" in resp.headers:
            self._etags[repo] = resp.headers["ETag"]
        try:
            return resp.json().get("workflow_runs", [])
        except ValueError as exc:
            log.warning("github returned non-JSON: %s", exc)
            return None

    def fetch_running_runs(self, repo: str) -> list[dict] | None:
        """GET .../actions/runs?status=in_progress&per_page=5 -- a distinct
        URL from fetch_runs, so it uses its own ETag slot (_running_etags,
        not _etags)."""
        url = f"{API}/repos/{repo}/actions/runs"
        headers = self._headers()
        if repo in self._running_etags:
            headers["If-None-Match"] = self._running_etags[repo]
        try:
            resp = requests.get(url, headers=headers,
                                params={"status": "in_progress", "per_page": 5},
                                timeout=(5, 15))
        except requests.RequestException as exc:
            log.debug("github unreachable (running poll): %s", exc)
            return None
        if resp.status_code == 304:
            return None
        if resp.status_code != 200:
            log.warning("github %s for %s (running poll)", resp.status_code, repo)
            return None
        if "ETag" in resp.headers:
            self._running_etags[repo] = resp.headers["ETag"]
        try:
            return resp.json().get("workflow_runs", [])
        except ValueError as exc:
            log.warning("github returned non-JSON (running poll): %s", exc)
            return None

    def fetch_median_eta(self, repo: str, workflow_id: int) -> float | None:
        """Median duration (minutes) of the last 5 successful runs of
        `workflow_id`, cached for the process's lifetime once successfully
        fetched (see the cache comment in __init__). Returns None on a
        confirmed empty history (a successful fetch with no matching runs
        -- this IS cached, it's a real answer) as well as on a fetch
        failure (NOT cached -- a transient network/HTTP error shouldn't
        permanently lock the workflow into "no history" for the rest of
        the process's life; the next encounter retries)."""
        if workflow_id in self._eta_cache:
            return self._eta_cache[workflow_id]
        url = f"{API}/repos/{repo}/actions/workflows/{workflow_id}/runs"
        try:
            resp = requests.get(url, headers=self._headers(),
                                params={"status": "success", "per_page": 5},
                                timeout=(5, 15))
        except requests.RequestException as exc:
            log.debug("github unreachable (median fetch): %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("github %s for %s workflow %s (median fetch)",
                       resp.status_code, repo, workflow_id)
            return None
        try:
            runs = resp.json().get("workflow_runs", [])
        except ValueError as exc:
            log.warning("github returned non-JSON (median fetch): %s", exc)
            return None
        from .logic import compute_median_duration_minutes
        median = compute_median_duration_minutes(runs)
        self._eta_cache[workflow_id] = median  # cache even if None -- see docstring
        return median
