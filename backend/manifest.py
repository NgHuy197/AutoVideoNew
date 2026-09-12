"""Write an offline runtime manifest with hashes for executable/model inputs."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .app.config import settings
from .runtime_import import directory_entries, sha256_file, tree_sha256


def _directory_row(name: str, path: Path) -> dict:
    """Hash every regular file in a runtime directory, including DLLs."""

    resolved = path.resolve()
    entries = directory_entries(resolved)
    return {"name": name, "path": str(resolved), "kind": "directory",
            "bytes": sum(int(x["bytes"]) for x in entries), "file_count": len(entries),
            "tree_sha256": tree_sha256(entries), "entries": entries}


def _row(name: str, value: str) -> dict | None:
    if not value: return None
    path = Path(value).expanduser().resolve()
    if path.is_file():
        return {"name": name, "path": str(path), "kind": "file", "bytes": path.stat().st_size, "sha256": sha256_file(path)}
    if path.is_dir(): return _directory_row(name, path)
    return None


def _runtime_inputs() -> dict[str, Path]:
    """Return named immutable runtime inputs with the Whisper DLL tree included."""

    configured = settings.manifest()
    inputs: dict[str, Path] = {}
    for name, value in configured.items():
        if name in {"data_root", "output_root", "runtime_root"} or not value:
            continue
        inputs[name] = Path(value)

    # The executable path alone does not prove that whisper-cli's adjacent
    # DLLs were imported.  Hash the complete Release directory as a separate
    # row while retaining the individual executable row for diagnostics.
    runtime_root = settings.runtime_root
    if runtime_root:
        inputs["whisper_runtime"] = runtime_root / "whisper" / "Release"
    elif settings.whisper_cli.parent not in {Path(""), Path(".")}:
        # A bare PATH command (the default before setup) has no adjacent
        # directory to hash. Avoid accidentally hashing the whole project.
        inputs["whisper_runtime"] = settings.whisper_cli.parent
    return inputs


def main() -> None:
    settings.ensure_dirs()
    rows = []
    strict = settings.runtime_root is not None
    for name, path in _runtime_inputs().items():
        item = _row(name, str(path))
        if item is None and strict:
            raise FileNotFoundError(f"managed runtime input is missing: {name}: {path}")
        if item is not None:
            rows.append(item)
    target = settings.config_root / "runtime-manifest.json"
    payload = {"version": 3, "offline": True, "runtime_root": str(settings.runtime_root or ""), "files": rows}
    temporary = target.with_name(f".{target.name}.partial")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    with temporary.open("r+b") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    print(target)


if __name__ == "__main__": main()
