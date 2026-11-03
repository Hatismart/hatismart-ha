"""Hatismart DALI Gateway (firmware ESPHome hatismart.dali_gateway): cài đặt bóng từ app.

Gateway chỉ nhận lệnh cài đặt (quét, nháy, nhóm, thông số…) qua trang web của nó (ô "DALI Cmd"), MQTT chỉ
bật/tắt đèn → máy chủ gọi trang web của gateway trong LAN, app gọi máy chủ (dùng được cả khi ở xa).

GET  /api/hatismart/dali               → gateway tìm thấy (tin esphome/discover/* trên MQTT) + gateway đã lưu
POST /api/hatismart/dali               → lưu tài khoản trang web gateway {id, ip, username, password} (thử đăng nhập trước)
GET  /api/hatismart/dali/<id>          → bóng, việc đang chạy, thông số bóng vừa đọc, tên nhóm, cảnh báo
POST /api/hatismart/dali/<id>/cmd      → {cmd: "scan" | "identify 3" | "group add 3 1" | …} (chỉ lệnh trong DANH SÁCH)
Mọi lệnh ghi chỉ dành cho quản trị. Mật khẩu gateway nằm trên máy chủ, không trả về app.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import quote

from aiohttp import BasicAuth, ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

_LOGGER = logging.getLogger(__name__)
PROJECT = "hatismart.dali"
ID = re.compile(r"^[0-9a-f]{12}$")
HOST = re.compile(r"^[\w.-]{1,64}$")
# lệnh cài đặt bóng cho phép từ app (không cho raw / knx / node / mqtt…)
VERBS = {"scan", "commission", "stop", "identify", "level", "ct", "name", "group", "groups", "gname",
         "params", "set", "addr", "forget", "dupfix"}
CMD = re.compile(r"^[^\x00-\x1f#]{1,110}$")
SENSORS = {"devices": "DALI Devices", "job": "DALI Job", "params": "DALI Params", "nodes": "DALI Nodes", "alerts": "DALI Alerts"}

_locks: dict[str, asyncio.Lock] = {}
_cache: dict[str, tuple[float, dict]] = {}
_found: tuple[float, dict] = (0.0, {})
_nonce = int(time.time()) % 100000


def _saved(d) -> dict:
    return d.config.setdefault("dali", {})


async def _discover(hass: HomeAssistant) -> dict:
    """Tin giữ lại esphome/discover/<tên> của gateway: {ip, mac, friendly_name, project_name…}"""
    global _found
    if time.time() - _found[0] < 60:
        return _found[1]
    try:
        from homeassistant.components import mqtt
    except ImportError:
        return {}
    out: dict = {}

    @callback
    def got(msg):
        try:
            p = json.loads(msg.payload)
        except (ValueError, TypeError):
            return
        mac = str(p.get("mac") or "").lower()
        if str(p.get("project_name") or "").startswith(PROJECT) and ID.match(mac):
            out[mac] = {"ip": p.get("ip", ""), "name": p.get("friendly_name") or p.get("name") or "DALI Gateway",
                        "version": p.get("project_version") or p.get("version", "")}

    try:
        unsub = await mqtt.async_subscribe(hass, "esphome/discover/+", got)
    except Exception:  # noqa: BLE001
        return {}
    await asyncio.sleep(1.5)   # tin giữ lại về gần như ngay
    unsub()
    _found = (time.time(), out)
    return out


class _Gw:
    def __init__(self, d, gid: str) -> None:
        self.d, self.gid = d, gid
        self.cfg = _saved(d).get(gid) or {}

    @property
    def ready(self) -> bool:
        return bool(self.cfg.get("ip") and self.cfg.get("username"))

    def _auth(self, cfg: dict | None = None):
        c = cfg or self.cfg
        return BasicAuth(c["username"], c.get("password", "")) if c.get("username") else None

    async def get(self, domain: str, name: str, cfg: dict | None = None) -> tuple[int, dict | None]:
        c = cfg or self.cfg
        url = f"http://{c['ip']}/{domain}/{quote(name)}"
        try:
            async with self.d.session().get(url, auth=self._auth(c), timeout=ClientTimeout(total=4)) as r:
                return r.status, (await r.json(content_type=None) if r.status == 200 else None)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("DALI %s: %s", url, e)
            return 0, None

    async def cmd(self, line: str) -> int:
        global _nonce
        _nonce += 1   # ô lệnh chỉ chạy khi giá trị đổi → thêm mã sau '#', gateway bỏ phần này
        url = f"http://{self.cfg['ip']}/text/{quote('DALI Cmd')}/set"
        try:
            async with self.d.session().post(url, params={"value": f"{line}#{_nonce}"}, auth=self._auth(),
                                             timeout=ClientTimeout(total=4)) as r:
                return r.status
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("DALI cmd %s: %s", line, e)
            return 0


def _lock(gid: str) -> asyncio.Lock:
    # ESP32 chỉ phục vụ vài kết nối: gọi lần lượt
    return _locks.setdefault(gid, asyncio.Lock())


def _parse(raw: dict) -> dict:
    def val(k):
        v = raw.get(k)
        return (v or {}).get("value") if isinstance(v, dict) else None

    def js(k, default):
        try:
            return json.loads(val(k) or "")
        except (ValueError, TypeError):
            return default

    devices = []
    # [a, type, feat, level, mirek, groups, online, "tên", status, dup, …]
    for x in js("devices", []):
        if isinstance(x, list) and len(x) >= 10:
            devices.append({"a": x[0], "type": x[1], "feat": x[2], "level": x[3], "mirek": x[4], "groups": x[5],
                            "online": bool(x[6]), "name": x[7], "status": x[8], "dup": bool(x[9])})
    nodes = js("nodes", {})
    return {"devices": devices, "job": val("job") or "", "params": js("params", None),
            "groups": (nodes.get("g") if isinstance(nodes, dict) else None) or [""] * 16,
            "alerts": (val("alerts") or "").strip()}


class DaliListView(HomeAssistantView):
    url = "/api/hatismart/dali"
    name = "api:hatismart:dali"

    async def get(self, request):
        from . import _data

        d = _data(request)
        if d is None:
            return self.json({"error": "not_loaded"}, status_code=503)
        found = await _discover(request.app["hass"])
        saved = _saved(d)
        out = []
        for gid in sorted(set(found) | set(saved)):
            f, s = found.get(gid, {}), saved.get(gid, {})
            out.append({"id": gid, "name": s.get("name") or f.get("name") or "DALI Gateway", "ip": f.get("ip") or s.get("ip", ""),
                        "version": f.get("version", ""), "found": gid in found, "configured": bool(s.get("username"))})
        return self.json({"gateways": out})

    async def post(self, request):
        from . import _data, _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        body = await request.json()
        gid = str(body.get("id") or "").lower()
        ip = str(body.get("ip") or "").strip()
        if not ip:
            ip = (await _discover(request.app["hass"])).get(gid, {}).get("ip", "")
        if not ID.match(gid) or not HOST.match(ip):
            return self.json({"error": "bad_request"}, status_code=400)
        cfg = {"ip": ip, "username": str(body.get("username") or ""), "password": str(body.get("password") or "")}
        if body.get("name"):
            cfg["name"] = str(body["name"])[:40]
        code, _ = await _Gw(d, gid).get("text_sensor", SENSORS["job"], cfg)
        if code != 200:
            return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=400)
        _saved(d)[gid] = cfg
        await d.save()
        _cache.pop(gid, None)
        return self.json({"ok": True})


class DaliGatewayView(HomeAssistantView):
    url = "/api/hatismart/dali/{gid}"
    name = "api:hatismart:dali_gateway"

    async def get(self, request, gid: str):
        from . import _data

        d = _data(request)
        gw = _Gw(d, gid) if d is not None and ID.match(gid) else None
        if gw is None or not gw.ready:
            return self.json({"error": "not_configured"}, status_code=404)
        hit = _cache.get(gid)
        if hit and time.time() - hit[0] < 0.8:   # nhiều điện thoại cùng mở: dùng chung một lần đọc
            return self.json(hit[1])
        async with _lock(gid):
            raw = {}
            for k, name in SENSORS.items():
                code, v = await gw.get("text_sensor", name)
                if code in (0, 401) and k == "devices":
                    return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
                raw[k] = v
        out = _parse(raw)
        _cache[gid] = (time.time(), out)
        return self.json(out)


class DaliCommandView(HomeAssistantView):
    url = "/api/hatismart/dali/{gid}/cmd"
    name = "api:hatismart:dali_cmd"

    async def post(self, request, gid: str):
        from . import _data, _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        gw = _Gw(d, gid) if d is not None and ID.match(gid) else None
        if gw is None or not gw.ready:
            return self.json({"error": "not_configured"}, status_code=404)
        line = " ".join(str((await request.json()).get("cmd") or "").split())
        if not CMD.match(line) or line.split(" ")[0] not in VERBS:
            return self.json({"error": "bad_command"}, status_code=400)
        async with _lock(gid):
            code = await gw.cmd(line)
        _cache.pop(gid, None)
        if code != 200:
            return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
        return self.json({"ok": True})


VIEWS = [DaliListView, DaliGatewayView, DaliCommandView]
