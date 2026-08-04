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
        # Account-wide repo discovery (v1.5.1): only page 1 of
        # /user/repos gets a conditional-request slot -- see
        # fetch_account_repos's docstring for why pages 2+ deliberately
        # don't get one.
        self._account_repos_etag: str | None = None

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

    def fetch_rate_limit(self) -> dict | None:
        """GET /rate_limit -- explicitly EXEMPT from GitHub's own rate
        limiting (checking your quota doesn't spend it), so this is a
        plain fresh GET every call: no ETag/conditional-request caching
        attempted (there's no quota cost to save) and no per-process cache
        either (unlike fetch_median_eta's history, remaining quota changes
        continuously and a cached value would go stale within the poll
        interval). Returns the raw parsed response (`{"resources":
        {"core": {...}, "graphql": {...}, ...}, ...}`) for `logic.
        parse_rate_limit` to extract from -- unlike the other fetch_*
        methods, no `workflow_runs` unwrapping happens here since the
        caller needs the whole `resources` object, not one list.
        """
        try:
            resp = requests.get(f"{API}/rate_limit", headers=self._headers(), timeout=(5, 15))
        except requests.RequestException as exc:
            log.debug("github unreachable (rate_limit fetch): %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("github %s (rate_limit fetch)", resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError as exc:
            log.warning("github returned non-JSON (rate_limit fetch): %s", exc)
            return None

    def fetch_account_repos(self) -> list[dict] | None:
        """GET /user/repos?affiliation=owner&sort=pushed&per_page=100,
        paginated (v1.5.1 account-wide watching). Includes private repos
        -- that's intentional and fine, since both the resulting config
        state and the physical display are local to the operator's own
        device; see the README's "Account-wide watching" section for the
        explicit note that private repo names can render on-screen.

        Only page 1 uses a conditional (ETag) request. This is a
        deliberate tradeoff, not an oversight: `sort=pushed` puts the
        most-recently-active repos first, so page 1's ETag is the cheap
        common-case win (a single-page account -- the overwhelming
        majority -- gets a 304 whenever nothing relevant changed, costing
        zero quota); paying for pages 2+ on every re-enumeration (which
        itself only runs every `repo_refresh_minutes`, not every poll) is
        an acceptable, rare cost for >100-repo accounts, and it avoids a
        subtler correctness trap: reusing a stale cached page 2+ result
        on a page-1 304 could miss a `pushed_at` update to a repo that
        moved within page 2 without ever crossing into page 1.

        Returns `None` on a page-1 304 (nothing changed) or any failure
        (network, non-200, malformed JSON) at any page -- the caller
        (`main._refresh_account_repos`) treats `None` as "keep whatever
        was cached before" and logs a warning on a genuine failure, never
        falling back to an empty list (an empty list would look
        indistinguishable from "this account genuinely owns zero repos,"
        which would silently stop watching everything).

        The page-1 ETag is committed to `self._account_repos_etag` ONLY
        after pagination completes in full -- not right after page 1's
        own response, even though that response is where the ETag value
        comes from. Committing it earlier would create a silent
        lock-in bug for >100-repo accounts: if page 2+ then failed, this
        call would still return the partial `repos` list collected so
        far (a partial result being better than discarding everything),
        but the page-1 ETag would already be cached -- so the *next*
        call sends `If-None-Match` for a page-1 body that genuinely
        hasn't changed, gets a 304, and this method returns `None`. The
        caller reads `None` as "nothing changed, keep the cached list" --
        permanently freezing the account's watch list at that one
        incomplete pagination run's partial subset, with no future call
        ever able to recover the rest (every subsequent page-1 request
        keeps matching the same cached ETag). On an incomplete pagination
        run, `self._account_repos_etag` is simply left as whatever it
        was (not overwritten) -- safe, not just "not actively wrong":
        reaching this branch means page 1's *response this call* was a
        fresh 200, not a 304, so page 1's content has already been
        confirmed to differ from whatever the old cached ETag matched;
        the next call sending that same old ETag will therefore get
        another fresh 200 (never a wrongful 304), giving pagination
        another full chance to complete rather than reusing the partial
        result. See the regression test for the failure mode this
        guards against.
        """
        url = f"{API}/user/repos"
        headers = self._headers()
        if self._account_repos_etag is not None:
            headers["If-None-Match"] = self._account_repos_etag
        try:
            resp = requests.get(url, headers=headers,
                                params={"affiliation": "owner", "sort": "pushed", "per_page": 100},
                                timeout=(5, 15))
        except requests.RequestException as exc:
            log.debug("github unreachable (account repo enumeration): %s", exc)
            return None
        if resp.status_code == 304:
            return None
        if resp.status_code != 200:
            log.warning("github %s (account repo enumeration)", resp.status_code)
            return None
        new_etag = resp.headers.get("ETag")   # not committed yet -- see the docstring above
        try:
            page_items = resp.json()
        except ValueError as exc:
            log.warning("github returned non-JSON (account repo enumeration): %s", exc)
            return None
        if not isinstance(page_items, list):
            log.warning("github returned unexpected shape (account repo enumeration)")
            return None

        repos = list(page_items)
        page = 2
        complete = True
        while len(page_items) == 100 and page <= 10:  # cap: 1000 owned repos
            try:
                resp = requests.get(url, headers=self._headers(),
                                    params={"affiliation": "owner", "sort": "pushed",
                                           "per_page": 100, "page": page},
                                    timeout=(5, 15))
            except requests.RequestException as exc:
                log.debug("github unreachable (account repo enumeration page %d): %s", page, exc)
                complete = False
                break  # a partial result is still better than discarding everything
            if resp.status_code != 200:
                log.warning("github %s (account repo enumeration page %d)", resp.status_code, page)
                complete = False
                break
            try:
                page_items = resp.json()
            except ValueError as exc:
                log.warning("github returned non-JSON (account repo enumeration page %d): %s", page, exc)
                complete = False
                break
            if not isinstance(page_items, list):
                complete = False
                break
            repos.extend(page_items)
            page += 1

        if complete and new_etag is not None:
            self._account_repos_etag = new_etag
        return repos

    def forget_repo(self, repo: str) -> None:
        """Drop cached conditional-request state (both ETag slots) for a
        repo that has left the effective watch list -- excluded, aged out
        of `active_within_days`, or deleted upstream (v1.5.1). Called by
        `main.run_once` alongside `state_cache`/`running_cache` pruning so
        a repo that's later re-added (e.g. it becomes active again) starts
        with a clean conditional-request slate instead of an ETag from a
        stale enumeration."""
        self._etags.pop(repo, None)
        self._running_etags.pop(repo, None)
