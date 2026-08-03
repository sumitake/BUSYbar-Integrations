from unittest.mock import Mock, patch
import requests
from busybar.client import BusyBarClient, DrawResult

ELEMENTS = [{"id": "0", "type": "text", "text": "hi", "font": "normal"}]


def _response(status_code: int, payload: dict | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.text = "error text"
    return resp


@patch("busybar.client.requests.request")
def test_draw_success(mock_request):
    mock_request.return_value = _response(200)
    client = BusyBarClient(host="192.0.2.1")
    assert client.draw("app", ELEMENTS, priority=20) == DrawResult.DRAWN
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "http://192.0.2.1/api/display/draw"
    body = mock_request.call_args.kwargs["json"]
    assert body["application_name"] == "app" and body["priority"] == 20
    assert "led_notification_color" not in body


@patch("busybar.client.requests.request")
def test_draw_409_is_rejected_not_error(mock_request):
    mock_request.return_value = _response(409)
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.REJECTED


@patch("busybar.client.requests.request")
def test_draw_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.UNREACHABLE


@patch("busybar.client.requests.request")
def test_clear_scopes_to_app(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().clear("app") is True
    assert mock_request.call_args.kwargs["params"] == {"application_name": "app"}


@patch("busybar.client.requests.request")
def test_status_none_when_unreachable(mock_request):
    mock_request.side_effect = requests.Timeout()
    assert BusyBarClient().status() is None


@patch("busybar.client.requests.request")
def test_set_busy_simple_payload(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().set_busy_simple(90_000) is True
    body = mock_request.call_args.kwargs["json"]
    assert body == {
        "type": "SIMPLE",
        "card_id": "00000000-0000-0000-0000-000000000000",
        "time_left_ms": 90_000,
        "is_paused": False,
    }


@patch("busybar.client.requests.request")
def test_draw_500_is_error_not_unreachable(mock_request):
    mock_request.return_value = _response(500)
    assert BusyBarClient().draw("app", ELEMENTS) == DrawResult.ERROR


@patch("busybar.client.requests.request")
def test_get_busy_success(mock_request):
    payload = {"type": "SIMPLE", "time_left_ms": 12_000}
    mock_request.return_value = _response(200, payload)
    assert BusyBarClient().get_busy() == payload


@patch("busybar.client.requests.request")
def test_status_success(mock_request):
    payload = {"version": "1.0.0", "uptime_ms": 300_000}
    mock_request.return_value = _response(200, payload)
    assert BusyBarClient().status() == payload
