"""Cross-process serialization for resumable upload files.

SQLite protects the metadata transaction, but it does not serialize random
access writes to the preallocated upload file with a finalizer in another
process.  A small id-derived lock file gives chunk writers and finalization a
single OS-level critical section.  The lock is released by Windows/POSIX when
the owning process exits, so a crash cannot leave a permanent lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def upload_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if __import__("os").name == "nt":
            import msvcrt

            handle.seek(0)
            # The lock file is kept at least one byte long so msvcrt has a
            # byte to lock.  LK_LOCK waits, which is exactly what we need for
            # two browser requests targeting the same upload.
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

