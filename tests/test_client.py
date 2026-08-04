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


@patch("busybar.client.time.time")
@patch("busybar.client.requests.request")
def test_set_busy_simple_payload(mock_request, mock_time):
    # Regression test for the confirmed-on-device bug: the device rejects a
    # flat BusySnapshot body (HTTP 400 "Failed to parse snapshot") even
    # though that's the shape /openapi.yaml's schema literally describes.
    # The firmware actually requires the snapshot nested under a
    # "snapshot" key alongside "snapshot_timestamp_ms" -- mirroring what
    # get_busy() (GET) returns -- and does NOT want busy_bar_settings on
    # this write path.
    mock_request.return_value = _response(200)
    mock_time.return_value = 1_700_000_000.5
    assert BusyBarClient().set_busy_simple(90_000) is True
    method, url = mock_request.call_args.args
    assert method == "PUT" and url == "http://10.0.4.20/api/busy/snapshot"
    body = mock_request.call_args.kwargs["json"]
    assert body == {
        "snapshot": {
            "type": "SIMPLE",
            "card_id": "00000000-0000-0000-0000-000000000000",
            "time_left_ms": 90_000,
            "is_paused": False,
        },
        "snapshot_timestamp_ms": 1_700_000_000_500,
    }
    assert "busy_bar_settings" not in body
    assert "busy_bar_settings" not in body["snapshot"]


@patch("busybar.client.requests.request")
def test_set_busy_simple_false_on_400(mock_request):
    # The pre-fix flat body reproduced a live 400 "Failed to parse
    # snapshot" on every call -- guard against regressing to that shape by
    # asserting the method's own failure handling is intact.
    mock_request.return_value = _response(400)
    assert BusyBarClient().set_busy_simple(90_000) is False


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


# --- play_audio (v1.5.2, calendar_countdown chirp) -------------------------------

@patch("busybar.client.requests.request")
def test_play_audio_stock_path_success(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().play_audio("calendar_countdown", stock_path="shared/calendar_event_starts.wav") is True
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "http://10.0.4.20/api/audio/play"
    body = mock_request.call_args.kwargs["json"]
    assert body == {"application_name": "calendar_countdown",
                    "stock_path": "shared/calendar_event_starts.wav"}

@patch("busybar.client.requests.request")
def test_play_audio_path_variant(mock_request):
    mock_request.return_value = _response(200)
    assert BusyBarClient().play_audio("app", path="data.snd") is True
    body = mock_request.call_args.kwargs["json"]
    assert body == {"application_name": "app", "path": "data.snd"}

def test_play_audio_requires_exactly_one_source():
    try:
        BusyBarClient().play_audio("app")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

@patch("busybar.client.requests.request")
def test_play_audio_false_on_404(mock_request):
    mock_request.return_value = _response(404)
    assert BusyBarClient().play_audio("app", stock_path="shared/nope.wav") is False

@patch("busybar.client.requests.request")
def test_play_audio_false_on_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().play_audio("app", stock_path="shared/x.wav") is False

@patch("busybar.client.requests.request")
def test_play_audio_never_touches_volume_endpoint(mock_request):
    mock_request.return_value = _response(200)
    BusyBarClient().play_audio("app", stock_path="shared/x.wav")
    for call in mock_request.call_args_list:
        assert "/api/audio/volume" not in call.args[1]
