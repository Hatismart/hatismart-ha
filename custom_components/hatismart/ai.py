"""Trợ lý AI chạy ngay trong cầu Hatismart (giai đoạn thử, 11/2026).

App Hatismart Pro gửi câu hỏi + danh sách thiết bị người dùng được phép dùng → cầu gọi mô hình AI (cổng kiểu Anthropic
Messages: vilao.ai / OpenRouter / Anthropic) → trả về câu trả lời và các lệnh công cụ. App tự thực hiện lệnh qua HA với
quyền của chính người dùng rồi gửi kết quả về (vòng lặp nằm ở app, giống hàm đám mây aiChat).

POST /api/hatismart/ai/chat   (xác thực HA của app)
  Body: {context, history: "<chuỗi JSON>"|"", text? | toolResults?: [{tool_use_id, content, is_error?}], now?}
  Trả về: {history: "<chuỗi JSON>", reply, toolCalls: [{id, name, input}], usage, costUSD, model, ms, remaining}
GET  /api/hatismart/ai/chat   → {enabled, model} (app kiểm tra có dùng được không)

Cấu hình: file <thư mục cấu hình HA>/hatismart/ai.json (chỉ quản trị máy đọc được), ví dụ
  {"key": "sk-…", "base_url": "https://api.vilao.ai/v1", "model": "deepseek-v4.1-flash-req", "daily_limit": 200}
Chưa có file/khoá → 404 {"error": "ai_not_configured"} (app chuyển sang hàm đám mây).
Hướng dẫn cho AI + công cụ: ai_prompt.json, sinh từ app_hatismart_pro/ai-relay/functions/assistant.js (một nguồn duy nhất).
Lưu ý: khoá nằm trên máy HA của nhà → chỉ dùng cho showroom/thử; bán cho khách thật thì giữ khoá ở dịch vụ đám mây.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from aiohttp import ClientTimeout

from homeassistant.components.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "ai_prompt.json")
MAX_BODY = 400 * 1024
MAX_CONTEXT = 120 * 1024
MAX_TOKENS = 4000
_VN = timezone(timedelta(hours=7))

_prompt: dict | None = None
_cfg_cache: tuple[float, dict | None] = (0.0, None)
_quota: dict[str, int] = {}   # "<user_id>_<ngày>" → số lượt (đếm trong bộ nhớ, khởi động lại thì đếm lại)


def _load_prompt() -> dict:
    global _prompt
    if _prompt is None:
        with open(_PROMPT_PATH, encoding="utf-8") as f:
            _prompt = json.load(f)
    return _prompt


def _load_cfg(path: str) -> dict | None:
    global _cfg_cache
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _cfg_cache[0] == mtime:
        return _cfg_cache[1]
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning("Đọc %s lỗi: %s", path, e)
        cfg = None
    _cfg_cache = (mtime, cfg)
    return cfg


def _take_quota(user_id: str, limit: int) -> int | None:
    """Trừ 1 lượt; trả về số lượt còn lại, None nếu hết."""
    day = datetime.now(_VN).strftime("%Y-%m-%d")
    for k in [k for k in _quota if not k.endswith(day)]:
        _quota.pop(k, None)
    key = f"{user_id}_{day}"
    n = _quota.get(key, 0)
    if n >= limit:
        return None
    _quota[key] = n + 1
    return limit - n - 1


def _tool_results(results: list) -> list:
    out = []
    for r in results:
        c = r.get("content")
        block = {"type": "tool_result", "tool_use_id": str(r.get("tool_use_id")),
                 "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}
        if r.get("is_error"):
            block["is_error"] = True
        out.append(block)
    return out


class AIChatView(HomeAssistantView):
    url = "/api/hatismart/ai/chat"
    name = "api:hatismart:ai_chat"

    async def get(self, request):
        hass = request.app["hass"]
        cfg = await hass.async_add_executor_job(_load_cfg, hass.config.path("hatismart", "ai.json"))
        return self.json({"enabled": bool(cfg and cfg.get("key")), "model": (cfg or {}).get("model", "")})

    async def post(self, request):
        hass = request.app["hass"]
        user = request.get("hass_user")
        cfg = await hass.async_add_executor_job(_load_cfg, hass.config.path("hatismart", "ai.json"))
        if not cfg or not cfg.get("key"):
            return self.json({"error": "ai_not_configured"}, status_code=404)
        if (request.content_length or 0) > MAX_BODY:
            return self.json({"error": "too_long"}, status_code=413)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return self.json({"error": "bad_request"}, status_code=400)

        context, text, results = body.get("context"), body.get("text"), body.get("toolResults")
        history = body.get("history") or []
        if isinstance(history, str):
            try:
                history = json.loads(history) if history else []
            except ValueError:
                return self.json({"error": "bad_history"}, status_code=400)
        if not isinstance(context, str) or len(context) > MAX_CONTEXT or not isinstance(history, list):
            return self.json({"error": "bad_request"}, status_code=400)
        has_text = isinstance(text, str) and text.strip()
        has_results = isinstance(results, list) and len(results) > 0
        if bool(has_text) == bool(has_results) or (has_text and len(text) > 2000):
            return self.json({"error": "bad_request"}, status_code=400)

        remaining = _take_quota(user.id if user else "?", int(cfg.get("daily_limit") or 200))
        if remaining is None:
            return self.json({"error": "daily_limit"}, status_code=429)

        p = _load_prompt()
        now = body.get("now")
        user_content = (f"[Bây giờ: {now}]\n{text.strip()}" if now else text.strip()) if has_text else _tool_results(results)
        messages = [*history, {"role": "user", "content": user_content}]
        payload = {
            "model": cfg.get("model") or "deepseek-v4.1-flash-req",
            "max_tokens": MAX_TOKENS,
            "system": [
                {"type": "text", "text": p["system"]},
                {"type": "text", "text": f"<thiet_bi>\n{context}\n</thiet_bi>", "cache_control": {"type": "ephemeral"}},
            ],
            "tools": p["tools"],
            "messages": messages,
        }
        key = cfg["key"]
        base = (cfg.get("base_url") or "https://api.anthropic.com/v1").rstrip("/")
        headers = {"x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}

        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        started = time.monotonic()
        try:
            async with async_get_clientsession(hass).post(f"{base}/messages", json=payload, headers=headers,
                                                          timeout=ClientTimeout(total=60)) as r:
                data = await r.json(content_type=None)
                status = r.status
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Gọi AI lỗi: %s", e)
            return self.json({"error": "upstream"}, status_code=502)
        ms = int((time.monotonic() - started) * 1000)
        if status != 200:
            msg = ((data or {}).get("error") or {}).get("message", "") if isinstance(data, dict) else ""
            _LOGGER.warning("AI trả %s: %s", status, msg[:200])
            if status in (429, 529):
                return self.json({"error": "busy"}, status_code=503)
            if status == 400:
                return self.json({"error": "bad_request", "detail": msg[:200]}, status_code=400)
            return self.json({"error": "upstream", "status": status}, status_code=502)

        content = data.get("content") or []
        stop = data.get("stop_reason")
        out = {"usage": data.get("usage"), "model": data.get("model"), "ms": ms, "costUSD": 0, "remaining": remaining}
        if stop in ("refusal", "max_tokens"):
            reply = ("Mình không làm được yêu cầu này. Bạn thử nói theo cách khác nhé." if stop == "refusal"
                     else "Câu trả lời bị cắt giữa chừng, bạn thử hỏi ngắn hơn nhé.")
            return self.json({**out, "history": json.dumps(history, ensure_ascii=False), "reply": reply, "toolCalls": []})
        calls = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
                 for b in content if b.get("type") == "tool_use"]
        reply = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip() or None
        messages.append({"role": "assistant", "content": content})
        _LOGGER.debug("AI %s: %d ms, %d lệnh", payload["model"], ms, len(calls))
        return self.json({**out, "history": json.dumps(messages, ensure_ascii=False), "reply": reply, "toolCalls": calls})
