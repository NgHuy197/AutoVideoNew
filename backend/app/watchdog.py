"""Bounded local HTTP probes and monotonic API restart policy."""
from __future__ import annotations

import json
from http.client import HTTPException
from urllib.request import ProxyHandler, build_opener


def api_healthy() -> bool:
    # Never send loopback probes through a user-configured proxy.
    try:
        with build_opener(ProxyHandler({})).open(
            "http://127.0.0.1:8765/api/v1/health/live", timeout=2
        ) as response:
            return response.status == 200 and json.loads(response.read(1024)).get("status") == "ok"
    except (OSError, ValueError, AttributeError, HTTPException):
        return False


class ApiWatchdog:
    def __init__(self) -> None:
        self.started_at = 0.0
        self.next_probe = 0.0
        self.next_start = 0.0
        self.failures = 0
        self.restarts = 0

    def started(self, current: float) -> None:
        self.started_at = current
        self.next_probe = current + 60  # allow cold imports/migrations
        self.failures = 0

    def failed(self, current: float) -> None:
        self.restarts = min(self.restarts + 1, 8)
        self.next_start = current + min(300, 2 ** self.restarts)

    def should_restart(self, current: float, probe=api_healthy) -> bool:
        if current < self.next_probe:
            return False
        self.next_probe = current + 15
        if probe():
            self.failures = 0
            if current - self.started_at >= 300:
                self.restarts = 0
            return False
        self.failures += 1
        return self.failures >= 3
