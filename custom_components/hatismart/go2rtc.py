"""go2rtc — vệ tinh camera cho nhà không có Frigate.

go2rtc chạy độc lập (Docker/Portainer, cổng 1984) cạnh Home Assistant. App xem và quản lý camera
qua /api/hatismart/go2rtc/..., dùng xác thực của Home Assistant, nên trong nhà hay qua 4G đều một đường
và không phải mở go2rtc ra Internet.

An toàn: go2rtc tự tạo luồng từ bất kỳ `src` nào nó nhận (kể cả `exec:` chạy lệnh trên máy), nên ở đây
- xem/ảnh chỉ nhận tên luồng đã có;
- thêm/sửa luồng chỉ cho quản trị và chỉ nhận nguồn camera (rtsp, rtsps, rtmp, http(s), onvif);
- thành viên thấy nguồn đã che mật khẩu.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from aiohttp import BasicAuth, ClientTimeout, WSMsgType, web

from homeassistant.components.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SOURCE_RE = re.compile(r"^(rtsps?|rtmp|https?|onvif)://", re.IGNORECASE)
# tin app được gửi vào go2rtc qua WebSocket (xem video), không cho các lệnh khác
WS_TYPES = {"mse", "webrtc", "webrtc/offer", "webrtc/candidate", "mp4", "mjpeg"}
LOCAL_CANDIDATES = ("http://127.0.0.1:1984", "http://localhost:1984")


def mask(url: str) -> str:
    """rtsp://user:pass@host/... → rtsp://user:***@host/... (mật khẩu có thể chứa @)"""
    p = url.find("://")
    at = url.rfind("@")
    if p < 0 or at < p:
        return url
    user = url[p + 3:at].split(":", 1)[0]
    return f"{url[:p + 3]}{user}:***@{url[at + 1:]}"


def _cfg(d) -> dict:
    return d.config.get("go2rtc") or {}


def _auth(g: dict) -> BasicAuth | None:
    return BasicAuth(g["username"], g.get("password", "")) if g.get("username") else None


async def probe(d, base: str, g: dict | None = None) -> dict | None:
    """go2rtc trả /api = {"version": ...}; 401 = có đặt tài khoản"""
    try:
        async with d.session().get(base + "/api", auth=_auth(g or {}), timeout=ClientTimeout(total=1.5)) as r:
            if r.status == 401:
                return {"url": base, "version": "", "needsLogin": True}
            if r.status == 200:
                j = await r.json(content_type=None)
                if isinstance(j, dict) and "version" in j:
                    return {"url": base, "version": str(j["version"]), "needsLogin": False}
    except Exception:  # noqa: BLE001
        pass
    return None


async def auto_setup(d) -> None:
    """Chưa cấu hình → thử go2rtc ngay trên máy Home Assistant (cài chuẩn: network_mode host)"""
    if _cfg(d).get("url"):
        return
    for base in LOCAL_CANDIDATES:
        r = await probe(d, base)
        if r and not r["needsLogin"]:
            d.config["go2rtc"] = {"url": base, "username": "", "password": "", "auto": True}
            await d.save()
            _LOGGER.info("Tự kết nối go2rtc %s (bản %s)", base, r["version"])
            return


async def _streams(d) -> dict | None:
    g = _cfg(d)
    if not g.get("url"):
        return None
    async with d.session().get(g["url"] + "/api/streams", auth=_auth(g), timeout=ClientTimeout(total=6)) as r:
        if r.status != 200:
            raise web.HTTPBadGateway(text="go2rtc_error")
        j = await r.json(content_type=None)
        return j if isinstance(j, dict) else {}


def _sources(stream: dict) -> list[str]:
    return [p.get("url", "") for p in (stream or {}).get("producers") or [] if p.get("url")]


async def _exists(d, name: str) -> bool:
    if not NAME_RE.match(name or ""):
        return False
    try:
        s = await _streams(d)
    except Exception:  # noqa: BLE001
        return False
    return bool(s) and name in s


