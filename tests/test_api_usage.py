from __future__ import annotations

import io
import json
from email.message import Message
from urllib.error import HTTPError, URLError

from backend.app import api, api_usage, translate
from backend.app.api_usage import ApiCall, describe_error, rate_limit_headers, record_call, usage_summary
from backend.app.db import ApiUsage, SessionLocal, Setting, init_db
from backend.app.translate import ApiTranslator, Translation


def _http_error(code: int, body: dict, headers: dict | None = None) -> HTTPError:
    message = Message()
    for key, value in (headers or {}).items(): message[key] = value
    return HTTPError("https://api.example.test/v1/chat/completions", code, "error", message, io.BytesIO(json.dumps(body).encode()))


class _Response(io.BytesIO):
    def __init__(self, payload: dict, headers: dict):
        super().__init__(json.dumps(payload).encode())
        self.headers = Message()
        for key, value in headers.items(): self.headers[key] = value


class _FakeNllb:
    def translate_batch(self, texts, source_code, **_kwargs):
        return [Translation(f"nllb:{text}", source_code) for text in texts]


def _reset_usage() -> None:
    init_db()
    with SessionLocal() as session:
        session.query(ApiUsage).delete()
        session.query(Setting).filter(Setting.key == api_usage.STATUS_KEY).delete()
        session.commit()


def test_errors_are_classified_in_vietnamese():
    assert describe_error(_http_error(401, {"error": {"message": "bad key"}}))[0] == "auth"
    assert describe_error(_http_error(429, {"error": {"code": "insufficient_quota"}}))[0] == "quota"
    assert describe_error(_http_error(429, {"error": {"message": "slow down"}}))[0] == "rate_limit"
    assert describe_error(_http_error(404, {"error": {"message": "The model `x` does not exist"}}))[0] == "model"
    assert describe_error(_http_error(503, {}))[0] == "server"
    assert describe_error(URLError("no route"))[0] == "network"
    kind, message = describe_error(_http_error(401, {"error": {"message": "bad key"}}))
    assert "API key" in message and "bad key" in message


def test_rate_limit_headers_are_kept_lowercase():
    headers = Message(); headers["X-RateLimit-Remaining-Requests"] = "499"; headers["Content-Type"] = "json"; headers["Retry-After"] = "2"
    assert rate_limit_headers(headers) == {"x-ratelimit-remaining-requests": "499", "retry-after": "2"}


def test_usage_is_counted_per_day_and_keeps_last_error():
    _reset_usage()
    record_call(ApiCall("gpt-test", 100, 20, {"x-ratelimit-remaining-requests": "9"}))
    record_call(ApiCall("gpt-test", error_kind="rate_limit", error="Gọi quá nhanh"))
    record_call(ApiCall("gpt-test", 50, 10))
    summary = usage_summary()
    assert summary["today"] == {"calls": 3, "prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180,
                                "errors": 1, "rate_limited": 1}
    assert summary["month"]["calls"] == 3
    status = summary["status"]
    assert status["ok"] and status["last_error"] == "Gọi quá nhanh"
    # A response without limit headers keeps the last known limits.
    assert status["rate_limits"] == {"x-ratelimit-remaining-requests": "9"}


def test_translator_reports_usage_and_stops_on_bad_key(monkeypatch):
    calls: list[ApiCall] = []
    replies = [_http_error(401, {"error": {"message": "bad key"}})]

    class Opener:
        def open(self, _request, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception): raise reply
            return reply

    monkeypatch.setattr(translate, "build_opener", lambda *a: Opener())
    item = ApiTranslator("https://api.example.test/v1", "sk", "gpt-test", _FakeNllb, on_call=calls.append)
    rows = item.translate_batch(["Hello."], "en")
    # A wrong key is not retried; the line goes to NLLB with the reason.
    assert len(calls) == 1 and calls[0].error_kind == "auth"
    assert rows[0].text == "nllb:Hello." and "API key" in rows[0].warning

    replies.append(_Response({"choices": [{"message": {"content": "1. Xin chào."}}],
                              "usage": {"prompt_tokens": 40, "completion_tokens": 5}},
                             {"x-ratelimit-remaining-tokens": "1000"}))
    item.translate_batch(["Hello."], "en")
    assert (calls[1].prompt_tokens, calls[1].completion_tokens) == (40, 5)
    assert calls[1].rate_limits == {"x-ratelimit-remaining-tokens": "1000"}


def test_rate_limit_waits_then_retries(monkeypatch):
    replies = [_http_error(429, {"error": {"message": "slow down"}}, {"Retry-After": "3"}),
               _Response({"choices": [{"message": {"content": "1. Xin chào."}}]}, {})]
    slept: list[float] = []

    class Opener:
        def open(self, _request, timeout):
            reply = replies.pop(0)
            if isinstance(reply, Exception): raise reply
            return reply

    monkeypatch.setattr(translate, "build_opener", lambda *a: Opener())
    monkeypatch.setattr(translate.time, "sleep", slept.append)
    rows = ApiTranslator("https://api.example.test/v1", "sk", "gpt-test", _FakeNllb).translate_batch(["Hello."], "en")
    assert slept == [3.0] and rows[0].text == "Xin chào." and rows[0].warning is None


def test_connection_test_endpoint(monkeypatch):
    _reset_usage()
    monkeypatch.setattr(api.settings, "api_key", "sk-saved")
    sent = []

    class Opener:
        def open(self, request, timeout):
            sent.append(request)
            return _Response({"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 8, "completion_tokens": 1}},
                             {"x-ratelimit-remaining-requests": "59"})

    monkeypatch.setattr(translate, "build_opener", lambda *a: Opener())
    result = api.test_api(api.ApiTestRequest(api_base_url="https://api.example.test/v1", api_model="gpt-test"), None)
    assert result["ok"] and result["reply"] == "OK" and result["rate_limits"] == {"x-ratelimit-remaining-requests": "59"}
    assert sent[0].get_header("Authorization") == "Bearer sk-saved"  # empty field uses the saved key
    assert result["usage"]["today"]["total_tokens"] == 9
