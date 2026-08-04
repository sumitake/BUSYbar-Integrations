from unittest.mock import Mock, patch
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from ci_status.github import RestPoller, get_token


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
    assert RestPoller("tok").fetch_runs("o/r") is None


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
    assert RestPoller("tok").fetch_runs("o/r") is None


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
    assert RestPoller("tok").fetch_running_runs("o/r") is None


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
