"""Usage counters, rate-limit headers and readable errors for the external translation API."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import socket
from urllib.error import HTTPError, URLError

from sqlalchemy import func, select

from .db import ApiUsage, SessionLocal, Setting, json_text, now

STATUS_KEY = "api_status"
# Kinds where a second attempt cannot succeed, so the batch goes straight to NLLB.
PERMANENT_ERRORS = {"auth", "quota", "model"}


@dataclass
class ApiCall:
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    rate_limits: dict[str, str] = field(default_factory=dict)
    error_kind: str | None = None
    error: str | None = None


def rate_limit_headers(headers) -> dict[str, str]:
    """Keep the provider's ``x-ratelimit-*`` and ``retry-after`` headers, lowercased."""
    if headers is None: return {}
    return {key.lower(): str(value) for key, value in headers.items()
            if key.lower().startswith("x-ratelimit-") or key.lower() == "retry-after"}


def describe_error(exc: BaseException) -> tuple[str, str]:
    """Return ``(kind, Vietnamese message)`` for a failed API call."""
    if isinstance(exc, HTTPError):
        try: body = exc.read().decode("utf-8", "replace")[:2000]
        except (OSError, AttributeError): body = ""
        lowered = body.lower()
        detail = ""
        try:
            error = json.loads(body).get("error")
            detail = str(error.get("message") if isinstance(error, dict) else error or "")[:200]
        except (ValueError, AttributeError):
            pass
        suffix = f": {detail}" if detail else ""
        if exc.code in (401, 403): return "auth", f"API key không hợp lệ hoặc không có quyền (HTTP {exc.code}){suffix}"
        if exc.code == 402 or any(word in lowered for word in ("insufficient_quota", "credit", "balance", "billing")):
            return "quota", f"Hết hạn mức hoặc hết tiền trong tài khoản API (HTTP {exc.code}){suffix}"
        if exc.code == 429: return "rate_limit", f"Gọi quá nhanh, vượt giới hạn tốc độ (HTTP 429){suffix}"
        if exc.code in (400, 404) and "model" in lowered: return "model", f"Sai tên model hoặc model không dùng được (HTTP {exc.code}){suffix}"
        if exc.code == 404: return "model", f"Sai địa chỉ API (HTTP 404){suffix}"
        if exc.code >= 500: return "server", f"Máy chủ API đang lỗi (HTTP {exc.code}){suffix}"
        return "http", f"API trả về lỗi HTTP {exc.code}{suffix}"
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
        return "timeout", "API phản hồi quá chậm (hết thời gian chờ)"
    if isinstance(exc, URLError): return "network", f"Không kết nối được tới API: {exc.reason}"
    if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)): return "format", "API trả về dữ liệu không đúng định dạng OpenAI"
    return "network", f"Không kết nối được tới API: {exc}"


def record_call(call: ApiCall) -> None:
    """Add one call to today's counters and remember the latest status."""
    day = datetime.now().strftime("%Y-%m-%d")
    with SessionLocal() as session:
        row = session.get(ApiUsage, day) or ApiUsage(day=day, calls=0, prompt_tokens=0, completion_tokens=0, errors=0, rate_limited=0)
        row.calls += 1
        row.prompt_tokens += call.prompt_tokens
        row.completion_tokens += call.completion_tokens
        row.errors += 1 if call.error else 0
        row.rate_limited += 1 if call.error_kind in ("rate_limit", "quota") else 0
        session.merge(row)
        stored = session.get(Setting, STATUS_KEY)
        previous = json.loads(stored.value_json) if stored else {}
        status = {"at": now().isoformat(), "model": call.model, "ok": call.error is None,
                  "error": call.error, "error_kind": call.error_kind,
                  "last_error": previous.get("last_error"), "last_error_at": previous.get("last_error_at"),
                  # Keep the last known limits when this response carried none.
                  "rate_limits": call.rate_limits or previous.get("rate_limits") or {}}
        if call.error: status["last_error"], status["last_error_at"] = call.error, status["at"]
        session.merge(Setting(key=STATUS_KEY, value_json=json_text(status)))
        session.commit()


def usage_summary() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    columns = (ApiUsage.calls, ApiUsage.prompt_tokens, ApiUsage.completion_tokens, ApiUsage.errors, ApiUsage.rate_limited)

    def totals(session, condition) -> dict:
        values = session.execute(select(*(func.coalesce(func.sum(column), 0) for column in columns)).where(condition)).one()
        calls, prompt, completion, errors, limited = (int(value) for value in values)
        return {"calls": calls, "prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion, "errors": errors, "rate_limited": limited}

    with SessionLocal() as session:
        stored = session.get(Setting, STATUS_KEY)
        return {"today": totals(session, ApiUsage.day == today),
                "month": totals(session, ApiUsage.day.like(today[:7] + "-%")),
                "status": json.loads(stored.value_json) if stored else None}
