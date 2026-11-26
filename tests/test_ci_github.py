from unittest.mock import Mock, patch
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
import ci_status.github as github
from ci_status.github import FETCH_DENIED, FETCH_FAILED, RestPoller, get_token


def _response(status: int, body: dict | None = None, etag: str | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = body or {}
    resp.headers = {"ETag": etag} if etag else {}
    return resp


@patch("ci_status.github.requests.get")
def test_fetch_returns_runs_and_caches_etag(mock_get):
    mock_get.return_value = _response(200, {"workflow_runs": [{"id": 1}]}, etag='W/"abc"')
    poller = RestPoller("tok")
    assert poller.fetch_runs("o/r") == [{"id": 1}]
    assert mock_get.call_args.args[0] == "https://api.github.com/repos/o/r/actions/runs"
    assert mock_get.call_args.kwargs["params"] == {"per_page": 10}
    first_headers = mock_get.call_args.kwargs["headers"]
    assert "If-None-Match" not in first_headers
    assert first_headers["Authorization"] == "Bearer tok"
    assert "graphql" not in mock_get.call_args.args[0]

    mock_get.return_value = _response(304)
    assert poller.fetch_runs("o/r") is None  # 304 -> no change
    assert mock_get.call_args.kwargs["headers"]["If-None-Match"] == 'W/"abc"'


@patch("ci_status.github.requests.get")
def test_fetch_swallows_network_errors(mock_get):
    mock_get.side_effect = requests.ConnectionError()
    assert RestPoller("tok").fetch_runs("o/r") is FETCH_FAILED


@patch("ci_status.github.subprocess.run")
def test_get_token_error_is_actionable(mock_run):
    mock_run.return_value = Mock(returncode=1, stdout="", stderr="not logged in")
    try:
        get_token()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "gh auth login" in str(exc)


@patch("ci_status.github.requests.get")
def test_fetch_returns_none_on_malformed_json(mock_get):
    resp = Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.side_effect = ValueError("Invalid JSON")
    mock_get.return_value = resp
    assert RestPoller("tok").fetch_runs("o/r") is FETCH_FAILED


# --- fetch_running_runs: separate ETag slot from fetch_runs -------------------

@patch("ci_status.github.requests.get")
def test_fetch_running_runs_uses_status_filter_and_per_page_5(mock_get):
    mock_get.return_value = _response(200, {"workflow_runs": [{"id": 9}]}, etag='W/"run1"')
    poller = RestPoller("tok")
    assert poller.fetch_running_runs("o/r") == [{"id": 9}]
    assert mock_get.call_args.args[0] == "https://api.github.com/repos/o/r/actions/runs"
    assert mock_get.call_args.kwargs["params"] == {"status": "in_progress", "per_page": 5}


@patch("ci_status.github.requests.get")
def test_fetch_running_runs_etag_is_independent_of_fetch_runs(mock_get):
    poller = RestPoller("tok")

    # Prime fetch_runs's ETag slot.
    mock_get.return_value = _response(200, {"workflow_runs": []}, etag='W/"failstuck-etag"')
    poller.fetch_runs("o/r")

    # A fresh fetch_running_runs call must NOT send fetch_runs's ETag --
    # it's a different URL/query and gets its own slot (first call, no
    # If-None-Match yet).
    mock_get.return_value = _response(200, {"workflow_runs": [{"id": 1}]}, etag='W/"running-etag"')
    poller.fetch_running_runs("o/r")
    assert "If-None-Match" not in mock_get.call_args.kwargs["headers"]

    # The second fetch_running_runs call sends *its own* cached ETag, not
    # fetch_runs's.
    mock_get.return_value = _response(304)
    poller.fetch_running_runs("o/r")
    assert mock_get.call_args.kwargs["headers"]["If-None-Match"] == 'W/"running-etag"'


@patch("ci_status.github.requests.get")
def test_fetch_running_runs_swallows_network_errors(mock_get):
    mock_get.side_effect = requests.ConnectionError()
    assert RestPoller("tok").fetch_running_runs("o/r") is FETCH_FAILED


# --- fetch_median_eta: process-lifetime cache per workflow_id -----------------

def _run(started: str, updated: str) -> dict:
    return {"run_started_at": started, "updated_at": updated}


@patch("ci_status.github.requests.get")
def test_fetch_median_eta_computes_and_caches(mock_get):
    runs = [_run("2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z"),   # 4 min
           _run("2026-08-03T09:00:00Z", "2026-08-03T09:06:00Z")]   # 6 min
    mock_get.return_value = _response(200, {"workflow_runs": runs})
    poller = RestPoller("tok")

    assert poller.fetch_median_eta("o/r", 42) == 5.0   # median of [4, 6]
    assert mock_get.call_args.args[0] == "https://api.github.com/repos/o/r/actions/workflows/42/runs"
    assert mock_get.call_args.kwargs["params"] == {"status": "success", "per_page": 5}

    # Second call for the same workflow_id must NOT hit the network again.
    mock_get.reset_mock()
    assert poller.fetch_median_eta("o/r", 42) == 5.0
    mock_get.assert_not_called()


@patch("ci_status.github.requests.get")
def test_fetch_median_eta_caches_confirmed_no_history(mock_get):
    mock_get.return_value = _response(200, {"workflow_runs": []})
    poller = RestPoller("tok")
    assert poller.fetch_median_eta("o/r", 7) is None
    mock_get.reset_mock()
    assert poller.fetch_median_eta("o/r", 7) is None
    mock_get.assert_not_called()   # a confirmed-empty history is cached too


@patch("ci_status.github.requests.get")
def test_fetch_median_eta_does_not_cache_on_error(mock_get):
    poller = RestPoller("tok")
    mock_get.return_value = _response(500)
    assert poller.fetch_median_eta("o/r", 3) is None

    # A transient error must not lock in "no history" -- the next call
    # retries the network rather than returning a cached None forever.
    mock_get.return_value = _response(200, {"workflow_runs": [_run(
        "2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z")]})
    assert poller.fetch_median_eta("o/r", 3) == 4.0


@patch("ci_status.github.requests.get")
def test_fetch_median_eta_does_not_cache_on_network_exception(mock_get):
    poller = RestPoller("tok")
    mock_get.side_effect = requests.ConnectionError()
    assert poller.fetch_median_eta("o/r", 3) is None

    mock_get.side_effect = None
    mock_get.return_value = _response(200, {"workflow_runs": [_run(
        "2026-08-03T10:00:00Z", "2026-08-03T10:04:00Z")]})
    assert poller.fetch_median_eta("o/r", 3) == 4.0


# --- fetch_rate_limit: free endpoint, no ETag/cache -----------------------------

@patch("ci_status.github.requests.get")
def test_fetch_rate_limit_returns_raw_response(mock_get):
    body = {"resources": {"core": {"limit": 5000, "remaining": 4990, "reset": 1000, "used": 10},
                          "graphql": {"limit": 5000, "remaining": 4800, "reset": 2000, "used": 200}}}
    mock_get.return_value = _response(200, body)
    poller = RestPoller("tok")
    assert poller.fetch_rate_limit() == body
    assert mock_get.call_args.args[0] == "https://api.github.com/rate_limit"
    # No params (no status/per_page filter -- this isn't a workflow_runs
    # endpoint) and no If-None-Match (no ETag caching attempted).
    assert "If-None-Match" not in mock_get.call_args.kwargs["headers"]

@patch("ci_status.github.requests.get")
def test_fetch_rate_limit_always_hits_network_even_when_called_twice(mock_get):
    # Unlike fetch_median_eta, there's no process-lifetime cache here --
    # remaining quota changes continuously, so every call is a fresh GET.
    mock_get.return_value = _response(200, {"resources": {}})
    poller = RestPoller("tok")
    poller.fetch_rate_limit()
    poller.fetch_rate_limit()
    assert mock_get.call_count == 2

@patch("ci_status.github.requests.get")
def test_fetch_rate_limit_swallows_network_errors(mock_get):
    mock_get.side_effect = requests.ConnectionError()
    assert RestPoller("tok").fetch_rate_limit() is None

@patch("ci_status.github.requests.get")
def test_fetch_rate_limit_none_on_non_200(mock_get):
    mock_get.return_value = _response(403)
    assert RestPoller("tok").fetch_rate_limit() is None

@patch("ci_status.github.requests.get")
def test_fetch_rate_limit_none_on_malformed_json(mock_get):
    resp = Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.side_effect = ValueError("Invalid JSON")
    mock_get.return_value = resp
    assert RestPoller("tok").fetch_rate_limit() is None


# --- fetch_account_repos: discovery, pagination, page-1-only ETag (v1.5.1) ------

def _list_response(status: int, body: list | None = None, etag: str | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = [] if body is None else body
    resp.headers = {"ETag": etag} if etag else {}
    return resp

def _repo(full_name: str, pushed_at: str = "2026-08-03T10:00:00Z", archived: bool = False) -> dict:
    return {"full_name": full_name, "archived": archived, "pushed_at": pushed_at}

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_single_page_passthrough(mock_get):
    repos = [_repo("o/a"), _repo("o/b")]
    mock_get.return_value = _list_response(200, repos, etag='W/"page1"')
    poller = RestPoller("tok")
    assert poller.fetch_account_repos() == repos
    assert mock_get.call_args.args[0] == "https://api.github.com/user/repos"
    assert mock_get.call_args.kwargs["params"] == {"affiliation": "owner", "sort": "pushed", "per_page": 100}
    assert "If-None-Match" not in mock_get.call_args.kwargs["headers"]

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_uses_cached_etag_on_next_call(mock_get):
    mock_get.return_value = _list_response(200, [_repo("o/a")], etag='W/"page1"')
    poller = RestPoller("tok")
    poller.fetch_account_repos()

    mock_get.return_value = _list_response(304)
    assert poller.fetch_account_repos() is None   # 304 -> caller keeps its own cached list
    assert mock_get.call_args.kwargs["headers"]["If-None-Match"] == 'W/"page1"'

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_paginates_across_multiple_pages(mock_get):
    page1 = [_repo(f"o/r{i}") for i in range(100)]   # exactly 100 -> triggers page 2
    page2 = [_repo("o/r100"), _repo("o/r101")]        # < 100 -> stops here
    mock_get.side_effect = [_list_response(200, page1), _list_response(200, page2)]
    poller = RestPoller("tok")
    result = poller.fetch_account_repos()
    assert len(result) == 102
    assert result[-1]["full_name"] == "o/r101"
    # Page 2 request carries no If-None-Match -- only page 1 gets a slot.
    second_call = mock_get.call_args_list[1]
    assert second_call.kwargs["params"]["page"] == 2
    assert "If-None-Match" not in second_call.kwargs["headers"]

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_partial_pagination_is_no_answer_not_a_short_list(mock_get):
    # A truncated list would replace the caller's previous list for the whole
    # refresh interval and prune every repo on the missing pages.
    page1 = [_repo(f"o/r{i}") for i in range(100)]
    for page2 in (_list_response(500), _limited(403, Retry_After="60"),
                  _list_response(200, {"not": "a list"})):
        mock_get.reset_mock()
        mock_get.side_effect = [_list_response(200, page1), page2]
        assert RestPoller("tok").fetch_account_repos() is None

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_etag_not_locked_in_by_partial_pagination_failure(mock_get):
    # Regression test: page 1 must NOT have its ETag committed until the
    # WHOLE pagination run succeeds -- otherwise a transient page-2+
    # failure would "lock in" a truncated list forever (every future
    # call sends the page-1 ETag, gets a 304 since page 1 itself is
    # unchanged, and the caller reads that as "nothing changed, keep the
    # stale partial list" -- with no way to ever recover the rest).
    page1 = [_repo(f"o/r{i}") for i in range(100)]
    page2_full = [_repo("o/r100"), _repo("o/r101")]

    # First call: page 1 succeeds (with an ETag), page 2 fails.
    mock_get.side_effect = [_list_response(200, page1, etag='W/"page1-v1"'), _list_response(500)]
    poller = RestPoller("tok")
    first = poller.fetch_account_repos()
    assert first is None   # incomplete -> no answer; the caller keeps its previous list

    # Second call: must NOT send If-None-Match for the page-1 ETag from
    # the incomplete run above -- the ETag was never committed. Both
    # pages now succeed, and the FULL list (102 repos) must be recovered.
    mock_get.reset_mock()
    mock_get.side_effect = [_list_response(200, page1, etag='W/"page1-v1"'), _list_response(200, page2_full)]
    second = poller.fetch_account_repos()
    first_call_headers = mock_get.call_args_list[0].kwargs["headers"]
    assert "If-None-Match" not in first_call_headers
    assert len(second) == 102   # full list recovered, not stuck at the earlier partial 100

    # Third call, now that a FULL pagination run has succeeded: the ETag
    # SHOULD be committed this time, and a genuinely-unchanged page 1
    # correctly short-circuits via 304.
    mock_get.reset_mock()
    mock_get.side_effect = [_list_response(304)]
    third = poller.fetch_account_repos()
    assert third is None
    assert mock_get.call_args.kwargs["headers"]["If-None-Match"] == 'W/"page1-v1"'

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_swallows_network_errors(mock_get):
    mock_get.side_effect = requests.ConnectionError()
    assert RestPoller("tok").fetch_account_repos() is None

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_none_on_non_200(mock_get):
    mock_get.return_value = _list_response(403)
    assert RestPoller("tok").fetch_account_repos() is None

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_none_on_malformed_json(mock_get):
    resp = Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.side_effect = ValueError("Invalid JSON")
    mock_get.return_value = resp
    assert RestPoller("tok").fetch_account_repos() is None

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_none_on_unexpected_shape(mock_get):
    # A dict instead of a list would indicate something is very wrong
    # (wrong endpoint, API change) -- must not silently misinterpret it.
    mock_get.return_value = _response_dict_shape()
    assert RestPoller("tok").fetch_account_repos() is None

def _response_dict_shape() -> Mock:
    resp = Mock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.return_value = {"not": "a list"}
    return resp

@patch("ci_status.github.requests.get")
def test_fetch_account_repos_empty_account_returns_empty_list_not_none(mock_get):
    # Zero owned repos is a legitimate (if unusual) real answer -- distinct
    # from None, which means "treat as unknown, keep whatever was cached."
    mock_get.return_value = _list_response(200, [])
    assert RestPoller("tok").fetch_account_repos() == []


# --- forget_repo: drops both ETag slots for a repo (v1.5.1) ---------------------

@patch("ci_status.github.requests.get")
def test_forget_repo_can_clear_one_slot_only(mock_get):
    # Discarding an unusable running-runs body must not also discard the
    # runs ETag fetch_runs just validated (and vice versa).
    poller = RestPoller("tok")
    mock_get.return_value = _response(200, {"workflow_runs": []}, etag='W/"a"')
    poller.fetch_runs("o/r")
    poller.fetch_running_runs("o/r")
    poller.forget_repo("o/r", runs=False)
    assert "o/r" in poller._etags and "o/r" not in poller._running_etags
    poller.fetch_running_runs("o/r")
    poller.forget_repo("o/r", running=False)
    assert "o/r" not in poller._etags and "o/r" in poller._running_etags


@patch("ci_status.github.requests.get")
def test_forget_repo_clears_both_etag_slots(mock_get):
    poller = RestPoller("tok")
    mock_get.return_value = _response(200, {"workflow_runs": []}, etag='W/"a"')
    poller.fetch_runs("o/r")
    mock_get.return_value = _response(200, {"workflow_runs": []}, etag='W/"b"')
    poller.fetch_running_runs("o/r")
    assert "o/r" in poller._etags and "o/r" in poller._running_etags

    poller.forget_repo("o/r")
    assert "o/r" not in poller._etags
    assert "o/r" not in poller._running_etags

def test_forget_repo_unknown_repo_is_a_no_op():
    RestPoller("tok").forget_repo("never/seen")   # must not raise


# --- "failed" is not "not modified" --------------------------------------------

@patch("ci_status.github.requests.get")
def test_fetch_error_status_is_failed_not_unchanged(mock_get):
    # 304 means "your cached copy is current". A refusal (the repo is gone,
    # or this token may not read it) is FETCH_DENIED and may expire cached
    # state; a server error is FETCH_FAILED, "could not ask", and never does.
    poller = RestPoller("tok")
    for status in (401, 403, 404, 410):
        mock_get.return_value = _response(status)
        assert poller.fetch_runs("o/r") is FETCH_DENIED
        assert poller.fetch_running_runs("o/r") is FETCH_DENIED
    for status in (500, 502, 503):
        mock_get.return_value = _response(status)
        assert poller.fetch_runs("o/r") is FETCH_FAILED
        assert poller.fetch_running_runs("o/r") is FETCH_FAILED
    mock_get.return_value = _response(304)
    assert poller.fetch_runs("o/r") is None
    assert poller.fetch_running_runs("o/r") is None


@patch("ci_status.github.requests.get")
def test_fetch_non_object_body_is_failed_not_a_crash(mock_get):
    poller = RestPoller("tok")
    for body in (["not", "an", "object"], "text", {"workflow_runs": {"oops": 1}}):
        resp = _response(200)
        resp.json.return_value = body
        mock_get.return_value = resp
        assert poller.fetch_runs("o/r") is FETCH_FAILED
        assert poller.fetch_running_runs("o/r") is FETCH_FAILED
        assert poller.fetch_median_eta("o/r", 7) is None
        if not isinstance(body, dict):   # an object IS a valid /rate_limit body
            assert poller.fetch_rate_limit() is None


@patch("ci_status.github.requests.get")
def test_etag_not_committed_for_an_unusable_body(mock_get):
    # Committing the ETag of a body we could not use would turn the next
    # poll into a 304 "unchanged" for content this process never read.
    poller = RestPoller("tok")
    for fetch in (poller.fetch_runs, poller.fetch_running_runs):
        resp = _response(200, etag='W/"bad"')
        resp.json.return_value = ["not", "an", "object"]
        mock_get.return_value = resp
        assert fetch("o/r") is FETCH_FAILED
        mock_get.return_value = _response(200, {"workflow_runs": []})
        fetch("o/r")
        assert "If-None-Match" not in mock_get.call_args.kwargs["headers"]


@patch("ci_status.github.subprocess.run")
def test_get_token_timeout_is_actionable_not_a_traceback(mock_run):
    import subprocess
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gh", "auth", "token"], timeout=10)
    try:
        get_token()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "gh auth" in str(exc)


# --- rate limits: pause every request for a bounded time ------------------------

def _limited(status: int, **headers: str) -> Mock:
    resp = _response(status)
    resp.headers = {k.replace("_", "-"): v for k, v in headers.items()}
    return resp


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_retry_after_pauses_all_requests_until_it_elapses(mock_get, mock_time):
    mock_time.monotonic.return_value = 1000.0
    mock_get.return_value = _limited(403, Retry_After="120")
    poller = RestPoller("tok")
    assert poller.fetch_runs("o/r") is FETCH_FAILED
    assert mock_get.call_count == 1

    # The limit is per token, so nothing else is sent either.
    mock_time.monotonic.return_value = 1119.0
    assert poller.fetch_runs("o/r") is FETCH_FAILED
    assert poller.fetch_runs("o/other") is FETCH_FAILED
    assert poller.fetch_running_runs("o/r") is FETCH_FAILED
    assert poller.fetch_median_eta("o/r", 7) is None
    assert poller.fetch_rate_limit() is None
    assert poller.fetch_account_repos() is None
    assert mock_get.call_count == 1

    mock_time.monotonic.return_value = 1120.0
    mock_get.return_value = _response(200, {"workflow_runs": [{"id": 1}]})
    assert poller.fetch_runs("o/r") == [{"id": 1}]
    assert mock_get.call_count == 2


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_exhausted_quota_pauses_until_reset(mock_get, mock_time):
    mock_time.monotonic.return_value = 500.0
    mock_time.time.return_value = 1_800_000_000.0
    mock_get.return_value = _limited(429, X_RateLimit_Remaining="0",
                                     X_RateLimit_Reset="1800000300")
    poller = RestPoller("tok")
    assert poller.fetch_runs("o/r") is FETCH_FAILED
    mock_time.monotonic.return_value = 799.0
    poller.fetch_runs("o/r")
    assert mock_get.call_count == 1
    mock_time.monotonic.return_value = 800.0
    poller.fetch_runs("o/r")
    assert mock_get.call_count == 2


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_rate_limit_pause_is_bounded_both_ways(mock_get, mock_time):
    mock_time.monotonic.return_value = 0.0
    mock_time.time.return_value = 1_800_000_000.0
    poller = RestPoller("tok")
    # An absurd header (or a wrong local clock) cannot silence polling for long.
    mock_get.return_value = _limited(403, Retry_After="999999")
    poller.fetch_runs("o/r")
    mock_time.monotonic.return_value = float(github.RATE_LIMIT_MAX_PAUSE_SECONDS)
    poller.fetch_runs("o/r")
    assert mock_get.call_count == 2
    # A reset already in the past, or an unreadable value, still waits the
    # documented minimum instead of hammering.
    for headers in ({"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "1"},
                    {"Retry_After": "soon"}):
        poller = RestPoller("tok")
        mock_get.reset_mock()
        mock_time.monotonic.return_value = 0.0
        mock_get.return_value = _limited(429, **headers)
        poller.fetch_runs("o/r")
        mock_time.monotonic.return_value = github.RATE_LIMIT_MIN_PAUSE_SECONDS - 1.0
        poller.fetch_runs("o/r")
        assert mock_get.call_count == 1
        mock_time.monotonic.return_value = float(github.RATE_LIMIT_MIN_PAUSE_SECONDS)
        poller.fetch_runs("o/r")
        assert mock_get.call_count == 2


@patch("ci_status.github.requests.get")
def test_plain_403_is_not_treated_as_a_rate_limit(mock_get):
    # A permissions 403 (no Retry-After, quota not exhausted) concerns one
    # repo; it must not pause polling for every other repo.
    mock_get.return_value = _limited(403, X_RateLimit_Remaining="4999")
    poller = RestPoller("tok")
    assert poller.fetch_runs("o/private") is FETCH_DENIED
    mock_get.return_value = _response(200, {"workflow_runs": []})
    assert poller.fetch_runs("o/r") == []
    assert mock_get.call_count == 2


# --- repeated identical warnings are gated ---------------------------------------

def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_repeated_error_warns_once_then_on_change_and_as_a_reminder(mock_get, mock_time, caplog):
    import logging
    mock_time.monotonic.return_value = 0.0
    poller = RestPoller("tok")
    with caplog.at_level(logging.WARNING, logger="ci_status"):
        mock_get.return_value = _response(404)
        for _ in range(5):
            poller.fetch_runs("o/gone")
        assert _warnings(caplog) == ["github 404 for o/gone"]

        mock_get.return_value = _response(500)      # the fault changed: say so now
        poller.fetch_runs("o/gone")
        poller.fetch_runs("o/gone")
        assert _warnings(caplog) == ["github 404 for o/gone", "github 500 for o/gone"]

        mock_time.monotonic.return_value = float(github.WARN_REPEAT_SECONDS)
        poller.fetch_runs("o/gone")                 # still broken: periodic reminder
        assert _warnings(caplog)[2:] == ["github 500 for o/gone"]


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_unreachable_and_rate_limit_each_warn_once(mock_get, mock_time, caplog):
    import logging
    mock_time.monotonic.return_value = 0.0
    poller = RestPoller("tok")
    with caplog.at_level(logging.WARNING, logger="ci_status"):
        mock_get.side_effect = requests.ConnectionError()
        for repo in ("o/a", "o/b", "o/c"):
            poller.fetch_runs(repo)
            poller.fetch_running_runs(repo)
        assert len(_warnings(caplog)) == 1 and "unreachable" in _warnings(caplog)[0]

        mock_get.side_effect = None
        for start in (0.0, 100.0, 200.0):           # limited again on every probe
            mock_time.monotonic.return_value = start
            mock_get.return_value = _limited(403, Retry_After="60")
            poller.fetch_runs("o/a")
        limited = [m for m in _warnings(caplog) if "rate limit" in m]
        assert len(limited) == 1


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_revoked_token_warns_once_not_once_per_repo(mock_get, mock_time, caplog):
    import logging
    mock_time.monotonic.return_value = 0.0
    mock_get.return_value = _response(401)
    poller = RestPoller("tok")
    with caplog.at_level(logging.WARNING, logger="ci_status"):
        for repo in ("o/a", "o/b", "o/c"):
            poller.fetch_runs(repo)
            poller.fetch_running_runs(repo)
    assert len(_warnings(caplog)) == 1 and "gh auth login" in _warnings(caplog)[0]


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_warning_gate_holds_with_more_live_faults_than_its_size_bound(mock_get, mock_time, caplog):
    import logging
    mock_time.monotonic.return_value = 0.0
    mock_get.return_value = _response(404)
    poller = RestPoller("tok")
    repos = [f"o/r{i}" for i in range(github.MAX_WARNING_KEYS + 10)]
    with caplog.at_level(logging.WARNING, logger="ci_status"):
        for _ in range(3):                           # three polls
            for repo in repos:
                poller.fetch_runs(repo)
    assert len(_warnings(caplog)) == len(repos)      # once each, not once per poll


@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_rate_limit_and_outage_are_could_not_ask_not_denied(mock_get, mock_time):
    mock_time.monotonic.return_value = 0.0
    poller = RestPoller("tok")
    mock_get.return_value = _limited(403, Retry_After="60")
    assert poller.fetch_runs("o/r") is FETCH_FAILED          # the limiting answer
    assert poller.fetch_runs("o/r") is FETCH_FAILED          # paused: nothing sent
    mock_time.monotonic.return_value = 60.0
    mock_get.return_value = _limited(429)
    assert poller.fetch_running_runs("o/r") is FETCH_FAILED
    mock_time.monotonic.return_value = 120.0
    mock_get.side_effect = requests.ConnectionError()
    assert poller.fetch_runs("o/r") is FETCH_FAILED


# --- secondary rate limits carry no rate-limit headers ------------------------
# GitHub identifies a secondary limit only by its error message; it can arrive
# as a 403 with quota remaining and no Retry-After, and asks for at least a
# minute's wait. It must never be mistaken for a refusal about one repo.

@patch("ci_status.github.time")
@patch("ci_status.github.requests.get")
def test_secondary_rate_limit_without_headers_pauses_instead_of_denying(mock_get, mock_time):
    mock_time.monotonic.return_value = 1000.0
    limited = _limited(403, X_RateLimit_Remaining="4990")
    limited.json.return_value = {"message": "You have exceeded a secondary rate limit. Please wait."}
    mock_get.return_value = limited
    poller = RestPoller("tok")
    assert poller.fetch_runs("o/r") is FETCH_FAILED          # could not ask, not a refusal
    assert poller.fetch_runs("o/other") is FETCH_FAILED      # and everything else waits too
    assert mock_get.call_count == 1
    mock_time.monotonic.return_value = 1000.0 + github.RATE_LIMIT_MIN_PAUSE_SECONDS
    mock_get.return_value = _response(200, {"workflow_runs": []})
    assert poller.fetch_runs("o/r") == []


@patch("ci_status.github.requests.get")
def test_plain_403_without_a_rate_limit_message_is_still_a_refusal(mock_get):
    from ci_status.github import FETCH_DENIED
    for body in ({"message": "Resource not accessible by integration"}, ["not", "an", "object"], None):
        refused = _limited(403, X_RateLimit_Remaining="4990")
        refused.json.return_value = body
        mock_get.return_value = refused
        assert RestPoller("tok").fetch_runs("o/r") is FETCH_DENIED
    unreadable = _limited(403, X_RateLimit_Remaining="4990")
    unreadable.json.side_effect = ValueError("no json")
    mock_get.return_value = unreadable
    assert RestPoller("tok").fetch_runs("o/r") is FETCH_DENIED
