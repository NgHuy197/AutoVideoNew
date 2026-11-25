"""Human-readable sizes for messages shown to the user (mirrors frontend/src/units.ts)."""
from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB")


def format_bytes(size: float) -> str:
    """1024 -> "1 KB", 1536 -> "1,5 KB", 21474836480 -> "20 GB" (Vietnamese decimal comma)."""
    if size < 0: return "—"
    value, unit = float(size), 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024; unit += 1
    if unit == 0: return f"{round(value)} B"
    if round(value) >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024; unit += 1
    text = f"{value:.1f}".rstrip("0").rstrip(".") if value < 10 else f"{value:.0f}"
    return f"{text.replace('.', ',')} {_UNITS[unit]}"
