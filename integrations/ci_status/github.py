"""GitHub REST access. REST ONLY — GraphQL is prohibited in this repo
(see spec: operator GraphQL quota exhaustion). Conditional requests keep
steady-state REST quota usage near zero (304s don't count)."""
import logging
import subprocess
import time

import requests

log = logging.getLogger("ci_status")
API = "https://api.github.com"


class _FetchFailed:
    def __repr__(self) -> str:
        return "FETCH_FAILED"


# What fetch_runs / fetch_running_runs return when GitHub gave no usable
# answer (unreachable, an error status, rate-limited, unusable body) -- as
# opposed to None, which is a 304: GitHub confirming the cached copy is
# still current. Callers keep cached state on both, but only a run of
# FETCH_FAILED may expire it.
FETCH_FAILED = _FetchFailed()

WARN_REPEAT_SECONDS = 600   # same reminder interval as busybar.client
MAX_WARNING_KEYS = 64
# Rate-limit pause bounds. GitHub's guidance: honor Retry-After, wait for
# X-RateLimit-Reset when the quota is exhausted, and never retry sooner than
# a minute. The cap keeps an absurd header or a wrong local clock from
# silencing polling for long; while genuinely limited it costs one probe
# request per cap interval.
RATE_LIMIT_MIN_PAUSE_SECONDS = 60
RATE_LIMIT_MAX_PAUSE_SECONDS = 900


