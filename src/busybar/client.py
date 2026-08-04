import logging
import time
from enum import Enum

import requests

log = logging.getLogger(__name__)

NULL_CARD_ID = "00000000-0000-0000-0000-000000000000"


class DrawResult(Enum):
    DRAWN = "drawn"
    REJECTED = "rejected"        # 409: higher-priority app on screen — expected
    UNREACHABLE = "unreachable"  # device off / USB unplugged — caller backs off
    ERROR = "error"              # non-200/409 from a live device — no backoff; retried next poll


class BusyBarClient:
    def __init__(self, host: str = "10.0.4.20", timeout: tuple = (3, 5)):
        self.base = f"http://{host}"
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> requests.Response | None:
        try:
            return requests.request(method, f"{self.base}{path}", timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            log.debug("device unreachable: %s", exc)
            return None

    def draw(self, application_name: str, elements: list[dict], priority: int = 50,
             led_notification_color: str | None = None) -> DrawResult:
        body: dict = {"application_name": application_name, "priority": priority,
                      "elements": elements}
        if led_notification_color is not None:
            body["led_notification_color"] = led_notification_color
        resp = self._request("POST", "/api/display/draw", json=body)
        if resp is None:
            return DrawResult.UNREACHABLE
        if resp.status_code == 409:
            return DrawResult.REJECTED
        if resp.status_code == 200:
            return DrawResult.DRAWN
        log.warning("draw failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return DrawResult.ERROR

    def play_audio(self, application_name: str, stock_path: str | None = None,
                   path: str | None = None) -> bool:
        """POST /api/audio/play (v1.5.2, added for calendar_countdown's
        event-start chirp). Exactly one of `stock_path` (a firmware-shipped
        sound, e.g. "shared/calendar_event_starts.snd" -- pattern
        `shared/[a-z0-9_.]+$`, no further subdirectories) or `path` (a file
        previously uploaded into this app's own assets directory) must be
        given, matching the device's own PlayAudio schema. Never touches
        `/api/audio/volume` -- this method has no volume parameter at all,
        deliberately, so a caller can't accidentally change the operator's
        own volume setting; playback always uses whatever volume is
        currently configured on the device.

        **Stock sound filenames are `.snd` at runtime, not `.wav`, even
        though the source assets in the firmware repo are `.wav` files.**
        The build pipeline converts `.wav` sources to `.snd` at packaging
        time; the source tree and the OpenAPI spec never reveal this --
        the only way to find the real runtime filename is a live `GET
        /api/storage/list` of the target directory (e.g.
        `/ext/apps_assets/shared/sounds`) against the actual device.
        Always verify a stock filename against that listing before
        shipping it in a `stock_path`, not against the source repo or the
        API docs.

        **A `True` return does NOT prove audible playback.** This
        endpoint returns `200` BEFORE the actual file open -- playback is
        queued behind a short amp holdoff (~100ms), and an open failure
        at holdoff-fire (e.g. because the filename is wrong) is logged
        device-side only and otherwise swallowed; nothing comes back over
        this HTTP response either way. A wrong filename (confirmed with
        the original, incorrect `.wav` stock_path used here before this
        was diagnosed) is therefore indistinguishable from a correct one
        at every layer this codebase can observe -- the request succeeds,
        the response is `200`, and `play_audio` returns `True`, with no
        actual sound. The only way to confirm real audibility is a human
        listening on the actual hardware; log every attempt's outcome
        (both `True` and `False`) at the call site so a silent-but-
        "successful" chirp is at least visible in the log for later
        correlation against an operator report, rather than doubly silent
        (no sound AND no log line) the way the original bug was.

        Returns True on a confirmed 200, False on anything else (network
        unreachable, 400 invalid path, 404 file not found, or any other
        non-200) -- best-effort, non-fatal by design: a caller should log
        the outcome but never let an audio failure block or crash the
        display loop (the same "audio failure may occur after display
        content is visible" tolerance the device's own client libraries
        document for this endpoint).
        """
        body: dict = {"application_name": application_name}
        if stock_path is not None:
            body["stock_path"] = stock_path
        elif path is not None:
            body["path"] = path
        else:
            raise ValueError("play_audio requires exactly one of stock_path or path")
        resp = self._request("POST", "/api/audio/play", json=body)
        if resp is None:
            log.debug("play_audio: device unreachable")
            return False
        if resp.status_code == 200:
            return True
        log.warning("play_audio failed: HTTP %s %s", resp.status_code, resp.text[:200])
        return False

    def clear(self, application_name: str) -> bool:
        resp = self._request("DELETE", "/api/display/draw",
                             params={"application_name": application_name})
        return resp is not None and resp.status_code == 200

    def status(self) -> dict | None:
        resp = self._request("GET", "/api/status")
        return resp.json() if resp is not None and resp.status_code == 200 else None

    def get_busy(self) -> dict | None:
        resp = self._request("GET", "/api/busy/snapshot")
        return resp.json() if resp is not None and resp.status_code == 200 else None

    def set_busy_simple(self, time_left_ms: int) -> bool:
        """PUT /api/busy/snapshot to start a SIMPLE BUSY session (used by
        calendar_countdown's auto_busy=true feature).

        The device's /openapi.yaml documents BusySnapshot as the
        discriminated snapshot variant merged (via allOf) with a required
        top-level `busy_bar_settings`, sent flat -- that is the shape this
        method sent before this fix. Empirically, against a live device,
        that flat body gets HTTP 400 "Failed to parse snapshot" every
        time. The shape the firmware actually accepts mirrors what
        get_busy() (GET, unaffected by this bug) returns: the snapshot
        variant nested under a "snapshot" key, sibling to a top-level
        "snapshot_timestamp_ms" -- and, on this write path, WITHOUT
        `busy_bar_settings` at all, despite the spec marking it required.
        Confirmed on-device: the nested body with no `busy_bar_settings`
        returns 200 and the session actually starts (visible in a
        subsequent get_busy() snapshot).

        `snapshot_timestamp_ms` must be a genuinely current timestamp, not
        a stale or placeholder value -- also confirmed on-device: PUTting
        this same nested body with a stale `snapshot_timestamp_ms` (e.g.
        one copied from a prior GET) still returns HTTP 200, but the
        write silently no-ops and the busy state does not actually
        change. Always send `time.time()`-derived "now", never a fixed
        or cached value.
        """
        body = {
            "snapshot": {"type": "SIMPLE", "card_id": NULL_CARD_ID,
                        "time_left_ms": time_left_ms, "is_paused": False},
            "snapshot_timestamp_ms": int(time.time() * 1000),
        }
        resp = self._request("PUT", "/api/busy/snapshot", json=body)
        return resp is not None and resp.status_code == 200
