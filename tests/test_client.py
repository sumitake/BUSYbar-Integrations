import logging
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


# --- cloud transport fallback (v1.6) ----------------------------------------
# Placeholder tokens only -- never anything resembling a real credential.
FAKE_TOKEN = "test-placeholder-token-do-not-use"


def _cloud_client(host="192.0.2.1", **kwargs):
    return BusyBarClient(host=host, cloud_token=FAKE_TOKEN,
                         cloud_base_url="https://cloud.example.test/busybar", **kwargs)


@patch("busybar.client.requests.request")
def test_auto_falls_back_to_cloud_on_local_failure(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 2
    local_call, cloud_call = mock_request.call_args_list
    assert local_call.args == ("POST", "http://192.0.2.1/api/display/draw")
    assert cloud_call.args == ("POST", "https://cloud.example.test/busybar/display/draw")
    assert client.active_transport == "cloud"


@patch("busybar.client.requests.request")
def test_no_fallback_when_cloud_unconfigured(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    client = BusyBarClient(host="192.0.2.1")  # cloud_token defaults to ""
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 1  # only local was ever attempted
    assert client.active_transport == "local"  # never transitions without cloud configured


@patch("busybar.client.requests.request")
def test_unreachable_when_both_transports_fail(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 2  # local AND cloud were both attempted
    assert client.active_transport == "cloud"


@patch("busybar.client.requests.request")
def test_forced_local_never_attempts_cloud_even_on_failure(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    client = _cloud_client(transport="local")
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[1].startswith("http://192.0.2.1")


@patch("busybar.client.requests.request")
def test_forced_cloud_always_goes_straight_to_cloud(mock_request):
    mock_request.return_value = _response(200)
    client = _cloud_client(transport="cloud")
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1
    method, url = mock_request.call_args.args
    assert method == "POST" and url == "https://cloud.example.test/busybar/display/draw"
    assert client.active_transport == "cloud"


@patch("busybar.client.requests.request")
def test_cloud_request_shape_bearer_header_and_base_url(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS, priority=30)
    cloud_call = mock_request.call_args_list[1]
    assert cloud_call.args == ("POST", "https://cloud.example.test/busybar/display/draw")
    assert cloud_call.kwargs["headers"] == {"Authorization": f"Bearer {FAKE_TOKEN}"}
    assert cloud_call.kwargs["timeout"] == (5, 15)
    assert cloud_call.kwargs["json"]["application_name"] == "app"


@patch("busybar.client.requests.request")
def test_cloud_path_mapping_strips_api_prefix(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200, {})]
    client = _cloud_client()
    client.status()
    cloud_call = mock_request.call_args_list[1]
    assert cloud_call.args == ("GET", "https://cloud.example.test/busybar/status")


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_degraded_client_skips_local_within_retry_window(mock_request, mock_time):
    # First call degrades to cloud at t=0. A second call at t=30 (well
    # inside LOCAL_RETRY_SECONDS=60) must skip the local attempt entirely
    # and go straight to cloud -- only one requests.request call for the
    # second draw, and it must be the cloud URL.
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 2  # local (failed) + cloud (succeeded)

    mock_time.return_value = 30.0
    mock_request.reset_mock()
    mock_request.side_effect = None
    mock_request.return_value = _response(200)
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1  # local skipped -- straight to cloud
    method, url = mock_request.call_args.args
    assert url == "https://cloud.example.test/busybar/display/draw"


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_degraded_client_probes_local_after_retry_window_elapses(mock_request, mock_time):
    # Degrade at t=0, then let LOCAL_RETRY_SECONDS (60) elapse: the next
    # call must try local FIRST again (the recovery probe), and recover
    # to active_transport == "local" when it succeeds.
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)
    assert client.active_transport == "cloud"

    mock_time.return_value = 61.0  # LOCAL_RETRY_SECONDS elapsed
    mock_request.reset_mock()
    mock_request.side_effect = None
    mock_request.return_value = _response(200)  # local now recovered
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1  # local probe succeeded, no cloud needed
    method, url = mock_request.call_args.args
    assert url == "http://192.0.2.1/api/display/draw"
    assert client.active_transport == "local"


@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_degraded_client_reprobes_local_and_stays_cloud_if_still_down(mock_request, mock_time):
    # Recovery probe fires after the window elapses but local is STILL
    # down: client must fall back to cloud again for that same request
    # (not return UNREACHABLE just because the probe failed) and reset
    # the degraded timer so the next probe is another window out.
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)

    mock_time.return_value = 61.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]  # local still down, cloud up
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert client.active_transport == "cloud"
    assert client._degraded_since == 61.0  # timer reset to the failed probe's time