def get_token() -> str:
    try:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True,
                              text=True, timeout=10)
    except FileNotFoundError:
        raise RuntimeError("GitHub CLI not found. Install gh, then run: gh auth login")
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh auth token timed out. Check that gh auth status "
                           "answers promptly, then retry.")
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
        # time.monotonic() before which no request is sent (rate limit).
        self._paused_until = 0.0
        self._warned: dict[str, tuple[str, float]] = {}

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def warn(self, key: str, message: str) -> None:
        """Warn when a condition appears or changes, then only as a reminder
        (same contract as BusyBarClient._warn). A fault that lasts for days
        -- a deleted repo, a revoked token -- must not write one line per
        repo per poll. Public so main.run_once can route its own per-poll
        GitHub-data warnings through the same gate.
        """
        now = time.monotonic()
        last = self._warned.get(key)
        if last is not None and last[0] == message and now - last[1] < WARN_REPEAT_SECONDS:
            return
        if last is None and len(self._warned) >= MAX_WARNING_KEYS:
            # Entries past the reminder interval would warn again anyway, so
            # dropping them loses nothing and keeps repo churn from growing
            # this without bound. (Clearing everything instead would defeat
            # the gate whenever more than MAX_WARNING_KEYS faults are live.)
            self._warned = {k: v for k, v in self._warned.items()
                            if now - v[1] < WARN_REPEAT_SECONDS}
        self._warned[key] = (message, now)
        log.warning("%s", message)

    def _get(self, url: str, what: str, params: dict | None,
             etag: str | None = None) -> requests.Response | None:
        """The one place a request is sent. Returns the response for a 200
        or 304; None -- after one gated warning -- when GitHub is
        unreachable, answers with any other status, or has rate-limited
        this token (in which case nothing is sent until the pause ends).
        `what` completes the warning ("github 404 <what>") and keys its gate.
        """
        if time.monotonic() < self._paused_until:
            return None
        headers = self._headers()
        if etag is not None:
            headers["If-None-Match"] = etag
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=(5, 15))
        except requests.RequestException as exc:
            self.warn("unreachable", f"github unreachable ({type(exc).__name__})")
            return None
        if resp.status_code in (200, 304):
            return resp
        pause = self._rate_limit_pause(resp)
        if pause is not None:
            self._paused_until = time.monotonic() + pause
            log.debug("github rate limit: no requests for %ds", pause)
            self.warn("rate limit", f"github rate limit ({resp.status_code}); "
                                    "pausing GitHub requests until it clears")
            return None
        if resp.status_code == 401:
            # About the token, not the repo: one line, not one per repo. The
            # token is read once at startup, hence the restart.
            self.warn("auth", "github 401: token rejected -- run gh auth login, "
                              "then restart ci_status")
        else:
            self.warn(what, f"github {resp.status_code} {what}")
        return None

    @staticmethod
    def _rate_limit_pause(resp: requests.Response) -> float | None:
        """Seconds to stop sending requests if `resp` is a rate-limit
        answer, else None. A 403 with neither Retry-After nor an exhausted
        quota is a permissions answer about one repo, not a rate limit."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("Retry-After")
        try:
            if retry_after is not None:
                pause = float(retry_after)
            elif resp.headers.get("X-RateLimit-Remaining") == "0":
                pause = float(resp.headers.get("X-RateLimit-Reset")) - time.time()
            elif resp.status_code == 429:
                pause = 0.0
            else:
                return None
        except (TypeError, ValueError):
            pause = 0.0   # limited, but the wait is unreadable: use the minimum
        return min(max(pause, RATE_LIMIT_MIN_PAUSE_SECONDS), RATE_LIMIT_MAX_PAUSE_SECONDS)

    def _json_object(self, resp: requests.Response, what: str) -> dict | None:
        """The response body as a JSON object, or None after one gated
        warning when it is not JSON or not an object."""
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self.warn(f"body {what}", f"github returned an unusable body {what}")
            return None
        return body

    def _fetch_run_list(self, repo: str, etags: dict[str, str], params: dict,
                        what: str) -> list[dict] | None | _FetchFailed:
        """Conditional GET of a repo's workflow runs: the list on a 200,
        None on a 304, FETCH_FAILED otherwise. The ETag is committed only
        once the body proved usable -- committing it first would turn the
        next poll into a 304 for content this process never read."""
        resp = self._get(f"{API}/repos/{repo}/actions/runs", what, params,
                         etag=etags.get(repo))
        if resp is None:
            return FETCH_FAILED
        if resp.status_code == 304:
            return None
        body = self._json_object(resp, what)
        if body is None:
            return FETCH_FAILED
        runs = body.get("workflow_runs", [])
        if not isinstance(runs, list):
            self.warn(f"body {what}", f"github returned an unusable body {what}")
            return FETCH_FAILED
        if "ETag" in resp.headers:
            etags[repo] = resp.headers["ETag"]
        return runs

    def fetch_runs(self, repo: str) -> list[dict] | None | _FetchFailed:
        """Newest 10 runs: the list, None on a 304, or FETCH_FAILED."""
        return self._fetch_run_list(repo, self._etags, {"per_page": 10}, f"for {repo}")

    def fetch_running_runs(self, repo: str) -> list[dict] | None | _FetchFailed:
        """GET .../actions/runs?status=in_progress&per_page=5 -- a distinct
        URL from fetch_runs, so it uses its own ETag slot (_running_etags,
        not _etags). Same three-way result as fetch_runs."""
        return self._fetch_run_list(repo, self._running_etags,
                                    {"status": "in_progress", "per_page": 5},
                                    f"for {repo} (running poll)")

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
        what = f"for {repo} workflow {workflow_id} (median fetch)"
        resp = self._get(f"{API}/repos/{repo}/actions/workflows/{workflow_id}/runs",
                         what, {"status": "success", "per_page": 5})
        body = self._json_object(resp, what) if resp is not None else None
        runs = body.get("workflow_runs", []) if body is not None else None
        if not isinstance(runs, list):
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

        Exempt or not, it still goes through `_get`, so it is silent during
        a rate-limit pause: a secondary limit means "send nothing", and the
        caller already treats None as "no quota frames this cycle".
        """
        resp = self._get(f"{API}/rate_limit", "(rate_limit fetch)", None)
        return self._json_object(resp, "(rate_limit fetch)") if resp is not None else None

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
        resp = self._get(url, "(account repo enumeration)",
                         {"affiliation": "owner", "sort": "pushed", "per_page": 100},
                         etag=self._account_repos_etag)
        if resp is None or resp.status_code == 304:
            return None
        new_etag = resp.headers.get("ETag")   # not committed yet -- see the docstring above
        try:
            page_items = resp.json()
        except ValueError:
            page_items = None
        if not isinstance(page_items, list):
            self.warn("body (account repo enumeration)",
                      "github returned an unusable body (account repo enumeration)")
            return None

        repos = list(page_items)
        page = 2
        complete = True
        while len(page_items) == 100 and page <= 10:  # cap: 1000 owned repos
            resp = self._get(url, f"(account repo enumeration page {page})",
                             {"affiliation": "owner", "sort": "pushed",
                              "per_page": 100, "page": page})
            if resp is None or resp.status_code != 200:
                complete = False
                break  # a partial result is still better than discarding everything
            try:
                page_items = resp.json()
            except ValueError:
                page_items = None
            if not isinstance(page_items, list):
                self.warn("body (account repo enumeration)",
                          f"github returned an unusable body (account repo enumeration page {page})")
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
