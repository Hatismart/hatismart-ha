"""Thông báo Hatismart: gửi đẩy + lưu lịch sử trên máy chủ (mọi điện thoại trong nhà xem chung).

Dịch vụ hatismart.notify — kịch bản gọi một lần thay cho notify.mobile_app_*:
    title, message, targets [notify.mobile_app_…], category (an-ninh | camera | tu-dong-hoa | he-thong | cap-nhat | …),
    level (passive | active | time-sensitive | critical), camera, event_id, entity_id, url, icon, color,
    actions [{title, url}], extra {…}
  → camera: chụp ảnh ngay (ảnh sự kiện hoặc khung hình hiện tại, ~480p) lưu trên máy chủ để xem lại về sau;
  → gửi đẩy tới từng máy, link ảnh có chữ ký (snapshot.sign) cho phần mở rộng thông báo của app;
  → lưu bản ghi (tối đa 500 bản, 30 ngày). Trường lạ (loại thông báo mới) giữ nguyên trong `extra`.

GET /api/hatismart/notifications?before=<ts>&limit=  → danh sách mới nhất trước (mọi người dùng đã đăng nhập)
GET /api/hatismart/notifications/<id>/image          → ảnh đã lưu (cần đăng nhập)
DELETE /api/hatismart/notifications                  → xoá hết (quản trị)
"""
from __future__ import annotations

import logging
import os
import secrets
import time

import voluptuous as vol
from aiohttp import web, ClientTimeout

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from . import snapshot
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
SERVICE = "notify"
KEY = f"{DOMAIN}_notify_log"
MAX_ITEMS = 500
MAX_AGE = 30 * 86400
IMAGE_H = 480
FIELDS = ("title", "message", "category", "level", "camera", "event_id", "entity_id", "url", "icon", "color", "actions")


class NotifyLog:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store(hass, 1, f"{DOMAIN}.notifications")
        self.items: list[dict] = []
        self.dir = hass.config.path("hatismart", "notify_images")

    async def load(self) -> None:
        self.items = (await self.store.async_load() or {}).get("items", [])
        await self.hass.async_add_executor_job(os.makedirs, self.dir, 0o755, True)

    def image_path(self, nid: str) -> str:
        return os.path.join(self.dir, f"{nid}.jpg")

    def _prune(self) -> list[str]:
        cutoff = time.time() - MAX_AGE
        keep = [i for i in self.items if i["time"] >= cutoff][:MAX_ITEMS]
        gone = [i["id"] for i in self.items if i not in keep and i.get("image")]
        self.items = keep
        return gone

    def _remove_files(self, ids: list[str]) -> None:
        for nid in ids:
            try:
                os.remove(self.image_path(nid))
            except OSError:
                pass

    async def add(self, item: dict) -> None:
        self.items.insert(0, item)
        gone = self._prune()
        if gone:
            await self.hass.async_add_executor_job(self._remove_files, gone)
        self.store.async_delay_save(lambda: {"items": self.items}, 2)

    async def clear(self) -> None:
        ids = [i["id"] for i in self.items if i.get("image")]
        self.items = []
        await self.hass.async_add_executor_job(self._remove_files, ids)
        await self.store.async_save({"items": []})


