"""Hatismart DALI Gateway (firmware ESPHome hatismart.dali_gateway): cài đặt bóng từ app.

Gateway chỉ nhận lệnh cài đặt (quét, nháy, nhóm, thông số…) qua trang web của nó (ô "DALI Cmd"), MQTT chỉ
bật/tắt đèn → máy chủ gọi trang web của gateway trong LAN, app gọi máy chủ (dùng được cả khi ở xa).

GET  /api/hatismart/dali[?scan=1]      → gateway đã lưu + gateway đang chạy trong LAN; found = đang chạy (mDNS, hoặc trang web trả lời),
                                          không dựa vào tin MQTT giữ lại; scan=1: quét mDNS lại ngay, bỏ bộ nhớ 30 s
POST /api/hatismart/dali               → lưu tài khoản trang web gateway {id, ip, password = mã kích hoạt[, username]} (thử đăng nhập trước)
GET  /api/hatismart/dali/<id>          → bóng, việc đang chạy, thông số bóng vừa đọc, tên nhóm, cảnh báo
DELETE /api/hatismart/dali/<id>        → ngắt kết nối: xoá tài khoản gateway đã lưu trên máy chủ
GET/POST /api/hatismart/dali/<id>/net  → kết nối MQTT / KNX của gateway (chỉ quản trị; không trả mật khẩu MQTT)
GET  /api/hatismart/dali/<id>/data?parts=scenes,sched,care,nodes,knx,sys → cảnh, lịch+HCL, theo dõi bóng, nút nhấn, KNX, hệ thống
POST /api/hatismart/dali/<id>/entity   → công tắc DT8 / hỏi trạng thái / kiểm tra echo, fade, RGBW, khởi động lại
POST /api/hatismart/dali/<id>/ota      → thân = file .bin → nạp firmware cho gateway
POST /api/hatismart/dali/<id>/cmd      → {cmds: [...]} nhiều lệnh theo thứ tự, hoặc {cmd: "scan" | "identify 3" | "group add 3 1" | "level b 254" | …} (chỉ lệnh trong DANH SÁCH)
Mã kích hoạt = mật khẩu trang web gateway (in trên tem); chỉ có mã → thử tài khoản "admin" rồi "hatismart", cả mã viết hoa.
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
# lệnh cho phép từ app (đủ như trang web gateway; MQTT đi đường /net riêng)
VERBS = {"scan", "commission", "stop", "identify", "level", "ct", "rgbw", "name", "group", "groups", "gname",
         "params", "set", "addr", "forget", "dupfix", "replace", "scene", "fade",
         # như trang web gateway: lịch, HCL, vị trí, theo dõi bóng, khi có điện lại, nút nhấn ESP-NOW, KNX, frame thô
         "sched", "hcl", "circ", "geo", "care", "poweron", "node", "nodes", "knx", "raw"}
CMD = re.compile(r"^[^\x00-\x1f#]{1,110}$")
SENSORS = {"devices": "DALI Devices", "job": "DALI Job", "params": "DALI Params", "nodes": "DALI Nodes", "alerts": "DALI Alerts"}

_locks: dict[str, asyncio.Lock] = {}
_cache: dict[str, tuple[float, dict]] = {}
_found: tuple[float, dict] = (0.0, {})
_mdns: tuple[float, dict] = (0.0, {})
_probe: dict[str, tuple[float, bool]] = {}
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


async def _scan_mdns(hass: HomeAssistant, fresh: bool = False) -> dict:
    """Gateway đang chạy trong LAN qua mDNS _esphomelib._tcp (kể cả gateway chưa nối MQTT): {mac: {ip, name, version}}"""
    global _mdns
    if not fresh and time.time() - _mdns[0] < 30:
        return _mdns[1]
    try:
        from homeassistant.components import zeroconf as hz
        from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
    except ImportError:
        return {}
    stype = "_esphomelib._tcp.local."
    names: set[str] = set()

    def seen(zeroconf, service_type, name, state_change):  # noqa: ARG001
        names.add(name)

    out: dict = {}
    try:
        aiozc = await hz.async_get_async_instance(hass)
        browser = AsyncServiceBrowser(aiozc.zeroconf, stype, handlers=[seen])
        await asyncio.sleep(2.5)
        await browser.async_cancel()
        infos = [AsyncServiceInfo(stype, n) for n in names]
        oks = await asyncio.gather(*(i.async_request(aiozc.zeroconf, 1500) for i in infos))
        for info, ok in zip(infos, oks):
            if not ok:
                continue
            props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore") for k, v in (info.properties or {}).items()}
            mac = props.get("mac", "").lower().replace(":", "")
            ips = info.parsed_addresses()
            if props.get("project_name", "").startswith(PROJECT) and ID.match(mac) and ips:
                out[mac] = {"ip": ips[0], "name": props.get("friendly_name") or n.split(".")[0],
                            "version": props.get("project_version", "")}
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("DALI mDNS: %s", e)
    _mdns = (time.time(), out)
    return out


async def _alive(d, ip: str) -> bool:
    """Gateway còn trả lời trang web không (401 cũng tính là sống); nhớ 15 s"""
    t, ok = _probe.get(ip, (0.0, False))
    if time.time() - t < 15:
        return ok
    try:
        async with d.session().get(f"http://{ip}/", timeout=ClientTimeout(total=2)) as r:
            ok = r.status < 500
    except Exception:  # noqa: BLE001
        ok = False
    _probe[ip] = (time.time(), ok)
    return ok


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

    async def act(self, domain: str, name: str, action: str, value=None) -> int:
        """Bấm/đặt một ô trên trang web gateway: POST /<domain>/<tên>/<action>[?value=]"""
        url = f"http://{self.cfg['ip']}/{domain}/{quote(name)}/{action}"
        try:
            async with self.d.session().post(url, params=None if value is None else {"value": str(value)}, auth=self._auth(),
                                             timeout=ClientTimeout(total=4)) as r:
                return r.status
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("DALI %s: %s", url, e)
            return 0

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
        hass = request.app["hass"]
        # tin MQTT giữ lại (retained) vẫn còn khi gateway đã tắt → chỉ dùng để biết tên/IP, không coi là "đang chạy"
        mqtt_seen = await _discover(hass)
        live = await _scan_mdns(hass, fresh=bool(request.query.get("scan")))
        saved = _saved(d)
        changed = False
        for gid, g in live.items():   # gateway đổi IP (DHCP) → cập nhật địa chỉ đã lưu
            if gid in saved and g["ip"] and saved[gid].get("ip") != g["ip"]:
                saved[gid]["ip"] = g["ip"]
                changed = True
        if changed:
            await d.save()
        ids = sorted(set(live) | set(saved) | set(mqtt_seen))
        info = {gid: live.get(gid) or mqtt_seen.get(gid) or {} for gid in ids}
        ips = {gid: (live.get(gid) or {}).get("ip") or saved.get(gid, {}).get("ip") or info[gid].get("ip", "") for gid in ids}
        # mDNS đôi khi sót → gateway đã lưu mà mDNS không thấy thì gọi thử trang web của nó
        # …trừ khi IP đó đang thuộc gateway khác (MAC khác) thấy qua mDNS: gateway này đã tắt, IP được cấp cho máy khác
        taken = {g["ip"] for g in live.values()}
        probe = [gid for gid in ids if gid not in live and ips[gid] and HOST.match(ips[gid]) and ips[gid] not in taken]
        alive = dict(zip(probe, await asyncio.gather(*(_alive(d, ips[gid]) for gid in probe))))
        out = []
        for gid in ids:
            on, s = gid in live or alive.get(gid, False), saved.get(gid, {})
            if not s and not on:
                continue   # gateway lạ chỉ còn tin MQTT cũ, không chạy → bỏ
            out.append({"id": gid, "name": s.get("name") or info[gid].get("name") or "DALI Gateway", "ip": ips[gid],
                        "version": info[gid].get("version", ""), "found": on, "configured": bool(s.get("username"))})
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
            ip = ((await _discover(request.app["hass"])).get(gid) or _mdns[1].get(gid) or {}).get("ip", "")
        if not ID.match(gid) or not HOST.match(ip):
            return self.json({"error": "bad_request"}, status_code=400)
        user, pw = str(body.get("username") or ""), str(body.get("password") or "")
        # Chỉ có mã kích hoạt → thử "admin" (firmware hiện tại) rồi "hatismart"; thử cả mã viết hoa
        users = [user] if user else ["admin", "hatismart"]
        pws = list(dict.fromkeys([pw, pw.strip().upper()])) if pw else [""]
        code, cfg = 0, {}
        for u in users:
            for p in pws:
                cfg = {"ip": ip, "username": u, "password": p}
                code, _ = await _Gw(d, gid).get("text_sensor", SENSORS["job"], cfg)
                if code != 401:
                    break
            if code != 401:
                break
        if code != 200:
            return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=400)
        if body.get("name"):
            cfg["name"] = str(body["name"])[:40]
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

    async def delete(self, request, gid: str):
        """Ngắt kết nối: máy chủ quên tài khoản gateway (gateway vẫn hiện nếu còn trong mạng)"""
        from . import _data, _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        if d is None or not ID.match(gid):
            return self.json({"error": "bad_request"}, status_code=400)
        _saved(d).pop(gid, None)
        await d.save()
        _cache.pop(gid, None)
        return self.json({"ok": True})


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
        body = await request.json()
        # {cmd: "…"} hoặc {cmds: ["…", …]} — nhiều lệnh chạy đúng thứ tự (lưu cảnh, khôi phục sao lưu…)
        raw = body.get("cmds") if isinstance(body.get("cmds"), list) else [body.get("cmd")]
        lines = [" ".join(str(x or "").split()) for x in raw[:200]]
        if not lines or any(not CMD.match(x) or x.split(" ")[0] not in VERBS for x in lines):
            return self.json({"error": "bad_command"}, status_code=400)
        async with _lock(gid):
            for i, line in enumerate(lines):
                code = await gw.cmd(line)
                if code != 200:
                    _cache.pop(gid, None)
                    return self.json({"error": "wrong_auth" if code == 401 else "unreachable", "done": i}, status_code=502)
                if len(lines) > 1:
                    await asyncio.sleep(0.05)
        _cache.pop(gid, None)
        return self.json({"ok": True})


IND = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{1,3}$")
TOPIC = re.compile(r"^[^/#+\s][^#+\s]{0,126}$")
# ô cài MQTT trên trang web gateway (giống hộp "Cài đặt MQTT" của trang web)
NET = {"enabled": ("switch", "MQTT Enabled"), "broker": ("text", "MQTT Broker"), "port": ("number", "MQTT Port"),
       "username": ("text", "MQTT Username"), "topic": ("text", "MQTT Topic Prefix"), "connected": ("binary_sensor", "MQTT Connected")}


class DaliNetView(HomeAssistantView):
    """Kết nối MQTT và KNX của chính gateway (hộp "Cài đặt MQTT" / "Cài đặt KNX" trên trang web gateway)"""
    url = "/api/hatismart/dali/{gid}/net"
    name = "api:hatismart:dali_net"

    @staticmethod
    def _gw(request, gid: str):
        from . import _data

        d = _data(request)
        gw = _Gw(d, gid) if d is not None and ID.match(gid) else None
        return gw if gw is not None and gw.ready else None

    async def get(self, request, gid: str):
        from . import _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        gw = self._gw(request, gid)
        if gw is None:
            return self.json({"error": "not_configured"}, status_code=404)
        mqtt: dict = {}
        async with _lock(gid):
            for k, (dom, name) in NET.items():
                code, v = await gw.get(dom, name)
                if code in (0, 401):
                    return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
                val = (v or {}).get("value")
                mqtt[k] = bool(val) if dom in ("switch", "binary_sensor") else (int(float(val or 0)) if dom == "number" else str(val or ""))
            _, kv = await gw.get("text_sensor", "DALI KNX")
        try:
            knx = json.loads((kv or {}).get("value") or "{}")
        except ValueError:
            knx = {}
        knx.pop("ch", None)   # bảng kênh GA: chưa đưa lên app
        return self.json({"mqtt": mqtt, "knx": knx})

    async def post(self, request, gid: str):
        """{mqtt: {enabled, broker, port, username, password?, topic}} → lưu rồi gateway khởi động lại
        {knx: {on, ip, port, ind}} → lưu và kết nối lại KNX;  {knx_scan: true} → tìm KNX IP interface trong mạng"""
        from . import _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        gw = self._gw(request, gid)
        if gw is None:
            return self.json({"error": "not_configured"}, status_code=404)
        body = await request.json()
        steps: list = []
        if isinstance(m := body.get("mqtt"), dict):
            broker, topic = str(m.get("broker") or "").strip(), str(m.get("topic") or "").strip().rstrip("/")
            try:
                port = int(m.get("port") or 1883)
            except (TypeError, ValueError):
                port = 0
            user, pw = str(m.get("username") or ""), str(m.get("password") or "")
            if m.get("enabled") and not broker:
                return self.json({"error": "bad_broker"}, status_code=400)
            if not (1 <= port <= 65535) or len(broker) > 63 or not HOST.match(broker or "x") \
                    or not TOPIC.match(topic) or len(user) > 63 or len(pw) > 63:
                return self.json({"error": "bad_request"}, status_code=400)
            steps += [("text", "MQTT Broker", "set", broker), ("number", "MQTT Port", "set", port),
                      ("text", "MQTT Username", "set", user), ("text", "MQTT Topic Prefix", "set", topic)]
            if pw:   # để trống = giữ mật khẩu cũ
                steps.append(("text", "MQTT Password", "set", pw))
            steps += [("switch", "MQTT Enabled", "turn_on" if m.get("enabled") else "turn_off", None),
                      ("button", "Apply MQTT Settings", "press", None)]   # gateway khởi động lại sau 1,5 s
        if isinstance(k := body.get("knx"), dict):
            ip, ind = str(k.get("ip") or "").strip(), str(k.get("ind") or "1.1.250").strip()
            try:
                port = int(k.get("port") or 3671)
            except (TypeError, ValueError):
                port = 0
            if ip and (not HOST.match(ip) or len(ip) > 23 or not (1 <= port <= 65535) or not IND.match(ind)):
                return self.json({"error": "bad_request"}, status_code=400)
            if k.get("on") and not ip:
                return self.json({"error": "bad_knx_ip"}, status_code=400)
            if ip:
                steps.append(("cmd", f"knx ip {ip} {port} {ind}"))
            steps.append(("cmd", "knx on" if k.get("on") else "knx off"))
        if body.get("knx_scan"):
            steps.append(("cmd", "knx scan"))
        if not steps:
            return self.json({"error": "bad_request"}, status_code=400)
        async with _lock(gid):
            for st in steps:
                code = await (gw.cmd(st[1]) if st[0] == "cmd" else gw.act(*st))
                if code != 200:
                    return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
        _cache.pop(gid, None)
        return self.json({"ok": True})


# phần dữ liệu thêm: tên → (ô JSON trên gateway)
PARTS = {"scenes": "DALI Scenes", "sched": "DALI Sched", "care": "DALI Care", "nodes": "DALI Nodes", "knx": "DALI KNX"}
# thông tin hệ thống (thẻ Trạng thái + CPU trên trang web)
SYS = [("text_sensor", "IP Address"), ("sensor", "Uptime"), ("text_sensor", "Firmware"), ("sensor", "Free Heap"),
       ("sensor", "Heap Max Block"), ("sensor", "Loop Time"), ("sensor", "CPU Frequency"), ("text_sensor", "Reset Reason"),
       ("text_sensor", "Chip Info"), ("sensor", "DALI Collisions"), ("sensor", "DALI TX Count"), ("text_sensor", "DALI Last Frame"),
       ("switch", "DT8 Color Temp"), ("switch", "DALI Status Polling"), ("switch", "DALI Echo Check"), ("select", "DALI Fade Time"),
       ("binary_sensor", "MQTT Connected"), ("text_sensor", "KNX State"), ("text_sensor", "Button Last"), ("sensor", "ESP-NOW Nodes")]
# ô được phép bấm/đặt từ app
ENTITY = {("switch", "DT8 Color Temp"), ("switch", "DALI Status Polling"), ("switch", "DALI Echo Check"),
          ("select", "DALI Fade Time"), ("button", "Restart Controller"), ("light", "DALI Broadcast RGBW")}


class DaliDataView(HomeAssistantView):
    """GET ?parts=scenes,sched,care,nodes,knx,sys → dữ liệu các thẻ còn lại của trang web gateway"""
    url = "/api/hatismart/dali/{gid}/data"
    name = "api:hatismart:dali_data"

    async def get(self, request, gid: str):
        gw = DaliNetView._gw(request, gid)
        if gw is None:
            return self.json({"error": "not_configured"}, status_code=404)
        want = [p for p in str(request.query.get("parts") or "").split(",") if p in PARTS or p == "sys"]
        out: dict = {}
        async with _lock(gid):
            for p in want:
                if p == "sys":
                    sys: dict = {}
                    for dom, name in SYS:
                        code, v = await gw.get(dom, name)
                        if code in (0, 401):
                            return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
                        sys[name] = (v or {}).get("value")
                    out["sys"] = sys
                    continue
                code, v = await gw.get("text_sensor", PARTS[p])
                if code in (0, 401):
                    return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
                try:
                    out[p] = json.loads((v or {}).get("value") or "null")
                except ValueError:
                    out[p] = None
        return self.json(out)


class DaliEntityView(HomeAssistantView):
    """POST {domain, name, action, value?, params?} → bấm/đặt một ô của gateway (chỉ các ô trong ENTITY)"""
    url = "/api/hatismart/dali/{gid}/entity"
    name = "api:hatismart:dali_entity"

    async def post(self, request, gid: str):
        from . import _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        gw = DaliNetView._gw(request, gid)
        if gw is None:
            return self.json({"error": "not_configured"}, status_code=404)
        b = await request.json()
        dom, name, action = str(b.get("domain") or ""), str(b.get("name") or ""), str(b.get("action") or "")
        if (dom, name) not in ENTITY or not re.match(r"^(turn_on|turn_off|press|set)$", action):
            return self.json({"error": "bad_request"}, status_code=400)
        params = {k: str(v) for k, v in (b.get("params") or {}).items() if re.match(r"^\w{1,16}$", str(k))}
        if "value" in b:
            params["option" if dom == "select" else "value"] = str(b["value"])
        url = f"http://{gw.cfg['ip']}/{dom}/{quote(name)}/{action}"
        async with _lock(gid):
            try:
                async with gw.d.session().post(url, params=params or None, auth=gw._auth(), timeout=ClientTimeout(total=4)) as r:
                    code = r.status
            except Exception:  # noqa: BLE001
                code = 0
        if code != 200:
            return self.json({"error": "wrong_auth" if code == 401 else "unreachable"}, status_code=502)
        return self.json({"ok": True})


class DaliOtaView(HomeAssistantView):
    """POST thân = file .bin firmware → chuyển cho gateway (/update, như nút Nạp firmware trên trang web)"""
    url = "/api/hatismart/dali/{gid}/ota"
    name = "api:hatismart:dali_ota"

    async def post(self, request, gid: str):
        from aiohttp import FormData

        from . import _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        gw = DaliNetView._gw(request, gid)
        if gw is None:
            return self.json({"error": "not_configured"}, status_code=404)
        data = await request.read()
        # ảnh ESP32: byte đầu 0xE9, tối đa 8 MB
        if len(data) < 65536 or len(data) > 8 * 1024 * 1024 or data[0] != 0xE9:
            return self.json({"error": "bad_firmware"}, status_code=400)
        form = FormData()
        form.add_field("update", data, filename="firmware.bin", content_type="application/octet-stream")
        async with _lock(gid):
            try:
                async with gw.d.session().post(f"http://{gw.cfg['ip']}/update", data=form, auth=gw._auth(),
                                               timeout=ClientTimeout(total=180)) as r:
                    code, text = r.status, (await r.text())[:200]
            except Exception as e:  # noqa: BLE001
                code, text = 0, str(e)[:200]
        _cache.pop(gid, None)
        if code != 200:
            return self.json({"error": "wrong_auth" if code == 401 else "ota_failed", "detail": text}, status_code=502)
        return self.json({"ok": True})


VIEWS = [DaliListView, DaliGatewayView, DaliCommandView, DaliNetView, DaliDataView, DaliEntityView, DaliOtaView]
