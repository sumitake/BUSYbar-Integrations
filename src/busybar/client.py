import logging
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
        body = {"type": "SIMPLE", "card_id": NULL_CARD_ID,
                "time_left_ms": time_left_ms, "is_paused": False}
        resp = self._request("PUT", "/api/busy/snapshot", json=body)
        return resp is not None and resp.status_code == 200
