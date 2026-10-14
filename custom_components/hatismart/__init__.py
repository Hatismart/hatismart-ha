"""Hatismart — cầu nối giữa app Hatismart Pro và các vệ tinh trong LAN (Frigate…).

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
    ("PUT", re.compile(r"^/api/config/set$"), True),
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
    if not hass.data.get(f"{DOMAIN}_views"):
        hass.http.register_view(InfoView())
        hass.http.register_view(FrigateScanView())
        hass.http.register_view(FrigateSetupView())
        hass.http.register_view(FrigateProxyView())
        hass.data[f"{DOMAIN}_views"] = True
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.pop(DOMAIN, None)
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
            "services": {"frigate": bool(d.frigate.get("url"))},
        }
        if _is_admin(request):
            out["frigate_url"] = d.frigate.get("url", "")
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
        f = {"url": url, "username": str(body.get("username") or ""), "password": str(body.get("password") or "")}
        headers = await d.frigate_headers(f)
        try:
            async with d.session().get(url + "/api/config", headers=headers, timeout=ClientTimeout(total=6)) as r:
                if r.status == 401:
                    return self.json({"error": "login_failed", "message": "Sai tài khoản Frigate"}, status_code=400)
                if r.status != 200:
                    return self.json({"error": "bad_response", "message": "Địa chỉ này không phải Frigate"}, status_code=400)
        except Exception:  # noqa: BLE001
            return self.json({"error": "unreachable", "message": "Không kết nối được địa chỉ này"}, status_code=400)
        d.config["frigate"] = f
        d.cookie = None
        await d.save()
        _LOGGER.info("%s đặt Frigate = %s", _user_name(request), url)
        return self.json({"ok": True, "url": url})


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
        if request.method != "GET":
            _LOGGER.info("%s %s frigate%s", _user_name(request), request.method, sub)
        body = await request.read() if request.method != "GET" else None
        headers = await d.frigate_headers()
        if body:
            headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
        try:
            async with d.session().request(
                request.method, d.frigate["url"] + sub, params=request.query, data=body,
                headers=headers, timeout=ClientTimeout(total=60),
            ) as r:
                if r.status == 401 and d.frigate.get("username"):
                    d.cookie = None
                content = await r.read()
                return web.Response(body=content, status=r.status,
                                    content_type=(r.content_type or "application/octet-stream"),
                                    headers={"Cache-Control": "no-store"})
        except Exception:  # noqa: BLE001
            return self.json({"error": "satellite_unreachable", "message": "Không kết nối được máy Frigate"}, status_code=502)

    async def get(self, request, path):
        return await self._handle(request, path)

    async def put(self, request, path):
        return await self._handle(request, path)

    async def post(self, request, path):
        return await self._handle(request, path)