@patch("busybar.client.requests.request")
def test_no_recovery_probe_before_window_when_never_degraded(mock_request):
    # A freshly-constructed "auto" client (active_transport == "local")
    # always tries local first, regardless of LOCAL_RETRY_SECONDS -- the
    # probe-skip logic only applies once actually degraded.
    mock_request.return_value = _response(200)
    client = _cloud_client()
    assert client.active_transport == "local"
    assert client.draw("app", ELEMENTS) == DrawResult.DRAWN
    assert mock_request.call_count == 1
    assert mock_request.call_args.args[1] == "http://192.0.2.1/api/display/draw"


# --- upload_asset (nyan_filler, local-only) ---------------------------------

@patch("busybar.client.requests.request")
def test_upload_asset_success(mock_request):
    mock_request.return_value = _response(200)
    data = b"\x00\x01\x02binarydata"
    assert BusyBarClient().upload_asset("nyan_filler", "nyan_72x16.anim", data) is True
    method, url = mock_request.call_args.args
    assert method == "POST"
    assert "application_name=nyan_filler" in url and "file=nyan_72x16.anim" in url
    assert mock_request.call_args.kwargs["headers"] == {"Content-Type": "application/octet-stream"}
    assert mock_request.call_args.kwargs["data"] == data


@patch("busybar.client.requests.request")
def test_upload_asset_false_on_non_200(mock_request):
    mock_request.return_value = _response(500)
    assert BusyBarClient().upload_asset("nyan_filler", "nyan_72x16.anim", b"x") is False


@patch("busybar.client.requests.request")
def test_upload_asset_false_when_unreachable(mock_request):
    mock_request.side_effect = requests.ConnectionError()
    assert BusyBarClient().upload_asset("nyan_filler", "nyan_72x16.anim", b"x") is False


# --- token never logged (v1.6 security requirement) -------------------------

@patch("busybar.client.time.monotonic")
@patch("busybar.client.requests.request")
def test_cloud_token_never_appears_in_log_output(mock_request, mock_time, caplog):
    caplog.set_level(logging.DEBUG, logger="busybar.client")
    mock_time.return_value = 0.0
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)  # local fails, cloud succeeds -- degrades to cloud

    # Still well within LOCAL_RETRY_SECONDS (60): per the recovery-probe
    # design (see test_degraded_client_skips_local_within_retry_window),
    # this second call skips the local attempt entirely and goes straight
    # to cloud -- only ONE requests.request call happens here, not two.
    mock_time.return_value = 30.0
    mock_request.side_effect = [requests.ConnectionError()]  # the sole (cloud) attempt fails
    assert client.draw("app", ELEMENTS) == DrawResult.UNREACHABLE

    for record in caplog.records:
        assert FAKE_TOKEN not in record.getMessage()
        assert FAKE_TOKEN not in str(record.args)


# --- fallback-only-on-RequestException contract (final-gate review) --------

@patch("busybar.client.requests.request")
def test_local_http_500_is_error_and_does_not_trigger_cloud_fallback(mock_request):
    # Highest-risk semantic in the whole feature: a local HTTP error
    # response (no exception -- the device is reachable, it just returned
    # a bad status) must NOT be treated as "local is down." Locks in that
    # fallback triggers ONLY on requests.RequestException, never on a
    # non-2xx/409 response the device actually returned.
    local_500 = _response(500)
    mock_request.return_value = local_500
    client = _cloud_client()
    assert client.draw("app", ELEMENTS) == DrawResult.ERROR
    assert mock_request.call_count == 1  # cloud was never attempted
    assert mock_request.call_args.args == ("POST", "http://192.0.2.1/api/display/draw")
    assert client.active_transport == "local"  # never degraded


# --- cloud 409 -> REJECTED (final-gate review) -------------------------------

@patch("busybar.client.requests.request")
def test_cloud_409_is_rejected_not_error_forced_cloud(mock_request):
    mock_request.return_value = _response(409)
    client = _cloud_client(transport="cloud")
    assert client.draw("app", ELEMENTS) == DrawResult.REJECTED

@patch("busybar.client.requests.request")
def test_cloud_409_is_rejected_not_error_while_degraded(mock_request):
    mock_request.side_effect = [requests.ConnectionError(), _response(200)]
    client = _cloud_client()
    client.draw("app", ELEMENTS)  # degrades to cloud
    assert client.active_transport == "cloud"

    mock_request.side_effect = [_response(409)]  # degraded -- skips local, straight to cloud
    assert client.draw("app", ELEMENTS) == DrawResult.REJECTED


# --- transport ValueError guard (final-gate review) --------------------------

def test_invalid_transport_value_raises_value_error():
    try:
        BusyBarClient(transport="carrier-pigeon")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "carrier-pigeon" in str(exc)
