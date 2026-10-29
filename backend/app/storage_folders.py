from __future__ import annotations

import os
from pathlib import Path

from .paths import confined


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _invalid() -> str:
    raise ValueError("folder must be a safe relative path")


def normalize_folder(value: object, *, allow_empty: bool = True) -> str:
    """Return a portable, safe path relative to a managed media root."""
    if value is None:
        return "" if allow_empty else _invalid()
    if not isinstance(value, str):
        return _invalid()
    raw = value.replace("\\", "/").strip().strip("/")
    if not raw:
        return "" if allow_empty else _invalid()
    if len(raw) > 240:
        return _invalid()
    parts = raw.split("/")
    for part in parts:
        if (not part or part in {".", ".."} or part[-1:] in {" ", "."}
                or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES):
            return _invalid()
    return "/".join(parts)


def folder_path(root: Path, relative: object = "") -> Path:
    normalized = normalize_folder(relative)
    target = root.joinpath(*normalized.split("/")) if normalized else root
    return confined(target, root)


def list_folders(root: Path, limit: int = 500) -> list[dict[str, str]]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows = [{"relative": "", "path": str(root), "name": "Thư mục gốc"}]
    pending = [root]
    while pending and len(rows) < limit:
        parent = pending.pop(0)
        try:
            children = sorted((item for item in parent.iterdir() if item.is_dir() and not item.is_symlink()),
                              key=lambda item: item.name.casefold())
        except OSError:
            continue
        for child in children:
            try:
                relative = child.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            rows.append({"relative": relative, "path": str(child.resolve()), "name": child.name})
            pending.append(child)
            if len(rows) >= limit:
                break
    return rows


def create_folder(root: Path, parent: object, name: object) -> dict[str, str]:
    parent_value = normalize_folder(parent)
    name_value = normalize_folder(name, allow_empty=False)
    if "/" in name_value:
        raise ValueError("folder name must contain one level")
    relative = "/".join(value for value in (parent_value, name_value) if value)
    target = folder_path(root, relative)
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError("folder already exists") from exc
    except OSError as exc:
        raise OSError(f"cannot create folder: {exc}") from exc
    return {"relative": relative, "path": str(target), "name": target.name}


def roots_overlap(first: Path, second: Path) -> bool:
    first_value, second_value = str(first.resolve()).casefold(), str(second.resolve()).casefold()
    separator = os.sep.casefold()
    return (first_value == second_value or first_value.startswith(second_value + separator)
            or second_value.startswith(first_value + separator))
