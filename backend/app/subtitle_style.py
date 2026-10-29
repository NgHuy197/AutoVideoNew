from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SubtitleStyle(BaseModel):
    """Portable ASS styling. Sizes and margins are percentages of the frame."""

    model_config = ConfigDict(extra="forbid", strict=True)

    # ASS style rows are comma-delimited; reject control/override syntax.
    font_family: str = Field(default="Noto Sans", min_length=1, max_length=80,
                             pattern=r"^[^,\\{}\x00-\x1f\x7f]+$")
    font_size: float = Field(default=5.5, ge=1, le=15, allow_inf_nan=False)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9a-fA-F]{6}$")
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    outline_width: float = Field(default=3, ge=0, le=8, allow_inf_nan=False)
    shadow: float = Field(default=1, ge=0, le=8, allow_inf_nan=False)
    # ASS numpad alignment: 1–3 bottom, 4–6 middle, 7–9 top.
    alignment: int = Field(default=2, ge=1, le=9)
    margin_v: float = Field(default=4.5, ge=0, le=30, allow_inf_nan=False)
    margin_h: float = Field(default=2.083333, ge=0, le=30, allow_inf_nan=False)
    bold: bool = False
    italic: bool = False
    max_chars: int = Field(default=42, ge=10, le=100)
    max_lines: int = Field(default=2, ge=1, le=4)


def normalize_subtitle_style(value: dict | SubtitleStyle | None) -> dict:
    style = value if isinstance(value, SubtitleStyle) else SubtitleStyle.model_validate({} if value is None else value)
    result = style.model_dump()
    result["font_family"] = result["font_family"].strip()
    if not result["font_family"]:
        raise ValueError("font_family must not be blank")
    for key in ("color", "outline_color"):
        result[key] = result[key].upper()
    return result
