from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def test_concurrent_same_blob_promotes_one_source(tmp_path):
    """Two upload finalizers may share one content-addressed source safely."""
    blob = (b"videoauto-source-" * 100_000)
    first = tmp_path / "upload-A.mp4"; second = tmp_path / "upload-B.mp4"
    first.write_bytes(blob); second.write_bytes(blob)
    data_root = tmp_path / "data"; output_root = tmp_path / "output"
    ready_a = tmp_path / "ready-a"; ready_b = tmp_path / "ready-b"; go = tmp_path / "go"
    child = """
import sys, time
from pathlib import Path
from backend.app.api import create_source_and_job
Path(sys.argv[2]).write_text('ready')
while not Path(sys.argv[3]).exists(): time.sleep(0.01)
source, job = create_source_and_job(Path(sys.argv[1]), {"subtitle_mode": "burn"}, original_name=Path(sys.argv[1]).name)
print(source.id + " " + job.id, flush=True)
"""
    child_env = os.environ.copy()
    child_env.update({"VIDEOAUTO_DATA_ROOT": str(data_root), "VIDEOAUTO_OUTPUT_ROOT": str(output_root),
                      "PYTHONPATH": str(Path.cwd())})
    common = {"cwd": str(Path.cwd()), "env": child_env, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE, "text": True,
              "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    first_process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", child, str(first), str(ready_a), str(go)], **common)
    deadline = time.monotonic() + 30
    while not ready_a.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready_a.exists(), "first upload worker did not initialize"
    # API import performs one additive DB initialization. Stagger only this
    # startup step; both source promotions still begin together at ``go``.
    second_process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", child, str(second), str(ready_b), str(go)], **common)
    deadline = time.monotonic() + 30
    while not ready_b.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready_b.exists(), "second upload worker did not initialize"
    processes = [first_process, second_process]
    go.write_text("go")
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(stdout.strip().split())
    assert all(len(result) == 2 for result in results)
    digest = hashlib.sha256(blob).hexdigest()
    with sqlite3.connect(data_root / "database" / "videoauto.sqlite3") as database:
        source_count = database.execute("SELECT COUNT(*) FROM sources WHERE sha256=?", (digest,)).fetchone()[0]
        job_count = database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert source_count == 1 and job_count == 2
