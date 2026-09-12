from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
import shutil  # compatibility for callers/tests that patch disk_usage at this boundary
from pathlib import Path

from sqlalchemy import func, select, update

from .config import settings
from .db import Job, SessionLocal, WatchFolder, WatchSeen, now
from .diagnostics import append_json_line, identity_matches, process_identity, resource_snapshot
from .media import sha256_file
from .process_control import OwnedProcess, close_owned, spawn_owned, terminate_owned


class QueueSupervisor:
    """Durable queue dispatcher. Each job runs in an isolated Python child process."""
    def __init__(self) -> None:
        self._stop = threading.Event(); self._thread: threading.Thread | None = None
        self._children: dict[str, OwnedProcess] = {}; self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}
        self._last_maintenance = 0.0
        self._last_resource_state: tuple[bool, str] | None = None
        self._last_error: dict[str, float] = {}
        # The inhibitor is deliberately owned by the supervisor thread. It is
        # refreshed there so it never leaks a power request after shutdown.
        from .maintenance import AcIdleInhibitor
        self._idle_inhibitor = AcIdleInhibitor()

    def start(self) -> None:
        if self._thread and self._thread.is_alive(): return
        self._recover_stale()
        self._reconcile_uploads()
        self._idle_inhibitor.refresh()
        self._thread = threading.Thread(target=self._loop, name="videoauto-supervisor", daemon=True); self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # API shutdown must leave child workers alive. A fresh supervisor will reclaim only
        # leases that have stopped heartbeating, so an API reload cannot cancel a job.
        if self._thread: self._thread.join(timeout=5)
        self._idle_inhibitor.clear()

    @staticmethod
    def _utc_epoch(value: datetime | None) -> float | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    def _record_error(self, component: str, error: BaseException | str) -> None:
        """Persist supervisor failures while rate-limiting identical storms."""
        message = str(error)
        key = f"{component}:{type(error).__name__}:{message}"
        current = time.monotonic()
        previous = self._last_error.get(key, 0.0)
        if current - previous < 60.0:
            return
        self._last_error[key] = current
        try:
            append_json_line(settings.logs_root / "supervisor.jsonl", {
                "time": time.time(), "component": component, "error": message,
                "type": type(error).__name__ if isinstance(error, BaseException) else "RuntimeError",
                "pid": os.getpid(),
            })
        except OSError:
            # Logging must never stop the queue. The original failure remains
            # visible in the persisted job/resource state when applicable.
            pass

    def _record_resource_state(self, ready: bool, reason: str, snapshot: dict[str, int | float]) -> None:
        try:
            append_json_line(settings.logs_root / "supervisor.jsonl", {
                "time": time.time(), "component": "resources", "ready": ready,
                "reason": reason, **snapshot, "pid": os.getpid(),
            })
        except OSError:
            pass

    def _reconcile_uploads(self) -> None:
        try:
            from .maintenance import reconcile_completing_uploads
            result = reconcile_completing_uploads()
            if result.get("failed"):
                self._record_error("upload-reconcile", json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            self._record_error("upload-reconcile", exc)

    def _recover_stale(self) -> None:
        # Read candidate identities first and close the read transaction. A
        # later API cancel/pause is then observed by the conditional release
        # below instead of being overwritten by a stale ORM object.
        with SessionLocal() as session:
            stale_before = now() - timedelta(seconds=90)
            active = {"RUNNING", "PAUSING", "CANCELLING"}
            stale_jobs = [(job.id, job.lease_id, job.worker_pid) for job in session.scalars(select(Job).where(Job.status.in_(active), Job.lease_at < stale_before))]
            watchdog_before = now() - timedelta(hours=2)
            overdue = [(job.id, job.lease_id, job.worker_pid) for job in session.scalars(select(Job).where(Job.status.in_(active), Job.worker_started_at.is_not(None), Job.worker_started_at < watchdog_before))]
            stage_expired = [(job.id, job.lease_id, job.worker_pid, job.stage) for job in session.scalars(select(Job).where(Job.status.in_(active), Job.stage_deadline_at.is_not(None), Job.stage_deadline_at < now()))]
        stale_ids = {item[0] for item in stale_jobs}
        overdue_ids = {item[0] for item in overdue}
        handled: set[str] = set()
        for job_id, lease_id, pid, stage in stage_expired:
            if job_id in stale_ids or job_id in overdue_ids: continue
            if pid and not self._terminate_job_process_by_identity(job_id, lease_id, pid):
                self._record_error("deadline", f"stage deadline reached but worker termination was not confirmed for {job_id}")
                continue
            self._release_owned(job_id, lease_id, f"stage deadline exceeded: {stage}")
            handled.add(job_id)
        for job_id, lease_id, pid in stale_jobs:
            if pid and not self._terminate_job_process_by_identity(job_id, lease_id, pid):
                self._record_error("lease", f"lease expired but worker termination was not confirmed for {job_id}")
                continue
            self._release_owned(job_id, lease_id, "worker lease expired after retry budget")
            handled.add(job_id)
        for job_id, lease_id, pid in overdue:
            if job_id in stale_ids: continue
            if pid and not self._terminate_job_process_by_identity(job_id, lease_id, pid):
                self._record_error("watchdog", f"watchdog reached but worker termination was not confirmed for {job_id}")
                continue
            self._release_owned(job_id, lease_id, "worker exceeded two-hour watchdog")

    @staticmethod
    def _terminate_job_process_by_identity(job_id: str, lease_id: str | None, pid: int) -> bool:
        with SessionLocal() as session:
            job = session.scalar(select(Job).where(Job.id == job_id, Job.lease_id == lease_id))
            if not job:
                return True
            return QueueSupervisor._terminate_job_process(job)

    def _release_owned(self, job_id: str, lease_id: str | None, reason: str) -> bool:
        """Release only the generation observed during stale recovery."""
        with SessionLocal() as session:
            job = session.scalar(select(Job).where(Job.id == job_id, Job.lease_id == lease_id,
                                                   Job.status.in_(["RUNNING", "PAUSING", "CANCELLING"])))
            if not job:
                return False
            previous = job.status
            if previous == "CANCELLING":
                values = {"status": "CANCELLED", "cancel_requested": True, "pause_requested": False,
                          "pause_acknowledged": False, "lease_id": None, "lease_at": None,
                          "worker_pid": None, "worker_started_at": None, "stage_deadline_at": None, "updated_at": now()}
            elif previous == "PAUSING":
                values = {"status": "PAUSED", "pause_requested": False, "pause_acknowledged": True,
                          "lease_id": None, "lease_at": None, "worker_pid": None,
                          "worker_started_at": None, "stage_deadline_at": None, "updated_at": now()}
            elif job.retry_count < len(settings.retry_delays):
                delay = settings.retry_delays[job.retry_count]
                values = {"status": "QUEUED", "retry_after": now() + timedelta(seconds=delay),
                          "retry_count": job.retry_count + 1, "error": reason, "lease_id": None,
                          "lease_at": None, "worker_pid": None, "worker_started_at": None,
                          "stage_deadline_at": None, "updated_at": now()}
            else:
                values = {"status": "FAILED", "error": reason, "lease_id": None, "lease_at": None,
                          "worker_pid": None, "worker_started_at": None, "stage_deadline_at": None, "updated_at": now()}
            result = session.execute(update(Job).where(Job.id == job_id, Job.lease_id == lease_id,
                                                       Job.status == previous).values(**values))
            if result.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            return True

    @staticmethod
    def _release_after_failure(job: Job, reason: str) -> None:
        """Release a durable owner and apply the bounded retry policy."""
        previous = job.status
        job.lease_id = None; job.lease_at = None; job.worker_pid = None; job.worker_started_at = None; job.stage_deadline_at = None
        if previous == "CANCELLING":
            job.status = "CANCELLED"; job.cancel_requested = True; job.pause_requested = False
        elif previous == "PAUSING":
            job.status = "PAUSED"; job.pause_requested = False; job.pause_acknowledged = True
        elif job.retry_count < len(settings.retry_delays):
            job.status = "QUEUED"; job.retry_after = now() + timedelta(seconds=settings.retry_delays[job.retry_count]); job.retry_count += 1; job.error = reason
        else:
            job.status = "FAILED"; job.error = reason

    @staticmethod
    def _terminate_job_process(job: Job) -> bool:
        if not job.worker_pid:
            return False
        pid = int(job.worker_pid)
        identity = process_identity(pid)
        if identity is None:
            # A missing PID is already terminated. AccessDenied and other
            # identity failures are deliberately fail-closed in
            # process_identity, so distinguish them from a confirmed absence.
            try:
                import psutil
            except ImportError:
                return False
            try:
                psutil.Process(pid)
            except psutil.NoSuchProcess:
                return True
            except Exception:
                return False
        started = QueueSupervisor._utc_epoch(job.worker_started_at)
        tokens = (job.id,) + ((job.lease_id,) if job.lease_id else ())
        if not identity_matches(identity, pid=pid, started_at=started,
                                command_tokens=tokens):
            return False
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0:
                return True
            # The process may have exited between the identity read and
            # taskkill. Confirm absence before treating that race as success.
            return process_identity(pid) is None
        try:
            os.kill(pid, signal.SIGTERM); return True
        except ProcessLookupError:
            return True

    def _handle_control_requests(self) -> None:
        """Consume API pause/cancel requests even when API and queue are separate processes."""
        with SessionLocal() as session:
            requests = list(session.scalars(select(Job).where(Job.status == "CANCELLING", Job.cancel_requested.is_(True), Job.lease_id.is_not(None))))
        for job in requests:
            with self._lock: child = self._children.get(job.id)
            if child is not None:
                if child.is_running(): terminate_owned(child)
                continue
            if self._terminate_job_process(job):
                with SessionLocal() as session:
                    result = session.execute(update(Job).where(Job.id == job.id, Job.lease_id == job.lease_id,
                                                               Job.status == "CANCELLING").values(status="CANCELLED", lease_id=None,
                                                               lease_at=None, worker_pid=None, worker_started_at=None, stage_deadline_at=None))
                    session.commit()

    @staticmethod
    def _terminate_process(pid: int) -> bool:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)); return result.returncode == 0
        try: os.kill(pid, 15); return True
        except ProcessLookupError: return True

    def _resource_state(self) -> tuple[bool, str, dict[str, int | float]]:
        """Check every filesystem used by an active job plus available RAM."""
        try:
            roots = (settings.data_root, settings.output_root)
            snapshots = [resource_snapshot(root) for root in roots]
            disk_free = min(int(item["disk_free_bytes"]) for item in snapshots)
            if disk_free < settings.min_free_bytes:
                return False, f"disk free {disk_free} bytes is below reserve {settings.min_free_bytes}", {
                    "disk_free_bytes": disk_free, "ram_available_bytes": min(int(item.get("ram_available_bytes", -1)) for item in snapshots),
                }
            ram_values = [int(item.get("ram_available_bytes", -1)) for item in snapshots if int(item.get("ram_available_bytes", -1)) >= 0]
            if ram_values and min(ram_values) < settings.min_available_ram_bytes:
                available = min(ram_values)
                return False, f"RAM available {available} bytes is below reserve {settings.min_available_ram_bytes}", {
                    "disk_free_bytes": disk_free, "ram_available_bytes": available,
                }
            return True, "ready", {"disk_free_bytes": disk_free, "ram_available_bytes": min(ram_values) if ram_values else -1}
        except (OSError, ValueError, TypeError) as exc:
            return False, f"resource check failed: {exc}", {"disk_free_bytes": -1, "ram_available_bytes": -1}

    def _update_resource_waiters(self, ready: bool, reason: str) -> None:
        """Persist WAITING_RESOURCES and return jobs to the queue on recovery."""
        with SessionLocal() as session:
            if ready:
                result = session.execute(update(Job).where(Job.status == "WAITING_RESOURCES").values(
                    status="QUEUED", error=None, updated_at=now()))
            else:
                result = session.execute(update(Job).where(Job.status.in_(["QUEUED", "WAITING_RESOURCES"])).values(
                    status="WAITING_RESOURCES", error=f"waiting for resources: {reason}", updated_at=now()))
            if result.rowcount:
                session.commit()

    def _loop(self) -> None:
        while not self._stop.wait(2):
            try:
                self._recover_stale(); self._handle_control_requests(); self._watch_folders(); self._reap(); self._maintenance()
                ready, reason, snapshot = self._resource_state()
                state = (ready, reason)
                if state != self._last_resource_state:
                    self._last_resource_state = state
                    self._record_resource_state(ready, reason, snapshot)
                self._update_resource_waiters(ready, reason)
                self._idle_inhibitor.refresh()
                with SessionLocal() as session:
                    active_count = int(session.scalar(select(func.count()).select_from(Job).where(Job.status.in_(["RUNNING", "PAUSING", "CANCELLING"]), Job.lease_at >= now() - timedelta(seconds=90))) or 0)
                with self._lock: slots = settings.max_concurrent_jobs - active_count
                if not ready: slots = 0
                for _ in range(max(0, slots)):
                    claimed = self._claim_next()
                    if not claimed: break
                    self._spawn(*claimed)
            except Exception as exc:
                # A malformed watch folder or transient SQLite lock must not kill the
                # supervisor thread; the next tick retries and the error is persisted.
                self._record_error("supervisor-loop", exc)
                continue

    def _maintenance(self) -> None:
        """Run bounded housekeeping without touching external originals or outputs."""
        if time.monotonic() - self._last_maintenance < 3600: return
        self._last_maintenance = time.monotonic()
        try:
            from .maintenance import reconcile_completing_uploads, run_housekeeping
            reconcile_completing_uploads()
            run_housekeeping()
        except Exception as exc:
            self._record_error("maintenance", exc)

    def _resources_ready(self) -> bool:
        return self._resource_state()[0]

    def _claim_next(self) -> tuple[str, str] | None:
        with SessionLocal() as session:
            if (session.scalar(select(func.count()).select_from(Job).where(Job.status.in_(["RUNNING", "PAUSING", "CANCELLING"]), Job.lease_at >= now() - timedelta(seconds=90))) or 0) >= settings.max_concurrent_jobs:
                return None
            job = session.scalar(select(Job).where(Job.status == "QUEUED", (Job.retry_after.is_(None) | (Job.retry_after <= now()))).order_by(Job.priority.desc(), Job.created_at.asc()).limit(1))
            if not job: return None
            lease = f"dispatcher-{os.getpid()}-{time.time_ns()}"
            result = session.execute(update(Job).where(Job.id == job.id, Job.status == "QUEUED").values(
                status="RUNNING", lease_id=lease, lease_at=now(), stage_deadline_at=now() + timedelta(seconds=300)))
            if result.rowcount != 1: session.rollback(); return None
            session.commit(); return job.id, lease

    def _spawn(self, job_id: str, lease_id: str) -> None:
        log_path = settings.logs_root / "workers" / f"{job_id}.log"
        try:
            child = spawn_owned([sys.executable, "-X", "utf8", "-m", "backend.worker", job_id, "--lease", lease_id],
                                cwd=Path(__file__).parents[2], log_path=log_path,
                                job_name=f"VideoAuto-worker-{os.getpid()}-{job_id}-{time.time_ns()}")
        except (OSError, RuntimeError) as exc:
            with SessionLocal() as session:
                result = session.execute(update(Job).where(Job.id == job_id, Job.lease_id == lease_id).values(
                    status="QUEUED", lease_id=None, lease_at=None, stage_deadline_at=None, error=f"worker start failed: {exc}"))
                session.commit()
            return
        with self._lock: self._children[job_id] = child; self._started_at[job_id] = time.monotonic()
        # ``worker_started_at`` doubles as the persisted process creation
        # fence. The DB stores UTC datetimes, while psutil reports epoch
        # seconds; this preserves enough precision for PID reuse checks after
        # a supervisor restart without adding a second legacy column.
        created_at = datetime.fromtimestamp(child.identity.create_time, timezone.utc) if child.identity else now()
        with SessionLocal() as session:
            result = session.execute(update(Job).where(Job.id == job_id, Job.lease_id == lease_id,
                                                       Job.status == "RUNNING").values(
                worker_pid=child.pid, worker_started_at=created_at, updated_at=now()))
            if result.rowcount != 1:
                session.rollback()
                with self._lock:
                    self._children.pop(job_id, None); self._started_at.pop(job_id, None)
                terminate_owned(child)
                close_owned(child)
                return
            session.commit()

    def _reap(self) -> None:
        with self._lock:
            finished = [job_id for job_id, child in self._children.items() if child.process.poll() is not None]
            overdue = [job_id for job_id, started in self._started_at.items() if time.monotonic() - started > 2 * 60 * 60 and job_id in self._children]
        for job_id in overdue:
            with self._lock: child = self._children.get(job_id)
            if child and not terminate_owned(child):
                self._record_error("watchdog", f"worker termination not confirmed for {job_id}")
                continue
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                lease_id = job.lease_id if job and job.status in {"RUNNING", "PAUSING", "CANCELLING"} else None
            if lease_id is not None:
                self._release_owned(job_id, lease_id, "worker exceeded two-hour watchdog")
        for job_id in finished:
            with self._lock: child = self._children.pop(job_id, None)
            with self._lock: self._started_at.pop(job_id, None)
            if not child: continue
            returncode = child.process.returncode
            close_owned(child)
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job and job.status in {"RUNNING", "FAILED"}:
                    if job.retry_count < len(settings.retry_delays):
                        delay = settings.retry_delays[job.retry_count]; job.retry_count += 1; job.status = "QUEUED"; job.retry_after = now() + timedelta(seconds=delay); job.error = f"worker exited ({returncode}); retry scheduled"
                    else:
                        job.status = "FAILED"; job.error = f"worker exited with code {returncode}"
                    job.lease_id = None; job.lease_at = None; job.worker_pid = None; job.worker_started_at = None; job.stage_deadline_at = None; session.commit()
                elif job and job.status in {"CANCELLING", "CANCELLED"}:
                    job.status = "CANCELLED"; job.lease_id = None; job.lease_at = None; job.worker_pid = None; job.worker_started_at = None; job.stage_deadline_at = None; session.commit()
                elif job and job.status == "PAUSING":
                    job.status = "PAUSED"; job.pause_requested = False; job.pause_acknowledged = True; job.lease_id = None; job.lease_at = None; job.worker_pid = None; job.worker_started_at = None; job.stage_deadline_at = None; session.commit()

    def cancel(self, job_id: str) -> bool:
        with self._lock: child = self._children.get(job_id)
        if not child:
            with SessionLocal() as session:
                job = session.get(Job, job_id); pid = job.worker_pid if job else None
            if not pid: return False
            if not self._terminate_job_process(job): return False
            with SessionLocal() as session:
                result = session.execute(update(Job).where(Job.id == job_id, Job.worker_pid == pid,
                                                           Job.status.in_(["RUNNING", "PAUSING", "CANCELLING"])).values(
                    status="CANCELLED", cancel_requested=True, lease_id=None, lease_at=None,
                    worker_pid=None, worker_started_at=None, stage_deadline_at=None, updated_at=now()))
                session.commit()
            return result.rowcount == 1
        if child.is_running() and not terminate_owned(child):
            return False
        with self._lock: self._children.pop(job_id, None); self._started_at.pop(job_id, None)
        close_owned(child)
        with SessionLocal() as session:
            result = session.execute(update(Job).where(Job.id == job_id, Job.worker_pid == child.pid,
                                                       Job.status.in_(["RUNNING", "PAUSING", "CANCELLING"])).values(
                status="CANCELLED", cancel_requested=True, lease_id=None, lease_at=None,
                worker_pid=None, worker_started_at=None, stage_deadline_at=None, updated_at=now()))
            session.commit()
        return result.rowcount == 1

    def _watch_folders(self) -> None:
        from .api import create_source_and_job
        with SessionLocal() as session:
            folders = list(session.scalars(select(WatchFolder).where(WatchFolder.enabled.is_(True))))
        for folder in folders:
            root = Path(folder.path)
            if not root.is_dir() or root.is_symlink(): continue
            resolved_root = root.resolve()
            if any(str(resolved_root).lower() == str(excluded.resolve()).lower() or str(resolved_root).lower().startswith(str(excluded.resolve()).lower() + os.sep) for excluded in (settings.data_root, settings.output_root)):
                continue
            for path in root.iterdir():
                if not path.is_file() or path.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"} or path.name.endswith(".partial"): continue
                try:
                    stat_a = path.stat(); key = str(path.resolve())
                    preset = json.loads(folder.preset_json); preset_hash = __import__("hashlib").sha256(json.dumps(preset, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
                    with SessionLocal() as session:
                        seen = session.scalar(select(WatchSeen).where(WatchSeen.folder_id == folder.id, WatchSeen.path == key).order_by(WatchSeen.id.desc()).limit(1))
                        if seen and seen.size_bytes == stat_a.st_size and seen.mtime_ns == stat_a.st_mtime_ns and seen.sha256 != "pending" and seen.preset_hash == preset_hash: continue
                        # Require the same size+mtime to be observed in two scans spanning stable_seconds.
                        # A preset change is a new observation too; otherwise the
                        # completed marker would suppress that new render forever.
                        if seen is None or seen.size_bytes != stat_a.st_size or seen.mtime_ns != stat_a.st_mtime_ns or seen.preset_hash != preset_hash:
                            if seen: session.delete(seen)
                            session.add(WatchSeen(folder_id=folder.id, path=key, size_bytes=stat_a.st_size, mtime_ns=stat_a.st_mtime_ns, sha256="pending", preset_hash=preset_hash)); session.commit(); continue
                    # The stable marker is old enough and is replaced with its content hash atomically.
                    with SessionLocal() as session:
                        marker = session.scalar(select(WatchSeen).where(WatchSeen.folder_id == folder.id, WatchSeen.path == key).order_by(WatchSeen.id.desc()).limit(1))
                        marker_time = marker.created_at.replace(tzinfo=timezone.utc) if marker and marker.created_at and marker.created_at.tzinfo is None else (marker.created_at if marker else None)
                        if not marker or marker.sha256 != "pending" or not marker_time or time.time() - marker_time.timestamp() < settings.stable_seconds: continue
                    digest = sha256_file(path)
                    source, job = create_source_and_job(path, preset=preset, watch_folder_id=folder.id, expected_sha256=digest)
                    with SessionLocal() as session:
                        marker = session.scalar(select(WatchSeen).where(WatchSeen.folder_id == folder.id, WatchSeen.path == key).order_by(WatchSeen.id.desc()).limit(1))
                        if marker: marker.sha256 = digest; marker.preset_hash = preset_hash; marker.job_id = job.id; session.commit()
                except (OSError, ValueError): continue


supervisor = QueueSupervisor()
