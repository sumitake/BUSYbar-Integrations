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

    def fetch_runs(self, repo: str) -> list[dict] | None:
        url = f"{API}/repos/{repo}/actions/runs"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
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
