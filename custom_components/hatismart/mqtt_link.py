"""Nối máy AI (Frigate) với Home Assistant để tự động hoá theo sự kiện camera.

GET  /api/hatismart/setup/frigate/mqtt → trạng thái: HA có MQTT chưa, Frigate đang gửi về đâu, Frigate có online không,
     tích hợp Frigate của HA trỏ đúng máy AI chưa, và cảm biến "có người" (occupancy) theo camera/vùng.
POST /api/hatismart/setup/frigate/mqtt → (nếu Frigate chưa gửi về) ghi broker của HA vào Frigate rồi khởi động lại;
     sửa / tạo tích hợp Frigate của HA cho đúng địa chỉ máy AI.
Tài khoản broker đi thẳng từ HA sang Frigate, không qua app. Chỉ quản trị.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

from aiohttp import ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)
PREFIX = "frigate"


def _broker(hass: HomeAssistant) -> dict | None:
    # bỏ mục "đã bỏ qua" (source=ignore) và mục tắt: chúng không có broker
    for e in hass.config_entries.async_entries("mqtt"):
        if e.source == "ignore" or e.disabled_by:
            continue
        data = {**e.data, **e.options}
        if data.get("broker"):
            return data
    return None


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


def _frigate_entry(hass: HomeAssistant):
    """Mục tích hợp Frigate (blakeblackshear/frigate-hass-integration) đang dùng, bỏ mục đã bỏ qua."""
    entries = [e for e in hass.config_entries.async_entries("frigate") if e.source != "ignore"]
    return next((e for e in entries if not e.disabled_by), entries[0] if entries else None)


async def _integration_installed(hass: HomeAssistant) -> bool:
    from homeassistant.loader import async_get_integration

    try:
        await async_get_integration(hass, "frigate")
        return True
    except Exception:  # noqa: BLE001
        return False


def _integration_status(hass: HomeAssistant, entry, frigate_url: str) -> dict:
    if entry is None:
        return {"entry": False}
    url = str(entry.data.get("url") or "")
    return {"entry": True, "url": url,
            "match": (urlparse(url).hostname or "") == (urlparse(frigate_url).hostname or ""),
            "loaded": entry.state == ConfigEntryState.LOADED}


def _occupancy(hass: HomeAssistant, entry, cfg: dict | None) -> dict:
    """"<camera|vùng>/<đối tượng>" → binary_sensor có người của tích hợp Frigate."""
    if entry is None or not cfg:
        return {}
    reg = er.async_get(hass)
    out: dict[str, str] = {}
    for cam, c in (cfg.get("cameras") or {}).items():
        objs = list((c.get("objects") or {}).get("track") or ["person"]) + ["all"]
        for where in [cam, *((c.get("zones") or {}).keys())]:
            for obj in objs:
                eid = reg.async_get_entity_id("binary_sensor", "frigate",
                                              f"{entry.entry_id}:occupancy_sensor:{where}_{obj}")
                if eid:
                    out[f"{where}/{obj}"] = eid
    return out


async def _link_integration(hass: HomeAssistant, d) -> str:
    """Tích hợp Frigate của HA trỏ đúng máy AI (sửa địa chỉ cũ hoặc tạo mới). Trả về việc đã làm."""
    f = d.frigate
    data = {"url": f["url"], "validate_ssl": False}
    if f.get("username"):
        data.update(username=f["username"], password=f.get("password") or "")
    entry = _frigate_entry(hass)
    if entry is None:
        if not await _integration_installed(hass):
            return "not_installed"
        r = await hass.config_entries.flow.async_init("frigate", context={"source": "user"}, data=data)
        return "created" if r.get("type") == "create_entry" else "create_failed"
    st = _integration_status(hass, entry, f["url"])
    if st["match"] and st["loaded"]:
        return "ok"
    if not st["match"]:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **data})
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    return "updated"


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
        entry = _frigate_entry(hass)
        out["integration"] = {**_integration_status(hass, entry, d.frigate["url"]),
                              "installed": entry is not None or await _integration_installed(hass)}
        out["occupancy"] = _occupancy(hass, entry, cfg)
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
        try:
            integration = await _link_integration(hass, d)
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Sửa tích hợp Frigate lỗi: %s", e)
            integration = "error"
        cfg = await self._frigate_config(d)
        if await _frigate_online(hass, ((cfg or {}).get("mqtt") or {}).get("topic_prefix") or PREFIX):
            _LOGGER.info("%s liên kết Frigate: MQTT sẵn, tích hợp %s", _user_name(request), integration)
            return self.json({"ok": True, "restarting": False, "integration": integration})
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
        _LOGGER.info("%s nối Frigate với MQTT %s:%s, tích hợp %s", _user_name(request), host, mqtt_cfg["port"], integration)
        return self.json({"ok": True, "restarting": True, "broker": host, "integration": integration})
