"""Independent Windows-friendly process supervisor for API and durable workers."""
from __future__ import annotations

import os
import sys
import time
import json
from pathlib import Path

os.environ["VIDEOAUTO_DISABLE_EMBEDDED_QUEUE"] = "1"

from .app.config import settings
from .app.db import init_db
from .app.diagnostics import append_json_line, process_identity
from .app.process_control import OwnedProcess, close_owned, spawn_owned, terminate_owned
from .app.service import QueueSupervisor
from .app.watchdog import SUSPEND_GAP_SECONDS, ApiWatchdog


def _open_byte_lock(path: Path):
    """Return a fixed-size lock file suitable for msvcrt byte locking."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    return handle


def _acquire_singleton():
    lock_path = settings.data_root / "supervisor.lock"
    handle = _open_byte_lock(lock_path)
    try:
        if os.name == "nt":
            import msvcrt; msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl; fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close(); raise SystemExit("Video Auto supervisor is already running")
    return handle


def _acquire_runtime_active_lock():
    """Hold the runtime ownership fence for the whole supervisor lifetime.

    Setup checks this byte-range lock before replacing a managed model or
    executable.  The singleton lock prevents a second supervisor, while this
    separate lock makes the ownership contract explicit to the importer even
    if a worker was started during recovery.
    """

    runtime_root = settings.runtime_root or (settings.data_root / "runtime")
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_root / ".runtime-active.lock"
    handle = _open_byte_lock(lock_path)
    try:
        if os.name == "nt":
            import msvcrt; msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl; fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close(); raise SystemExit("Video Auto runtime is already in use")
    return handle


def _write_pid_file() -> Path:
    """Publish supervisor PID plus creation time for fenced stop/status tools."""
    target = settings.data_root / "supervisor.pid.json"
    identity = process_identity(os.getpid())
    payload = {"pid": os.getpid(), "create_time": identity.create_time if identity else None,
               "project_root": str(Path(__file__).parents[1])}
    partial = target.with_suffix(target.suffix + ".partial")
    partial.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(partial, target)
    return target


def _clear_pid_file(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if int(payload.get("pid", -1)) != os.getpid():
            return
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    path.unlink(missing_ok=True)


def main() -> None:
    settings.ensure_dirs(); init_db(); lock_handle = _acquire_singleton(); runtime_lock_handle = _acquire_runtime_active_lock(); pid_file = _write_pid_file()
    queue = QueueSupervisor(); queue.start()
    api_process: OwnedProcess | None = None
    watchdog = ApiWatchdog()
    project_root = Path(__file__).parents[1]
    last_loop = time.monotonic()
    try:
        while True:
            current = time.monotonic()
            if current - last_loop > SUSPEND_GAP_SECONDS:
                # Every thread was frozen, so silence here is not a queue stall.
                queue.record_error("system-resume", f"process was suspended for {current - last_loop:.0f}s")
                queue.note_resume(); watchdog.resumed(current)
            last_loop = current
            if not queue.is_healthy():
                queue.record_error("queue-watchdog", "Queue thread stopped or no tick for 15 minutes")
                raise RuntimeError("Queue watchdog expired; service restart required")
            if api_process is not None:
                dead = not api_process.is_running()
                unhealthy = not dead and watchdog.should_restart(current)
                if unhealthy:
                    queue.record_error("api-watchdog", "API failed three liveness probes")
                    dead = terminate_owned(api_process)
                if dead:
                    close_owned(api_process)
                    api_process = None
                    watchdog.failed(current)
            if api_process is None and current >= watchdog.next_start:
                try:
                    api_process = spawn_owned(
                        [sys.executable, "-X", "utf8", "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8765"],
                        cwd=project_root, log_path=settings.logs_root / "api.log",
                        job_name=f"VideoAuto-api-{os.getpid()}-{time.time_ns()}")
                    watchdog.started(time.monotonic())
                except (OSError, RuntimeError) as exc:
                    watchdog.failed(time.monotonic())
                    try:
                        append_json_line(settings.logs_root / "supervisor.jsonl", {
                            "time": time.time(), "component": "api-spawn", "error": str(exc), "pid": os.getpid()})
                    except OSError:
                        pass
            time.sleep(2)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if api_process is not None:
            terminate_owned(api_process)
            close_owned(api_process)
        queue.stop()
        _clear_pid_file(pid_file)
        runtime_lock_handle.close()
        lock_handle.close()


if __name__ == "__main__": main()
