"""Run one real local-model pipeline smoke against a supplied video."""
from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import select

from backend.app.api import create_source_and_job
from backend.app.config import settings
from backend.app.db import Artifact, Job, SessionLocal, init_db
from backend.app.pipeline import run_job


parser = argparse.ArgumentParser(); parser.add_argument("video", type=Path); args = parser.parse_args()
settings.ensure_dirs(); init_db()
source, job = create_source_and_job(args.video, {"audio_mode": "replace", "subtitle_mode": "burn", "include_dubbing": True})
run_job(job.id)
with SessionLocal() as session:
    record = session.get(Job, job.id); print({"job_id": job.id, "status": record.status, "error": record.error, "progress": record.progress})
    print([{"kind": x.kind, "path": x.path, "valid": x.valid} for x in session.scalars(select(Artifact).where(Artifact.job_id == job.id))])
