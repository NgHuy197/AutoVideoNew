"""Import the offline runtime into a managed, user-owned data directory.

The source assets are intentionally passed in by the setup script.  They may
live beside the project or in another user directory, but the running service
must use the copies under ``DATA_ROOT/runtime`` so a packaged Windows process
and a normal process resolve the same physical paths.

Only the Python standard library is used here.  Setup can therefore run this
step before the application dependencies are importable, and an interrupted
copy can be retried safely without touching the source assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable


CHUNK_SIZE = 1024 * 1024

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class RuntimeImportError(RuntimeError):
    """Raised when a runtime asset is missing, changing, or fails validation."""


def _lock_file(handle, *, blocking: bool) -> None:
    """Acquire one byte of an OS lock file on Windows or POSIX."""

    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        if not blocking:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        # LK_LOCK retries only a small fixed number of times in the CRT. A
        # model-tree copy can legitimately exceed that window, so retry the
        # non-blocking primitive ourselves with a bounded, explicit timeout.
        deadline = time.monotonic() + 120
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeImportError("timed out waiting for another runtime import") from exc
                time.sleep(0.1)
    else:
        import fcntl

        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), flags)


def _open_lock_file(path: Path):
    """Open a stable one-byte lock file without appending on every acquire."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    return handle


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _runtime_import_lock(data_root: Path):
    """Serialize import cleanup and directory swaps across processes/threads."""

    key = os.path.normcase(str(_absolute(data_root)))
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    # The in-process lock is needed because Windows byte-range locks have
    # subtly different same-process semantics across CRT versions.
    with process_lock:
        lock_path = _absolute(data_root) / ".runtime-import.lock"
        with _open_lock_file(lock_path) as handle:
            acquired = False
            try:
                _lock_file(handle, blocking=True)
                acquired = True
                yield
            finally:
                if acquired:
                    _unlock_file(handle)


def _lock_is_free(path: Path) -> bool:
    """Probe an active-runtime lock without waiting for a running process."""

    path = _absolute(path)
    with _open_lock_file(path) as handle:
        acquired = False
        try:
            _lock_file(handle, blocking=False)
            acquired = True
            return True
        except (OSError, BlockingIOError):
            return False
        finally:
            if acquired:
                _unlock_file(handle)


def _active_runtime_process(data_root: Path) -> str | None:
    """Return an owning service process description, if one is still active.

    The supervisor singleton lock is the authoritative service gate.  The
    process scan covers a worker started directly (for example after a
    supervisor crash) and is best-effort because process environment access can
    be restricted on Windows.
    """

    singleton = _absolute(data_root) / "supervisor.lock"
    if singleton.exists() and not _lock_is_free(singleton):
        return str(singleton)
    try:
        import psutil
    except ImportError:
        return None
    this_pid = os.getpid()
    for process in psutil.process_iter(("pid", "name", "cmdline")):
        try:
            if int(process.info["pid"]) == this_pid:
                continue
            command = " ".join(process.info.get("cmdline") or []).lower()
            if "backend.supervisor" in command or "backend.worker" in command:
                return command
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
    return None


@contextmanager
def _runtime_update_gate(data_root: Path, runtime_root: Path):
    """Keep setup from replacing assets while a service owns the runtime."""

    active = _absolute(runtime_root) / ".runtime-active.lock"
    if not _lock_is_free(active):
        raise RuntimeImportError(f"managed runtime is in use: {active}")
    owner = _active_runtime_process(data_root)
    if owner:
        raise RuntimeImportError(f"managed runtime is in use by {owner}")
    yield


def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Return a deterministic SHA-256 digest for *path*."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeImportError(f"cannot read runtime file: {path}: {exc}") from exc
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    """Reject links/junctions so an import cannot escape its source tree."""

    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(str(path)))


