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
