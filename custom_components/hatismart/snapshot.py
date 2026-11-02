"""Ảnh kèm thông báo: link ảnh sự kiện camera AI có chữ ký, hết hạn sau 1 giờ.

Dịch vụ hatismart.ai_snapshot_url (trả về {"url": ...}) → kịch bản gắn vào `data.image` của thông báo.
GET /api/hatismart/snap/<camera>/<sự kiện>.jpg?exp=…&sig=… không cần đăng nhập (điện thoại tải ảnh
từ phần mở rộng thông báo), chỉ đúng chữ ký và còn hạn mới trả ảnh.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time

import voluptuous as vol
from aiohttp import web, ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN

SERVICE = "ai_snapshot_url"
TTL = 3600
NAME = re.compile(r"^[\w.-]+$")


async def _secret(d) -> bytes:
    s = d.config.get("snap_secret")
    if not s:
        s = d.config["snap_secret"] = secrets.token_hex(32)
        await d.save()
    return bytes.fromhex(s)


def _sign(key: bytes, camera: str, event: str, exp: int) -> str:
    return hmac.new(key, f"{camera}/{event}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


async def async_setup(hass: HomeAssistant) -> None:
    async def handle(call: ServiceCall) -> dict:
        d = hass.data.get(DOMAIN)
        camera, event = str(call.data["camera"]), str(call.data.get("event_id") or "")
        if d is None or not NAME.match(camera) or (event and not NAME.match(event)):
            return {"url": ""}
        base = str(call.data.get("base") or "").rstrip("/")
        if not base:
            try:
                base = get_url(hass, prefer_external=True)
            except NoURLAvailableError:
                return {"url": ""}
        exp = int(time.time()) + TTL
        ev = event or "latest"
        sig = _sign(await _secret(d), camera, ev, exp)
        return {"url": f"{base}/api/hatismart/snap/{camera}/{ev}.jpg?exp={exp}&sig={sig}"}

    hass.services.async_register(
        DOMAIN, SERVICE, handle,
        schema=vol.Schema({vol.Required("camera"): str, vol.Optional("event_id"): vol.Any(str, None),
                           vol.Optional("base"): str}),
        supports_response=SupportsResponse.ONLY,
    )


def async_unload(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE)


class SnapshotView(HomeAssistantView):
    url = "/api/hatismart/snap/{camera}/{event}"
    name = "api:hatismart:snap"
    requires_auth = False

    async def get(self, request, camera: str, event: str):
        event = event.removesuffix(".jpg")
        d = request.app["hass"].data.get(DOMAIN)
        try:
            exp = int(request.query.get("exp", "0"))
        except ValueError:
            exp = 0
        sig = request.query.get("sig", "")
        if (d is None or not d.frigate.get("url") or not NAME.match(camera) or not NAME.match(event)
                or exp < time.time() or exp > time.time() + TTL + 60
                or not hmac.compare_digest(sig, _sign(await _secret(d), camera, event, exp))):
            return web.Response(status=403)
        base, headers = d.frigate["url"], await d.frigate_headers()
        # sự kiện đang diễn ra có thể chưa có ảnh chụp → lấy ảnh nhỏ, cuối cùng là khung hình hiện tại
        paths = [f"/api/events/{event}/snapshot.jpg?bbox=1&quality=80", f"/api/events/{event}/thumbnail.jpg"] \
            if event != "latest" else []
        paths.append(f"/api/{camera}/latest.jpg?h=720&quality=80")
        for p in paths:
            try:
                async with d.session().get(base + p, headers=headers, timeout=ClientTimeout(total=8)) as r:
                    if r.status == 200 and r.content_type.startswith("image/"):
                        return web.Response(body=await r.read(), content_type=r.content_type,
                                            headers={"Cache-Control": "private, max-age=3600"})
            except Exception:  # noqa: BLE001
                continue
        return web.Response(status=404)