def _absolute(path: Path | str) -> Path:
    """Make a lexical absolute path without following links or junctions."""

    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _reject_reparse_ancestors(path: Path, root: Path, *, label: str) -> None:
    """Reject an existing reparse point before any ``resolve`` or write.

    A lexical ``commonpath`` check alone is insufficient on Windows: a
    junction such as ``DATA_ROOT/runtime`` can redirect a newly-created child
    outside the managed tree.  Walk the existing path components using their
    lexical spelling, so the check observes the junction itself.
    """

    path = _absolute(path)
    root = _absolute(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise RuntimeImportError(f"{label} escapes its intended root: {path}") from exc
    candidates = [root]
    current = root
    for part in relative.parts:
        current = current / part
        candidates.append(current)
    for candidate in candidates:
        if (candidate.exists() or candidate.is_symlink()) and _is_reparse_point(candidate):
            raise RuntimeImportError(f"{label} contains a link or junction: {candidate}")


def _iter_files(root: Path) -> Iterable[Path]:
    """Yield regular files below *root* in stable relative-path order."""

    # Check the spelling supplied by the caller *before* resolving it.  On
    # Windows ``Path.resolve`` follows a junction, making a linked source look
    # indistinguishable from its target afterwards.
    supplied_root = _absolute(root)
    if _is_reparse_point(supplied_root):
        raise RuntimeImportError(f"runtime source cannot be a link or junction: {supplied_root}")
    if not supplied_root.is_dir():
        raise RuntimeImportError(f"runtime directory is missing: {supplied_root}")
    root = supplied_root.resolve()

    files: list[Path] = []
    for base, directories, names in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        # os.walk does not follow ordinary symlink directories with
        # followlinks=False, but explicitly reject them rather than silently
        # producing an incomplete runtime tree.
        for directory in list(directories):
            child = base_path / directory
            if _is_reparse_point(child):
                raise RuntimeImportError(f"runtime source contains a link or junction: {child}")
        for name in names:
            child = base_path / name
            if _is_reparse_point(child):
                raise RuntimeImportError(f"runtime source contains a link or junction: {child}")
            if not child.is_file():
                raise RuntimeImportError(f"runtime source contains a non-file entry: {child}")
            files.append(child)
    return sorted(files, key=lambda item: (item.relative_to(root).as_posix().lower(), item.relative_to(root).as_posix()))


def directory_entries(root: Path) -> list[dict[str, str | int]]:
    """Return file hashes for a directory using stable POSIX relative paths."""

    root = root.resolve()
    entries: list[dict[str, str | int]] = []
    for child in _iter_files(root):
        relative = child.relative_to(root).as_posix()
        entries.append({"path": relative, "bytes": child.stat().st_size, "sha256": sha256_file(child)})
    return entries


def tree_sha256(entries: Iterable[dict[str, str | int]]) -> str:
    """Hash a directory manifest in the same format as ``backend.manifest``."""

    digest = hashlib.sha256()
    for entry in entries:
        digest.update(str(entry["path"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["sha256"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry["bytes"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def directory_digest(root: Path) -> dict[str, object]:
    """Return the complete, hashable description of a directory."""

    entries = directory_entries(root)
    return {
        "bytes": sum(int(entry["bytes"]) for entry in entries),
        "file_count": len(entries),
        "tree_sha256": tree_sha256(entries),
        "entries": entries,
    }


def _require_file(path: Path, label: str) -> Path:
    supplied = _absolute(path)
    if _is_reparse_point(supplied) or not supplied.is_file():
        raise RuntimeImportError(f"required {label} is missing or not a regular file: {supplied}")
    return supplied.resolve()


def _require_directory(path: Path, label: str) -> Path:
    supplied = _absolute(path)
    if _is_reparse_point(supplied) or not supplied.is_dir():
        raise RuntimeImportError(f"required {label} is missing or not a regular directory: {supplied}")
    return supplied.resolve()


def _ensure_destination_inside(path: Path, root: Path) -> None:
    # Do not call Path.resolve() here.  Windows packaged processes can map a
    # newly-created child of a normal-looking AppData path into LocalCache;
    # resolving the child and the root independently then reports a false
    # escape.  The setup script already supplies the canonical physical root;
    # containment for destinations is lexical and stays on that root.
    path_absolute = Path(os.path.abspath(os.fspath(path)))
    root_absolute = Path(os.path.abspath(os.fspath(root)))
    try:
        common = os.path.commonpath([str(path_absolute), str(root_absolute)])
    except ValueError as exc:
        raise RuntimeImportError(f"runtime destination is on a different drive: {path}") from exc
    if os.path.normcase(common) != os.path.normcase(str(root_absolute)):
        raise RuntimeImportError(f"runtime destination escapes DATA_ROOT: {path}")


def _copy_file_atomic(source: Path, destination: Path, expected_sha256: str | None = None,
                      expected_bytes: int | None = None) -> None:
    """Copy one file through a sibling temporary file and verify before rename."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    expected_bytes = source.stat().st_size if expected_bytes is None else expected_bytes
    expected_sha256 = sha256_file(source) if expected_sha256 is None else expected_sha256
    try:
        with source.open("rb") as source_handle, temporary.open("wb") as destination_handle:
            while True:
                chunk = source_handle.read(CHUNK_SIZE)
                if not chunk:
                    break
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        if temporary.stat().st_size != expected_bytes or sha256_file(temporary) != expected_sha256:
            raise RuntimeImportError(f"hash validation failed while importing {source}")
        os.replace(temporary, destination)
        if destination.stat().st_size != expected_bytes or sha256_file(destination) != expected_sha256:
            raise RuntimeImportError(f"hash validation failed after importing {destination}")
    except OSError as exc:
        raise RuntimeImportError(f"cannot import runtime file {source} -> {destination}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _recover_directory_swap(destination: Path, backup: Path) -> None:
    """Recover a crash between the two atomic directory renames."""

    if backup.exists() and not destination.exists():
        os.replace(backup, destination)
    elif backup.exists() and destination.exists():
        # The new directory is already installed; the backup is stale.
        _remove_path(backup)


def _cleanup_staging_directories(destination: Path) -> None:
    """Remove abandoned staging trees left by an interrupted import."""

    prefix = f".{destination.name}.import-"
    for candidate in destination.parent.glob(f"{prefix}*.partial"):
        _remove_path(candidate)


def _copy_directory_atomic(source: Path, destination: Path) -> dict[str, object]:
    """Import a directory via a validated sibling tree and an atomic swap."""

    source = _require_directory(source, "runtime directory")
    destination = Path(os.path.abspath(os.fspath(destination)))
    source_entries = directory_entries(source)
    source_tree = tree_sha256(source_entries)

    backup = destination.with_name(f".{destination.name}.previous")
    _recover_directory_swap(destination, backup)
    _cleanup_staging_directories(destination)
    if destination.is_dir() and not _is_reparse_point(destination):
        current = directory_digest(destination)
        if current["tree_sha256"] == source_tree and current["file_count"] == len(source_entries):
            return current

    stage = destination.with_name(f".{destination.name}.import-{uuid.uuid4().hex}.partial")
    _remove_path(stage)
    try:
        stage.mkdir(parents=True, exist_ok=False)
        for entry in source_entries:
            relative = Path(str(entry["path"]))
            _copy_file_atomic(source / relative, stage / relative,
                              expected_sha256=str(entry["sha256"]), expected_bytes=int(entry["bytes"]))
        # Re-read both source and stage.  A source changed during the copy is
        # rejected and never becomes the managed runtime.
        if tree_sha256(directory_entries(source)) != source_tree:
            raise RuntimeImportError(f"runtime source changed during import: {source}")
        imported = directory_digest(stage)
        if imported["tree_sha256"] != source_tree or imported["file_count"] != len(source_entries):
            raise RuntimeImportError(f"hash validation failed for imported runtime directory: {source}")

        if destination.exists() or destination.is_symlink():
            if _is_reparse_point(destination):
                raise RuntimeImportError(f"runtime destination cannot be a link or junction: {destination}")
            _remove_path(backup)
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except OSError:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        _remove_path(backup)
        return imported
    except OSError as exc:
        raise RuntimeImportError(f"cannot import runtime directory {source} -> {destination}: {exc}") from exc
    finally:
        _remove_path(stage)


def _copy_file_if_needed(source: Path, destination: Path) -> dict[str, object]:
    source = _require_file(source, "runtime file")
    expected_bytes = source.stat().st_size
    expected_sha256 = sha256_file(source)
    if destination.is_file() and not _is_reparse_point(destination):
        if destination.stat().st_size == expected_bytes and sha256_file(destination) == expected_sha256:
            return {"bytes": expected_bytes, "sha256": expected_sha256}
    _copy_file_atomic(source, destination, expected_sha256, expected_bytes)
    return {"bytes": expected_bytes, "sha256": expected_sha256}


def import_runtime_assets(data_root: Path | str, model_root: Path | str, whisper_release: Path | str,
                          ffmpeg: Path | str, ffprobe: Path | str, fonts: Path | str) -> dict[str, str]:
    """Copy all required offline assets and return managed environment paths."""

    # Keep the caller's physical path spelling.  Path.resolve() can apply
    # package redirection to a child that already exists while leaving the
    # parent unchanged, which breaks otherwise valid containment checks.
    data_root = _absolute(data_root)
    # DATA_ROOT is a user-selected trust boundary.  Do not silently create or
    # write through a junction supplied at that boundary.
    if (data_root.exists() or data_root.is_symlink()) and _is_reparse_point(data_root):
        raise RuntimeImportError(f"managed data root cannot be a link or junction: {data_root}")
    data_root.mkdir(parents=True, exist_ok=True)
    if not data_root.is_dir():
        raise RuntimeImportError(f"managed data root is not a directory: {data_root}")
    model_root = _require_directory(Path(model_root), "model root")
    whisper_release = _require_directory(Path(whisper_release), "Whisper Release directory")
    ffmpeg = _require_file(Path(ffmpeg), "FFmpeg")
    ffprobe = _require_file(Path(ffprobe), "FFprobe")
    fonts = _require_directory(Path(fonts), "font directory")
    if not any(path.suffix.lower() in {".ttf", ".otf", ".ttc"} for path in _iter_files(fonts)):
        raise RuntimeImportError(f"font directory contains no supported font files: {fonts}")

    whisper_cli = _require_file(whisper_release / "whisper-cli.exe", "Whisper CLI")
    whisper_model = _require_file(model_root / "whisper" / "ggml-small-q5_1.bin", "Whisper model")
    nllb_model = _require_directory(model_root / "nllb", "NLLB model directory")
    piper_model = _require_file(model_root / "piper" / "banmai.onnx", "Piper model")
    piper_config = _require_file(model_root / "piper" / "banmai.onnx.json", "Piper model config")

    runtime_root = data_root / "runtime"
    _ensure_destination_inside(runtime_root, data_root)
    paths = {
        "runtime_root": runtime_root,
        "whisper_runtime": runtime_root / "whisper" / "Release",
        "whisper_cli": runtime_root / "whisper" / "Release" / whisper_cli.name,
        "whisper_model": runtime_root / "models" / "whisper" / whisper_model.name,
        "nllb_model": runtime_root / "models" / "nllb",
        "piper_model": runtime_root / "models" / "piper" / piper_model.name,
        "piper_config": runtime_root / "models" / "piper" / piper_config.name,
        "ffmpeg": runtime_root / "ffmpeg" / ffmpeg.name,
        "ffprobe": runtime_root / "ffmpeg" / ffprobe.name,
        "font_dir": runtime_root / "fonts",
    }
    for path in paths.values():
        _ensure_destination_inside(path, data_root)

    # Both checks must happen before any helper creates a child directory.
    # Repeat them after taking the importer lock to close the ordinary
    # check-then-swap window between two setup invocations.
    _reject_reparse_ancestors(runtime_root, data_root, label="runtime destination")
    with _runtime_import_lock(data_root):
        _reject_reparse_ancestors(runtime_root, data_root, label="runtime destination")
        for path in paths.values():
            _reject_reparse_ancestors(path, data_root, label="runtime destination")
        with _runtime_update_gate(data_root, runtime_root):
            _copy_directory_atomic(whisper_release, paths["whisper_runtime"])
            _copy_file_if_needed(whisper_model, paths["whisper_model"])
            _copy_directory_atomic(nllb_model, paths["nllb_model"])
            _copy_file_if_needed(piper_model, paths["piper_model"])
            _copy_file_if_needed(piper_config, paths["piper_config"])
            _copy_file_if_needed(ffmpeg, paths["ffmpeg"])
            _copy_file_if_needed(ffprobe, paths["ffprobe"])
            _copy_directory_atomic(fonts, paths["font_dir"])

    return {name: str(path) for name, path in paths.items()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--model-root", required=True, type=Path)
    parser.add_argument("--whisper-release", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--fonts", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = import_runtime_assets(args.data_root, args.model_root, args.whisper_release,
                                       args.ffmpeg, args.ffprobe, args.fonts)
    except RuntimeImportError as exc:
        print(f"runtime import failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
