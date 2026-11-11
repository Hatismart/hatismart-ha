"""Hatismart — cầu nối giữa app Hatismart Pro và các vệ tinh trong LAN (Frigate, go2rtc…).

Mọi API nằm dưới /api/hatismart/..., dùng xác thực sẵn có của Home Assistant
(token của app). Lệnh sửa cấu hình chỉ dành cho admin.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket

from aiohttp import web, ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from . import ai, dali, go2rtc, mqtt_link, notify_log, snapshot
from .const import DOMAIN, STORAGE_KEY, VERSION

_LOGGER = logging.getLogger(__name__)

# (method, regex, cần admin)
FRIGATE_RULES = [
    ("GET", re.compile(r"^/api/config$"), False),
    ("GET", re.compile(r"^/api/stats$"), False),
    ("GET", re.compile(r"^/api/version$"), False),
    ("GET", re.compile(r"^/api/[\w-]+/latest\.(jpg|webp)$"), False),
    ("GET", re.compile(r"^/api/events$"), False),
    ("GET", re.compile(r"^/api/events/[\w.-]+/(snapshot\.jpg|thumbnail\.(jpg|webp)|clip\.mp4)$"), False),
    ("GET", re.compile(r"^/api/review$"), False),
    ("GET", re.compile(r"^/api/events/[\w.-]+/thumbnail\.jpg$"), False),
    ("GET", re.compile(r"^/api/[\w-]+/recordings$"), False),
    ("GET", re.compile(r"^/api/[\w-]+/recordings/summary$"), False),
    ("GET", re.compile(r"^/api/review/summary$"), False),
    ("GET", re.compile(r"^/api/recordings/storage$"), False),
    ("GET", re.compile(r"^/vod/[\w-]+/start/[\d.]+/end/[\d.]+/[\w./-]+$"), False),
    ("GET", re.compile(r"^/vod/event/[\w.-]+/[\w./-]+$"), False),
    # Khuôn mặt (Frigate ≥ 0.16): xem danh sách/ảnh cho mọi người, dạy/sửa/xoá chỉ quản trị
    ("GET", re.compile(r"^/api/faces$"), False),
    ("GET", re.compile(r"^/clips/faces/[\w-]+/[\w.-]+\.(webp|jpg|jpeg|png)$"), False),
    ("POST", re.compile(r"^/api/faces/[\w-]+/create$"), True),
    ("POST", re.compile(r"^/api/faces/[\w-]+/register$"), True),
    ("POST", re.compile(r"^/api/faces/train/[\w-]+/classify$"), True),
    ("POST", re.compile(r"^/api/faces/[\w-]+/delete$"), True),
    ("PUT", re.compile(r"^/api/faces/[\w-]+/rename$"), True),
    ("POST", re.compile(r"^/api/faces/reprocess$"), True),
    ("PUT", re.compile(r"^/api/config/set$"), True),
    # Thử luồng camera trước khi thêm vào máy AI (chỉ nhận đường rtsp://, kiểm ở _handle)
    ("GET", re.compile(r"^/api/ffprobe$"), True),
    ("POST", re.compile(r"^/api/restart$"), True),
]


class HatismartData:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store(hass, 1, STORAGE_KEY)
        self.config: dict = {}
        self.cookie: str | None = None
        self.cookie_at = 0.0

    async def load(self) -> None:
        self.config = await self.store.async_load() or {}

    async def save(self) -> None:
        await self.store.async_save(self.config)

    @property
    def frigate(self) -> dict:
        return self.config.get("frigate") or {}

    def session(self):
        # Frigate cổng 8971 dùng chứng chỉ tự ký trong LAN
        return async_get_clientsession(self.hass, verify_ssl=False)

    async def frigate_headers(self, f: dict | None = None) -> dict:
        f = f or self.frigate
        if not f.get("username"):
            return {}
        loop_time = self.hass.loop.time()
        if self.cookie and loop_time - self.cookie_at < 1800 and f is self.frigate:
            return {"Cookie": self.cookie}
        try:
            async with self.session().post(
                f["url"] + "/api/login",
                json={"user": f["username"], "password": f.get("password", "")},
                timeout=ClientTimeout(total=8),
            ) as r:
                c = r.cookies.get("frigate_token")
                if c is not None:
                    cookie = f"frigate_token={c.value}"
                    if f is self.frigate:
                        self.cookie, self.cookie_at = cookie, loop_time
                    return {"Cookie": cookie}
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Đăng nhập Frigate lỗi: %s", e)
        return {}


def _is_admin(request) -> bool:
    user = request.get("hass_user")
    return bool(user and user.is_admin)


def _user_name(request) -> str:
    user = request.get("hass_user")
    return user.name if user else "?"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = HatismartData(hass)
    await data.load()
    hass.data[DOMAIN] = data
    hass.async_create_task(go2rtc.auto_setup(data))
    await snapshot.async_setup(hass)
    await notify_log.async_setup(hass)
    if not hass.data.get(f"{DOMAIN}_views"):
        hass.http.register_view(InfoView())
        hass.http.register_view(FrigateScanView())
        hass.http.register_view(FrigateSetupView())
        hass.http.register_view(FrigateProxyView())
        hass.http.register_view(mqtt_link.FrigateMqttView())
        hass.http.register_view(snapshot.SnapshotView())
        hass.http.register_view(notify_log.NotificationsView())
        hass.http.register_view(notify_log.NotificationImageView())
        hass.http.register_view(ai.AIChatView())
        for view in go2rtc.VIEWS + dali.VIEWS:
            hass.http.register_view(view())
        hass.data[f"{DOMAIN}_views"] = True
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.pop(DOMAIN, None)
    snapshot.async_unload(hass)
    notify_log.async_unload(hass)
    return True


def _data(request) -> HatismartData | None:
    return request.app["hass"].data.get(DOMAIN)


class InfoView(HomeAssistantView):
    url = "/api/hatismart/info"
    name = "api:hatismart:info"

    async def get(self, request):
        d = _data(request)
        if d is None:
            return self.json({"error": "not_loaded"}, status_code=503)
        user = request.get("hass_user")
        out = {
            "version": VERSION,
            "user": {"name": user.name if user else "", "is_admin": _is_admin(request)},
            "services": {"frigate": bool(d.frigate.get("url")), "go2rtc": bool((d.config.get("go2rtc") or {}).get("url"))},
        }
        if _is_admin(request):
            out["frigate_url"] = d.frigate.get("url", "")
            out["go2rtc_url"] = (d.config.get("go2rtc") or {}).get("url", "")
        return self.json(out)


# ---------- Tìm Frigate trong LAN ----------
async def _probe(d: HatismartData, base: str) -> dict | None:
    try:
        async with d.session().get(base + "/api/version", timeout=ClientTimeout(total=1.2)) as r:
            txt = (await r.text()).strip()
            if r.status == 200 and re.match(r"^\d+\.\d+", txt):
                return {"url": base, "version": txt, "needsLogin": False}
            if r.status == 401 and base.endswith(":8971"):
                return {"url": base, "version": "", "needsLogin": True}
    except Exception:  # noqa: BLE001
        pass
    return None


async def _local_prefixes(hass: HomeAssistant) -> set[str]:
    prefixes: set[str] = set()
    try:
        from homeassistant.components import network

        for adapter in await network.async_get_adapters(hass):
            if not adapter.get("enabled"):
                continue
            for ip in adapter.get("ipv4", []):
                addr = ip.get("address", "")
                if addr and not ipaddress.ip_address(addr).is_loopback:
                    prefixes.add(".".join(addr.split(".")[:3]))
    except Exception:  # noqa: BLE001
        pass
    if not prefixes:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            prefixes.add(".".join(s.getsockname()[0].split(".")[:3]))
            s.close()
        except Exception:  # noqa: BLE001
            pass
    # bỏ dải mạng nội bộ của docker
    return {p for p in prefixes if not re.match(r"^(172\.(1[7-9]|2\d|3[01])|10\.88)\.", p + ".")}


class FrigateScanView(HomeAssistantView):
    url = "/api/hatismart/setup/frigate/scan"
    name = "api:hatismart:frigate_scan"

    async def get(self, request):
        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        hass = request.app["hass"]
        targets = [
            f"{'https' if port == 8971 else 'http'}://{p}.{i}:{port}"
            for p in await _local_prefixes(hass)
            for i in range(1, 255)
            for port in (5000, 8971)
        ]
        sem = asyncio.Semaphore(96)

        async def one(t):
            async with sem:
                return await _probe(d, t)

        found = [r for r in await asyncio.gather(*(one(t) for t in targets)) if r]
        return self.json({"found": found})


class FrigateSetupView(HomeAssistantView):
    url = "/api/hatismart/setup/frigate"
    name = "api:hatismart:frigate_setup"

    async def post(self, request):
        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        url = str(body.get("url") or "").strip().rstrip("/")
        if not url:
            d.config.pop("frigate", None)
            d.cookie = None
            await d.save()
            return self.json({"ok": True})
        if not re.match(r"^https?://", url):
            url = "http://" + url
        if not re.search(r":\d+$", url):
            url += ":5000"
        user, pw = str(body.get("username") or ""), str(body.get("password") or "")
        # Chỉ có mã kích hoạt (không tên đăng nhập) → tài khoản "hatismart" in trên tem, rồi "admin"; thử cả mã viết hoa
        users = [user] if user else (["hatismart", "admin"] if pw else [""])
        pws = list(dict.fromkeys([pw, pw.strip().upper()])) if pw else [""]
        f = None
        for u in users:
            for p in pws:
                cand = {"url": url, "username": u, "password": p}
                try:
                    async with d.session().get(url + "/api/config", headers=await d.frigate_headers(cand),
                                               timeout=ClientTimeout(total=6)) as r:
                        if r.status == 200:
                            f = cand
                            break
                        if r.status != 401:
                            return self.json({"error": "bad_response", "message": "Địa chỉ này không phải máy Hatismart AI"}, status_code=400)
                except Exception:  # noqa: BLE001
                    return self.json({"error": "unreachable", "message": "Không kết nối được địa chỉ này"}, status_code=400)
            if f:
                break
        if f is None:
            return self.json({"error": "login_failed", "message": "Sai mã kích hoạt" if pw else "Máy AI này cần mã kích hoạt"},
                             status_code=400)
        d.config["frigate"] = f
        d.cookie = None
        await d.save()
        _LOGGER.info("%s đặt Frigate = %s", _user_name(request), url)
        return self.json({"ok": True, "url": url})


def _scrub(v):
    """Che mật khẩu trong cấu hình Frigate cho người không phải quản trị (đường camera, MQTT…)."""
    if isinstance(v, dict):
        return {k: ("***" if k in ("password", "pass") and v[k] else _scrub(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub(x) for x in v]
    if isinstance(v, str) and "://" in v and "@" in v:
        return go2rtc.mask(v)
    return v


# ---------- Chuyển tiếp tới Frigate ----------
class FrigateProxyView(HomeAssistantView):
    url = "/api/hatismart/frigate/{path:.*}"
    name = "api:hatismart:frigate"

    async def _handle(self, request, path: str):
        d = _data(request)
        if d is None or not d.frigate.get("url"):
            return self.json({"error": "not_configured"}, status_code=404)
        sub = "/" + path
        rule = next((r for r in FRIGATE_RULES if r[0] == request.method and r[1].match(sub)), None)
        if rule is None:
            return self.json({"error": "not_allowed"}, status_code=403)
        if rule[2] and not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        if sub == "/api/ffprobe":
            paths = request.query.get("paths", "")
            if not paths or not all(re.match(r"^rtsps?://", x.strip()) for x in paths.split(",")):
                return self.json({"error": "bad_path", "message": "Chỉ thử được đường rtsp://"}, status_code=400)
        if request.method != "GET":
            _LOGGER.info("%s %s frigate%s", _user_name(request), request.method, sub)
        scrub = sub == "/api/config" and not _is_admin(request)
        body = await request.read() if request.method != "GET" else None
        headers = await d.frigate_headers()
        if body:
            headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]   # trình phát video cần tua theo byte
        try:
            r = await d.session().request(
                request.method, d.frigate["url"] + sub, params=request.query, data=body,
                headers=headers, timeout=ClientTimeout(total=None, sock_connect=10, sock_read=60),
            )
        except Exception:  # noqa: BLE001
            return self.json({"error": "satellite_unreachable", "message": "Không kết nối được máy chủ AI"}, status_code=502)
        try:
            if r.status == 401 and d.frigate.get("username"):
                d.cookie = None
            if scrub and r.status == 200:
                return self.json(_scrub(await r.json(content_type=None)))
            out_headers = {"Cache-Control": "no-store"}
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if h in r.headers:
                    out_headers[h] = r.headers[h]
            resp = web.StreamResponse(status=r.status, headers=out_headers)
            await resp.prepare(request)
            async for chunk in r.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            r.release()

    async def get(self, request, path):
        return await self._handle(request, path)

    async def put(self, request, path):
        return await self._handle(request, path)

    async def post(self, request, path):
        return await self._handle(request, path)
