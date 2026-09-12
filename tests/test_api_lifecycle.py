from __future__ import annotations

import asyncio

from backend.app import api


def _run_lifespan(app) -> None:
    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(exercise())


def test_lifespan_starts_and_stops_embedded_supervisor(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.delenv("VIDEOAUTO_DISABLE_EMBEDDED_QUEUE", raising=False)
    monkeypatch.setattr(api.supervisor, "start", lambda: calls.append("start"))
    monkeypatch.setattr(api.supervisor, "stop", lambda: calls.append("stop"))

    _run_lifespan(api.create_app())

    assert calls == ["start", "stop"]


def test_lifespan_can_disable_embedded_supervisor(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setenv("VIDEOAUTO_DISABLE_EMBEDDED_QUEUE", "1")
    monkeypatch.setattr(api.supervisor, "start", lambda: calls.append("start"))
    monkeypatch.setattr(api.supervisor, "stop", lambda: calls.append("stop"))

    _run_lifespan(api.create_app())

    assert calls == []
