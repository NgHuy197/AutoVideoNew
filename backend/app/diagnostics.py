"""Small, dependency-light diagnostics used by the Windows supervisor."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    exe: str = ""
    command_line: str = ""


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read a PID's creation identity, returning ``None`` if it is gone."""

    try:
        import psutil
        process = psutil.Process(int(pid))
        return ProcessIdentity(process.pid, float(process.create_time()),
                               process.exe() or "", " ".join(process.cmdline()))
    except (ImportError, OSError, ValueError):
        return None
    except Exception:
        # psutil raises NoSuchProcess/AccessDenied subclasses whose exact
        # availability varies by version. A missing identity is fail-closed.
        return None


def identity_matches(identity: ProcessIdentity | None, *, pid: int, started_at: float | None = None,
                     command_tokens: tuple[str, ...] = ()) -> bool:
    """Return true only when PID, creation time, and command ownership match."""

    if identity is None or identity.pid != int(pid):
        return False
    # psutil reports process creation in seconds while SQLite stores the
    # persisted value with microsecond precision. A one-second fence allows
    # only timestamp conversion/clock precision, and is far too small to
    # accept a reused PID created later in normal operation.
    if started_at is not None and abs(identity.create_time - float(started_at)) > 1.0:
        return False
    command = f"{identity.exe} {identity.command_line}".lower()
    return all(token.lower() in command for token in command_tokens)


def resource_snapshot(root: Path) -> dict[str, int | float]:
    """Capture disk and available-RAM values used by queue gating."""

    usage = shutil.disk_usage(root)
    snapshot: dict[str, int | float] = {
        "disk_free_bytes": int(usage.free),
        "disk_total_bytes": int(usage.total),
        "sampled_at": time.time(),
    }
    try:
        import psutil
        memory = psutil.virtual_memory()
        snapshot["ram_available_bytes"] = int(memory.available)
        snapshot["ram_total_bytes"] = int(memory.total)
    except ImportError:
        snapshot["ram_available_bytes"] = -1
        snapshot["ram_total_bytes"] = -1
    return snapshot


def append_json_line(path: Path, payload: dict) -> None:
    """Append one UTF-8 diagnostic record, creating its parent directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def exception_payload(error: BaseException, *, component: str) -> dict[str, object]:
    return {"time": time.time(), "component": component, "error": str(error),
            "type": type(error).__name__, "python": sys.version.split()[0]}