class Go2rtcScanView(HomeAssistantView):
    url = "/api/hatismart/setup/go2rtc/scan"
    name = "api:hatismart:go2rtc_scan"

    async def get(self, request):
        from . import _data, _is_admin, _local_prefixes

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        hass = request.app["hass"]
        targets = list(LOCAL_CANDIDATES[:1]) + [
            f"http://{p}.{i}:1984" for p in await _local_prefixes(hass) for i in range(1, 255)
        ]
        sem = asyncio.Semaphore(96)

        async def one(t):
            async with sem:
                return await probe(d, t)

        found = [r for r in await asyncio.gather(*(one(t) for t in targets)) if r]
        return self.json({"found": found})


class Go2rtcSetupView(HomeAssistantView):
    url = "/api/hatismart/setup/go2rtc"
    name = "api:hatismart:go2rtc_setup"

    async def post(self, request):
        from . import _data, _is_admin, _user_name

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        d = _data(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        url = str(body.get("url") or "").strip().rstrip("/")
        if not url:
            d.config.pop("go2rtc", None)
            await d.save()
            return self.json({"ok": True})
        if not re.match(r"^https?://", url):
            url = "http://" + url
        if not re.search(r":\d+$", url):
            url += ":1984"
        g = {"url": url, "username": str(body.get("username") or ""), "password": str(body.get("password") or "")}
        r = await probe(d, url, g)
        if r is None:
            return self.json({"error": "unreachable", "message": "Không kết nối được go2rtc ở địa chỉ này"}, status_code=400)
        if r["needsLogin"]:
            return self.json({"error": "login_failed", "message": "Sai tài khoản go2rtc"}, status_code=400)
        d.config["go2rtc"] = g
        await d.save()
        _LOGGER.info("%s đặt go2rtc = %s", _user_name(request), url)
        return self.json({"ok": True, "url": url, "version": r["version"]})


class Go2rtcStreamsView(HomeAssistantView):
    """GET: {"streams": {tên: [nguồn]}} · PUT {name, sources} · DELETE ?name= (quản trị)"""

    url = "/api/hatismart/go2rtc/streams"
    name = "api:hatismart:go2rtc_streams"

    async def get(self, request):
        from . import _data, _is_admin

        d = _data(request)
        if d is None or not _cfg(d).get("url"):
            return self.json({"error": "not_configured"}, status_code=404)
        try:
            s = await _streams(d) or {}
        except Exception:  # noqa: BLE001
            return self.json({"error": "satellite_unreachable", "message": "Không kết nối được go2rtc"}, status_code=502)
        admin = _is_admin(request)
        out = {n: [u if admin else mask(u) for u in _sources(v)] for n, v in s.items()}
        return self.json({"streams": out})

    async def put(self, request):
        from . import _data, _is_admin, _user_name

        d = _data(request)
        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        g = _cfg(d) if d else {}
        if not g.get("url"):
            return self.json({"error": "not_configured"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        name = str(body.get("name") or "")
        sources = [str(s).strip() for s in body.get("sources") or [] if str(s).strip()]
        if not NAME_RE.match(name):
            return self.json({"error": "bad_name", "message": "Tên luồng chỉ gồm chữ không dấu, số, _ và -"}, status_code=400)
        if not sources or not all(SOURCE_RE.match(s) for s in sources):
            return self.json({"error": "bad_source", "message": "Nguồn phải là rtsp://, rtsps://, rtmp://, http(s):// hoặc onvif://"}, status_code=400)
        params = [("name", name)] + [("src", s) for s in sources]
        try:
            async with d.session().put(g["url"] + "/api/streams", params=params, auth=_auth(g),
                                       timeout=ClientTimeout(total=10)) as r:
                txt = (await r.text()).strip()
                if r.status != 200:
                    return self.json({"error": "go2rtc_error", "message": txt[:200]}, status_code=400)
        except Exception:  # noqa: BLE001
            return self.json({"error": "satellite_unreachable", "message": "Không kết nối được go2rtc"}, status_code=502)
        _LOGGER.info("%s lưu luồng go2rtc %s (%d nguồn)", _user_name(request), name, len(sources))
        return self.json({"ok": True})

    async def delete(self, request):
        from . import _data, _is_admin, _user_name

        d = _data(request)
        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        g = _cfg(d) if d else {}
        name = request.query.get("name", "")
        if not g.get("url") or not NAME_RE.match(name):
            return self.json({"error": "bad_request"}, status_code=400)
        try:
            async with d.session().delete(g["url"] + "/api/streams", params={"src": name}, auth=_auth(g),
                                          timeout=ClientTimeout(total=10)) as r:
                if r.status != 200:
                    return self.json({"error": "go2rtc_error", "message": (await r.text())[:200]}, status_code=400)
        except Exception:  # noqa: BLE001
            return self.json({"error": "satellite_unreachable", "message": "Không kết nối được go2rtc"}, status_code=502)
        _LOGGER.info("%s xoá luồng go2rtc %s", _user_name(request), name)
        return self.json({"ok": True})


class Go2rtcFrameView(HomeAssistantView):
    """Ảnh mới nhất của một luồng (?src=tên)"""

    url = "/api/hatismart/go2rtc/frame.jpeg"
    name = "api:hatismart:go2rtc_frame"

    async def get(self, request):
        from . import _data

        d = _data(request)
        src = request.query.get("src", "")
        if d is None or not await _exists(d, src):
            return self.json({"error": "unknown_stream"}, status_code=404)
        g = _cfg(d)
        try:
            async with d.session().get(g["url"] + "/api/frame.jpeg", params={"src": src}, auth=_auth(g),
                                       timeout=ClientTimeout(total=15)) as r:
                content = await r.read()
                return web.Response(body=content, status=r.status, content_type=r.content_type or "image/jpeg",
                                    headers={"Cache-Control": "no-store"})
        except Exception:  # noqa: BLE001
            return self.json({"error": "satellite_unreachable"}, status_code=502)


class Go2rtcWSView(HomeAssistantView):
    """Xem video: nối WebSocket của app với /api/ws?src=tên của go2rtc (MSE / WebRTC).

    Trang trong app không gắn được header token → app xin đường ký sẵn (auth/sign_path, có cả ?src=),
    Home Assistant kiểm chữ ký trước khi vào đây.
    """

    url = "/api/hatismart/go2rtc/ws"
    name = "api:hatismart:go2rtc_ws"

    async def get(self, request):
        from . import _data

        d = _data(request)
        src = request.query.get("src", "")
        if d is None or not await _exists(d, src):
            return self.json({"error": "unknown_stream"}, status_code=404)
        g = _cfg(d)
        client = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await client.prepare(request)
        up_url = re.sub(r"^http", "ws", g["url"]) + "/api/ws"
        try:
            async with d.session().ws_connect(up_url, params={"src": src}, auth=_auth(g), heartbeat=30,
                                              max_msg_size=0) as up:

                async def to_go2rtc():
                    async for m in client:
                        if m.type != WSMsgType.TEXT:
                            break
                        try:
                            kind = json.loads(m.data).get("type")
                        except Exception:  # noqa: BLE001
                            kind = None
                        if kind in WS_TYPES:
                            await up.send_str(m.data)

                async def to_app():
                    async for m in up:
                        if m.type == WSMsgType.BINARY:
                            await client.send_bytes(m.data)
                        elif m.type == WSMsgType.TEXT:
                            await client.send_str(m.data)
                        else:
                            break

                async def guard(coro):
                    # app đóng trang giữa chừng → ghi vào kết nối đang đóng báo lỗi, coi như kết thúc bình thường
                    try:
                        await coro
                    except Exception:  # noqa: BLE001
                        pass

                tasks = [asyncio.ensure_future(guard(to_go2rtc())), asyncio.ensure_future(guard(to_app()))]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("go2rtc ws %s: %s", src, e)
            if not client.closed:
                await client.send_str('{"type":"error","value":"satellite_unreachable"}')
        if not client.closed:
            await client.close()
        return client


VIEWS = [Go2rtcScanView, Go2rtcSetupView, Go2rtcStreamsView, Go2rtcFrameView, Go2rtcWSView]