def _shrink(body: bytes) -> bytes:
    """Ảnh sự kiện của Frigate luôn ở cỡ gốc (~270 KB) → thu về ~480p để điện thoại tải nhanh"""
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(body))
        if im.height <= IMAGE_H * 1.2:
            return body
        im.draft("RGB", (im.width * IMAGE_H // im.height, IMAGE_H))   # giải mã sẵn ở cỡ nhỏ: nhanh hơn nhiều
        im = im.convert("RGB")
        im.thumbnail((IMAGE_H * 4, IMAGE_H))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=75)
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return body


async def _capture(hass: HomeAssistant, d, log: NotifyLog, nid: str, camera: str, event_id: str) -> bool:
    """Lấy ảnh từ máy AI ngay lúc báo (link ảnh của Frigate có thể chưa có / bị xoá sau này)."""
    if d is None or not d.frigate.get("url"):
        return False
    base, headers = d.frigate["url"], await d.frigate_headers()
    paths = [f"/api/events/{event_id}/snapshot.jpg?bbox=1&quality=80"] if event_id else []
    paths.append(f"/api/{camera}/latest.jpg?height={IMAGE_H}&quality=75")
    for p in paths:
        try:
            async with d.session().get(base + p, headers=headers, timeout=ClientTimeout(total=4)) as r:
                if r.status == 200 and r.content_type.startswith("image/"):
                    body = await r.read()

                    def write() -> None:
                        data = _shrink(body)
                        with open(log.image_path(nid), "wb") as f:
                            f.write(data)
                    await hass.async_add_executor_job(write)
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def async_setup(hass: HomeAssistant) -> None:
    log = NotifyLog(hass)
    await log.load()
    hass.data[KEY] = log

    async def handle(call: ServiceCall) -> dict:
        data = dict(call.data)
        nid = secrets.token_hex(6)
        item = {"id": nid, "time": time.time(), "category": "he-thong", "level": "active"}
        item.update({k: data[k] for k in FIELDS if data.get(k) not in (None, "", [])})
        extra = {k: v for k, v in data.items() if k not in FIELDS and k not in ("targets", "base")}
        if extra:
            item["extra"] = extra
        d = hass.data.get(DOMAIN)
        camera, event_id = item.get("camera"), str(item.get("event_id") or "")
        if camera and snapshot.NAME.match(camera) and (not event_id or snapshot.NAME.match(event_id)):
            item["image"] = await _capture(hass, d, log, nid, camera, event_id)
        await log.add(item)

        # gửi đẩy: relay chỉ chuyển url, group, entity_id, image, actions → mã thông báo nằm trong url
        push = {"interruption-level": item["level"]}
        if item["level"] == "critical":
            push["sound"] = {"name": "default", "critical": 1, "volume": 1.0}
        pdata: dict = {"group": item["category"], "url": f"hatismart://inbox/{nid}", "push": push}
        if item.get("entity_id"):
            pdata["entity_id"] = item["entity_id"]
        if item.get("image") and d is not None:
            base = str(data.get("base") or "").rstrip("/")
            if not base:
                try:
                    base = get_url(hass, prefer_external=True)
                except NoURLAvailableError:
                    base = ""
            if base:
                pdata["image"] = await snapshot.sign_url(d, base, "n", nid)
        for t in data.get("targets") or []:
            dom, _, svc = str(t).partition(".")
            if dom != "notify" or not svc or not hass.services.has_service("notify", svc):
                continue
            hass.async_create_task(hass.services.async_call(
                "notify", svc, {"title": item.get("title") or "Hatismart", "message": item.get("message") or "", "data": pdata}))
        return {"id": nid}

    hass.services.async_register(
        DOMAIN, SERVICE, handle,
        schema=vol.Schema({
            vol.Optional("title"): str, vol.Optional("message"): str,
            vol.Optional("targets"): [str], vol.Optional("category"): str, vol.Optional("level"): str,
            vol.Optional("camera"): vol.Any(str, None), vol.Optional("event_id"): vol.Any(str, None),
            vol.Optional("entity_id"): vol.Any(str, None), vol.Optional("url"): vol.Any(str, None),
            vol.Optional("icon"): str, vol.Optional("color"): str,
            vol.Optional("actions"): [{vol.Required("title"): str, vol.Required("url"): str}],
            vol.Optional("base"): str,
        }, extra=vol.ALLOW_EXTRA),
        supports_response=SupportsResponse.OPTIONAL,
    )


def async_unload(hass: HomeAssistant) -> None:
    hass.services.async_remove(DOMAIN, SERVICE)
    hass.data.pop(KEY, None)


class NotificationsView(HomeAssistantView):
    url = "/api/hatismart/notifications"
    name = "api:hatismart:notifications"

    async def get(self, request):
        log: NotifyLog | None = request.app["hass"].data.get(KEY)
        if log is None:
            return self.json({"items": []})
        try:
            before = float(request.query.get("before", "inf"))
            limit = max(1, min(200, int(request.query.get("limit", "100"))))
        except ValueError:
            before, limit = float("inf"), 100
        items = [i for i in log.items if i["time"] < before][:limit]
        return self.json({"items": items, "more": len([i for i in log.items if i["time"] < before]) > limit})

    async def delete(self, request):
        from . import _is_admin

        if not _is_admin(request):
            return self.json({"error": "admin_only"}, status_code=403)
        log: NotifyLog | None = request.app["hass"].data.get(KEY)
        if log is not None:
            await log.clear()
        return self.json({"ok": True})


class NotificationImageView(HomeAssistantView):
    url = "/api/hatismart/notifications/{nid}/image"
    name = "api:hatismart:notification_image"

    async def get(self, request, nid: str):
        log: NotifyLog | None = request.app["hass"].data.get(KEY)
        if log is None or not snapshot.NAME.match(nid):
            return web.Response(status=404)
        return await image_response(request.app["hass"], log, nid)


async def image_response(hass: HomeAssistant, log: NotifyLog, nid: str):
    def read() -> bytes | None:
        try:
            with open(log.image_path(nid), "rb") as f:
                return f.read()
        except OSError:
            return None
    body = await hass.async_add_executor_job(read)
    if body is None:
        return web.Response(status=404)
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
