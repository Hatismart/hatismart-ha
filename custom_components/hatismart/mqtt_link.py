"""Nối máy AI (Frigate) với MQTT của Home Assistant để tự động hoá theo sự kiện camera.

GET  /api/hatismart/setup/frigate/mqtt → trạng thái: HA có MQTT chưa, Frigate đang gửi về đâu, Frigate có online không.
POST /api/hatismart/setup/frigate/mqtt → ghi broker của HA vào cấu hình Frigate (mqtt.*) rồi khởi động lại Frigate.
Tài khoản broker đi thẳng từ HA sang Frigate, không qua app. Chỉ quản trị.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

from aiohttp import ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)
PREFIX = "frigate"


def _broker(hass: HomeAssistant) -> dict | None:
    entries = hass.config_entries.async_entries("mqtt")
    if not entries:
        return None
    data = {**entries[0].data, **entries[0].options}
    if not data.get("broker"):
        return None
    return data


async def _broker_host_for(hass: HomeAssistant, broker: str, frigate_host: str) -> str:
    """Broker nằm cùng máy HA (core-mosquitto, localhost…) → Frigate phải gọi bằng IP LAN của máy HA."""
    try:
        if not ipaddress.ip_address(broker).is_loopback:
            return broker
    except ValueError:
        if "." in broker:   # tên miền đầy đủ: Frigate tự phân giải
            return broker
    from homeassistant.components.network import async_get_source_ip

    try:
        return await async_get_source_ip(hass, target_ip=frigate_host)
    except Exception:  # noqa: BLE001
        return await async_get_source_ip(hass)


async def _frigate_online(hass: HomeAssistant, prefix: str) -> bool | None:
    """Frigate giữ tin `<prefix>/available` = online (retain) → đăng ký là nhận ngay."""
    try:
        from homeassistant.components import mqtt
    except ImportError:
        return None
    if _broker(hass) is None:
        return None
    fut = hass.loop.create_future()

    @callback
    def got(msg):
        if not fut.done():
            fut.set_result(msg.payload)

    try:
        unsub = await mqtt.async_subscribe(hass, f"{prefix}/available", got)
    except Exception:  # noqa: BLE001
        return None
    try:
        payload = await asyncio.wait_for(fut, 3)
        return (payload.decode() if isinstance(payload, bytes) else str(payload)) == "online"
    except asyncio.TimeoutError:
        return False
    finally:
        unsub()


class FrigateMqttView(HomeAssistantView):
    url = "/api/hatismart/setup/frigate/mqtt"
    name = "api:hatismart:frigate_mqtt"

    async def _frigate_config(self, d) -> dict | None:
        try:
            async with d.session().get(d.frigate["url"] + "/api/config", headers=await d.frigate_headers(),
                                       timeout=ClientTimeout(total=8)) as r:
                return await r.json(content_type=None) if r.status == 200 else None
        except Exception:  # noqa: BLE001
            return None

    async def get(self, request):
        from . import _data, _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        hass = request.app["hass"]
        if d is None or not d.frigate.get("url"):
            return self.json({"error": "not_configured"}, status_code=404)
        b = _broker(hass)
        cfg = await self._frigate_config(d)
        m = (cfg or {}).get("mqtt") or {}
        prefix = m.get("topic_prefix") or PREFIX
        out = {
            "ha_mqtt": b is not None,
            "frigate_reachable": cfg is not None,
            "frigate": {"enabled": bool(m.get("enabled")), "host": m.get("host", ""), "port": m.get("port", 1883),
                        "topic_prefix": prefix},
            "online": await _frigate_online(hass, prefix),
        }
        if b is not None:
            out["broker"] = await _broker_host_for(hass, str(b["broker"]), urlparse(d.frigate["url"]).hostname or "")
        return self.json(out)

    async def post(self, request):
        from . import _data, _is_admin, _user_name

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        hass = request.app["hass"]
        if d is None or not d.frigate.get("url"):
            return self.json({"error": "not_configured"}, status_code=404)
        b = _broker(hass)
        if b is None:
            return self.json({"error": "no_mqtt", "message": "Máy chủ chưa bật dịch vụ sự kiện. Báo kỹ thuật Hatismart."},
                             status_code=400)
        host = await _broker_host_for(hass, str(b["broker"]), urlparse(d.frigate["url"]).hostname or "")
        mqtt_cfg = {"enabled": True, "host": host, "port": int(b.get("port") or 1883),
                    "topic_prefix": PREFIX, "client_id": "frigate"}
        if b.get("username"):
            mqtt_cfg["user"] = b["username"]
            mqtt_cfg["password"] = b.get("password") or ""
        base = d.frigate["url"]
        headers = await d.frigate_headers()
        try:
            async with d.session().put(base + "/api/config/set", headers=headers, timeout=ClientTimeout(total=20),
                                       json={"requires_restart": 0, "config_data": {"mqtt": mqtt_cfg}}) as r:
                if r.status != 200:
                    body = await r.json(content_type=None)
                    return self.json({"error": "frigate_rejected",
                                      "message": (body or {}).get("message") or f"Máy AI báo lỗi {r.status}"}, status_code=400)
        except Exception:  # noqa: BLE001
            return self.json({"error": "unreachable", "message": "Không kết nối được máy AI"}, status_code=502)
        try:   # Frigate có thể cắt kết nối ngay khi khởi động lại
            async with d.session().post(base + "/api/restart", headers=headers, json={}, timeout=ClientTimeout(total=20)):
                pass
        except Exception:  # noqa: BLE001
            pass
        _LOGGER.info("%s nối Frigate với MQTT %s:%s", _user_name(request), host, mqtt_cfg["port"])
        return self.json({"ok": True, "broker": host})
