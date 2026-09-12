from __future__ import annotations

import json
import sys
import traceback

from .app.config import settings
from .app.db import Job, SessionLocal, init_db, now
from .app.pipeline import run_job


if __name__ == "__main__":
    settings.ensure_dirs(); init_db()
    if len(sys.argv) not in {2, 4} or (len(sys.argv) == 4 and sys.argv[2] != "--lease"): raise SystemExit("usage: python -m backend.worker JOB_ID [--lease LEASE_ID]")
    job_id = sys.argv[1]; lease_id = sys.argv[3] if len(sys.argv) == 4 else None
    try:
        run_job(job_id, lease_id)
    except BaseException as exc:
        # Pipeline normally persists domain failures itself. This outer guard
        # covers import/runtime faults and abrupt worker errors so a durable
        # owner never remains RUNNING with no explanation. The supervisor still
        # owns retry scheduling based on the non-zero process exit.
        try:
            with SessionLocal() as session:
                query = session.query(Job).filter(Job.id == job_id)
                if lease_id:
                    query = query.filter(Job.lease_id == lease_id)
                job = query.one_or_none()
                if job and job.status in {"RUNNING", "PAUSING"}:
                    details = traceback.format_exc()[-4000:]
                    job.status = "FAILED"; job.error = str(exc)
                    job.warning_json = json.dumps((json.loads(job.warning_json or "[]") + [details])[-8:], ensure_ascii=False, separators=(",", ":"))
                    job.updated_at = now(); session.commit()
        except Exception:
            pass
        raise
