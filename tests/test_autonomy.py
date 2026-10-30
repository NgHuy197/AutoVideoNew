from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import api, service, watchdog
from backend.app.db import Base, Job, WatchFolder, WatchSeen, now


def test_watchdog_grace_threshold_and_recovery():
    monitor = watchdog.ApiWatchdog()
    monitor.started(100)
    def bad(): return False
    assert not monitor.should_restart(159, bad)
    assert not monitor.should_restart(160, bad)
    assert not monitor.should_restart(161, bad)  # no rapid repeated probes
    assert not monitor.should_restart(175, bad)
    assert not monitor.should_restart(190, lambda: True)
    assert not monitor.should_restart(205, bad)
    assert not monitor.should_restart(220, bad)
    assert monitor.should_restart(235, bad)


def test_restart_backoff_is_bounded_and_resets_only_after_stability():
    monitor = watchdog.ApiWatchdog()
    for _ in range(100): monitor.failed(10)
    assert monitor.next_start == 266
    monitor.started(300)
    monitor.should_restart(360, lambda: True)
    assert monitor.restarts == 8
    monitor.should_restart(600, lambda: True)
    assert monitor.restarts == 0


def test_probe_rejects_malformed_or_wrong_service(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'[]'
    monkeypatch.setattr(watchdog, "build_opener", lambda *args: SimpleNamespace(open=lambda *a, **k: Response()))
    assert not watchdog.api_healthy()


def test_background_failure_does_not_skip_reaping_or_maintenance(monkeypatch):
    queue = service.QueueSupervisor()
    calls = []
    def fail(): raise OSError("folder disconnected")
    monkeypatch.setattr(queue, "_recover_stale", fail)
    monkeypatch.setattr(queue, "_handle_control_requests", lambda: calls.append("controls"))
    monkeypatch.setattr(queue, "_reap", lambda: calls.append("reap"))
    monkeypatch.setattr(queue, "_watch_folders", fail)
    monkeypatch.setattr(queue, "_maintenance", lambda: calls.append("maintenance"))
    monkeypatch.setattr(queue, "_record_error", lambda component, exc: calls.append(component))
    queue._background_steps()
    assert calls == ["recovery", "controls", "reap", "watch", "maintenance"]


def test_power_request_is_released_on_loop_failure(monkeypatch):
    queue = service.QueueSupervisor()
    calls = []
    def stop(): raise RuntimeError("stop")
    monkeypatch.setattr(queue, "_run_loop", stop)
    monkeypatch.setattr(queue._idle_inhibitor, "clear", lambda: calls.append("clear"))
    import pytest
    with pytest.raises(RuntimeError): queue._loop()
    assert calls == ["clear"]


def test_watch_backpressure_resumes_without_losing_files(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(service, "SessionLocal", sessions)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    for name in ("data_root", "sources_root", "output_root"):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setattr(service.settings, name, path)
    monkeypatch.setattr(service.settings, "watch_queue_limit", 1)
    monkeypatch.setattr(service.settings, "watch_batch_size", 1)
    monkeypatch.setattr(service.settings, "min_free_bytes", 0)
    folder = tmp_path / "watch"
    folder.mkdir()
    for index in range(2): (folder / f"{index}.mp4").write_bytes(bytes([index]))
    with sessions() as session:
        session.add(WatchFolder(path=str(folder)))
        session.commit()
    queue = service.QueueSupervisor()
    monkeypatch.setattr(queue, "_resources_ready", lambda: True)
    queue._watch_folders()  # observations, files still stabilizing
    with sessions() as session:
        for row in session.scalars(select(WatchSeen)): row.created_at = now() - timedelta(minutes=1)
        session.commit()
    queue._watch_folders()
    queue._watch_folders()  # queue full, second file must stay pending
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1
        jobs[0].status = "COMPLETED"
        session.commit()
    queue._watch_folders()
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 2
    assert len(list(folder.glob('*.mp4'))) == 2
    engine.dispose()


def test_watch_skips_scanning_when_resources_are_low(monkeypatch):
    queue = service.QueueSupervisor()
    monkeypatch.setattr(queue, "_resources_ready", lambda: False)
    def unexpected(): raise AssertionError("must not query or copy")
    monkeypatch.setattr(service, "SessionLocal", unexpected)
    queue._watch_folders()


def test_error_cache_is_bounded(monkeypatch, tmp_path):
    queue = service.QueueSupervisor()
    monkeypatch.setattr(service, "append_json_line", lambda *args: None)
    for index in range(1000): queue._record_error("watch", f"bad file {index}")
    assert len(queue._last_error) <= 256


def test_queue_health_detects_dead_and_stuck_threads(monkeypatch):
    queue = service.QueueSupervisor()
    assert not queue.is_healthy()
    queue._thread = SimpleNamespace(is_alive=lambda: True)
    queue._last_tick = 100
    monkeypatch.setattr(service.time, "monotonic", lambda: 999)
    assert queue.is_healthy()
    monkeypatch.setattr(service.time, "monotonic", lambda: 1000)
    assert not queue.is_healthy()


def test_legacy_watch_schema_upgrades_without_losing_markers(tmp_path, monkeypatch):
    from backend.app import db
    from sqlalchemy import text
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE watch_seen DROP COLUMN preset_hash"))
        conn.execute(text("INSERT INTO watch_seen (folder_id,path,size_bytes,mtime_ns,sha256,created_at) "
                          "VALUES ('folder','source.mp4',100,1,'pending',CURRENT_TIMESTAMP)"))
    monkeypatch.setattr(db, "engine", engine)
    db._migrate_legacy_schema()
    db._migrate_legacy_schema()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT path,preset_hash FROM watch_seen")).all() == [("source.mp4", "")]
    engine.dispose()


def test_resource_readiness_has_recovery_margin(monkeypatch):
    gib = 1024 ** 3
    ram = {"value": 0}
    monkeypatch.setattr(service, "resource_snapshot", lambda root: {
        "disk_free_bytes": 500 * gib, "ram_available_bytes": ram["value"]})
    monkeypatch.setattr(service.settings, "min_free_bytes", 20 * gib)
    monkeypatch.setattr(service.settings, "min_available_ram_bytes", 4 * gib)
    monkeypatch.setattr(service.settings, "ram_recovery_margin_bytes", gib // 2)
    queue = service.QueueSupervisor()

    states = []
    for value in (5 * gib, int(3.9 * gib), int(4.1 * gib), int(4.4 * gib), int(4.6 * gib), int(4.1 * gib)):
        ram["value"] = value
        states.append(queue._resource_state()[0])

    # Below 4 GiB stops; recovery needs 4.5 GiB; once ready, 4.1 GiB is enough.
    assert states == [True, False, False, False, True, True]


def test_resume_from_standby_restarts_queue_stall_timer(monkeypatch):
    queue = service.QueueSupervisor()
    queue._thread = SimpleNamespace(is_alive=lambda: True)
    queue._last_tick = 100
    monkeypatch.setattr(service.time, "monotonic", lambda: 8000)
    assert not queue.is_healthy()

    queue.note_resume()

    assert queue.is_healthy()


def test_resume_from_standby_delays_api_probes():
    monitor = watchdog.ApiWatchdog()
    monitor.failures = 2
    monitor.resumed(1000)

    assert not monitor.should_restart(1059, lambda: False)
    assert monitor.failures == 0
    assert not monitor.should_restart(1060, lambda: False)
    assert monitor.failures == 1
