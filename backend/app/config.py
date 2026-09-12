from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# The application is intentionally offline-first. Transformers/Piper must never
# attempt a Hub lookup or telemetry call while processing a local job.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}


def _windows_default(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else fallback


@dataclass
class AppSettings:
    """Settings are resolved once at process start and can be overridden by DB settings."""

    data_root: Path = field(default_factory=lambda: _windows_default("VIDEOAUTO_DATA_ROOT", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VideoAuto"))
    output_root: Path = field(default_factory=lambda: _windows_default("VIDEOAUTO_OUTPUT_ROOT", Path(os.environ.get("USERPROFILE", Path.home())) / "Videos" / "Video Auto Exports"))
    ffmpeg: Path = field(default_factory=lambda: Path(os.environ.get("VIDEOAUTO_FFMPEG", "ffmpeg")))
    ffprobe: Path = field(default_factory=lambda: Path(os.environ.get("VIDEOAUTO_FFPROBE", "ffprobe")))
    runtime_root: Path | None = field(default_factory=lambda: Path(os.environ["VIDEOAUTO_RUNTIME_ROOT"]).resolve() if os.environ.get("VIDEOAUTO_RUNTIME_ROOT") else None)
    whisper_cli: Path = field(default_factory=lambda: Path(os.environ.get("VIDEOAUTO_WHISPER", "whisper-cli.exe")))
    whisper_model: Path | None = field(default_factory=lambda: Path(os.environ["VIDEOAUTO_WHISPER_MODEL"]) if os.environ.get("VIDEOAUTO_WHISPER_MODEL") else None)
    nllb_model: Path | None = field(default_factory=lambda: Path(os.environ["VIDEOAUTO_NLLB_MODEL"]) if os.environ.get("VIDEOAUTO_NLLB_MODEL") else None)
    piper_model: Path | None = field(default_factory=lambda: Path(os.environ["VIDEOAUTO_PIPER_MODEL"]) if os.environ.get("VIDEOAUTO_PIPER_MODEL") else None)
    piper_config: Path | None = field(default_factory=lambda: Path(os.environ["VIDEOAUTO_PIPER_CONFIG"]) if os.environ.get("VIDEOAUTO_PIPER_CONFIG") else None)
    font_dir: Path | None = field(default_factory=lambda: Path(os.environ["VIDEOAUTO_FONT_DIR"]) if os.environ.get("VIDEOAUTO_FONT_DIR") else None)
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_duration_seconds: int = 30 * 60
    stable_seconds: int = 30
    cpu_threads: int = 6
    max_concurrent_jobs: int = 1
    min_free_bytes: int = 20 * 1024 * 1024 * 1024
    min_available_ram_bytes: int = 4 * 1024 * 1024 * 1024
    retry_delays: tuple[int, ...] = (30, 120)
    backup_retention_days: int = 7
    partial_upload_retention_hours: int = 24

    def __post_init__(self) -> None:
        self.data_root = self.data_root.resolve()
        self.output_root = self.output_root.resolve()
        self.sources_root = self.data_root / "sources"
        self.jobs_root = self.data_root / "jobs"
        self.inbox_root = self.data_root / "inbox"
        self.cache_root = self.data_root / "cache"
        self.logs_root = self.data_root / "logs"
        self.database_root = self.data_root / "database"
        self.config_root = self.data_root / "config"
        self.backups_root = self.data_root / "backups"
        self.tools_root = self.data_root / "tools"
        if self.runtime_root is not None:
            self.runtime_root = self.runtime_root.resolve()

    def ensure_dirs(self) -> None:
        for path in (self.data_root, self.output_root, self.sources_root, self.jobs_root, self.inbox_root,
                     self.cache_root, self.logs_root, self.database_root, self.config_root,
                     self.backups_root, self.tools_root):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{(self.database_root / 'videoauto.sqlite3').as_posix()}"

    def manifest(self) -> dict:
        return {"data_root": str(self.data_root), "output_root": str(self.output_root),
                "ffmpeg": str(self.ffmpeg), "ffprobe": str(self.ffprobe),
                "runtime_root": str(self.runtime_root or ""),
                "whisper_cli": str(self.whisper_cli), "whisper_model": str(self.whisper_model or ""),
                "nllb_model": str(self.nllb_model or ""), "piper_model": str(self.piper_model or ""),
                "piper_config": str(self.piper_config or ""), "font_dir": str(self.font_dir or "")}

    def snapshot(self) -> dict:
        """Return the complete immutable runtime contract for a new job.

        The queue stores this value at creation time so a later settings edit
        cannot silently change the model, resource budget, or media paths used
        by an already accepted job.  Keep this explicit rather than relying on
        ``dataclasses.asdict``: derived paths and optional model paths need to
        be serialized as strings and the groups are useful to API consumers.
        """
        return {
            "schema_version": 1,
            "paths": self.manifest(),
            "resources": {
                "cpu_threads": int(self.cpu_threads),
                "max_concurrent_jobs": int(self.max_concurrent_jobs),
                "min_free_bytes": int(self.min_free_bytes),
                "min_available_ram_bytes": int(self.min_available_ram_bytes),
            },
            "limits": {
                "max_file_bytes": int(self.max_file_bytes),
                "max_duration_seconds": int(self.max_duration_seconds),
                "stable_seconds": int(self.stable_seconds),
            },
            "retry_delays": [int(value) for value in self.retry_delays],
            "retention": {
                "backup_retention_days": int(self.backup_retention_days),
                "partial_upload_retention_hours": int(self.partial_upload_retention_hours),
            },
            "offline": True,
        }


settings = AppSettings()
