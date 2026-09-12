from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from backend.manifest import _directory_row
from backend.runtime_import import RuntimeImportError, directory_digest, import_runtime_assets


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def packaging_root() -> Path:
    # The packaged Windows runner denies enumeration of its global TEMP root.
    with tempfile.TemporaryDirectory(prefix="packaging-test-", dir=Path(".runtime")) as value:
        yield Path(value)


def _sources(root: Path) -> dict[str, Path]:
    model_root = root / "models"
    whisper = root / "whisper-release"
    fonts = root / "fonts"
    _write(model_root / "whisper" / "ggml-small-q5_1.bin", b"whisper model")
    _write(model_root / "nllb" / "config.json", b"{}")
    _write(model_root / "nllb" / "weights" / "pytorch_model.bin", b"nllb weights")
    _write(model_root / "piper" / "banmai.onnx", b"piper model")
    _write(model_root / "piper" / "banmai.onnx.json", b"{\"sample_rate\": 22050}")
    _write(whisper / "whisper-cli.exe", b"whisper executable")
    _write(whisper / "whisper.dll", b"whisper dll")
    _write(whisper / "ggml-cpu-x64.dll", b"ggml dll")
    _write(root / "bin" / "ffmpeg.exe", b"ffmpeg")
    _write(root / "bin" / "ffprobe.exe", b"ffprobe")
    _write(fonts / "NotoSans-Regular.ttf", b"font")
    _write(fonts / "LICENSE", b"license")
    return {
        "model_root": model_root,
        "whisper_release": whisper,
        "ffmpeg": root / "bin" / "ffmpeg.exe",
        "ffprobe": root / "bin" / "ffprobe.exe",
        "fonts": fonts,
    }


def test_runtime_import_is_complete_idempotent_and_preserves_sources(packaging_root: Path) -> None:
    source = _sources(packaging_root / "source")
    data_root = packaging_root / "managed"
    result = import_runtime_assets(data_root, **source)

    runtime = Path(result["runtime_root"])
    assert Path(result["whisper_cli"]).is_file()
    assert Path(result["whisper_runtime"]) == runtime / "whisper" / "Release"
    assert (runtime / "whisper" / "Release" / "whisper.dll").read_bytes() == b"whisper dll"
    assert (runtime / "models" / "nllb" / "weights" / "pytorch_model.bin").read_bytes() == b"nllb weights"
    assert (runtime / "models" / "piper" / "banmai.onnx.json").read_bytes() == b"{\"sample_rate\": 22050}"
    assert directory_digest(Path(result["whisper_runtime"]))["tree_sha256"] == directory_digest(source["whisper_release"])["tree_sha256"]
    assert directory_digest(Path(result["nllb_model"]))["tree_sha256"] == directory_digest(source["model_root"] / "nllb")["tree_sha256"]

    source_hashes = {path: _sha256(path) for path in source["whisper_release"].rglob("*") if path.is_file()}
    stale_stage = Path(result["whisper_runtime"]).with_name(".Release.import-stale.partial")
    stale_stage.mkdir(parents=True)
    _write(stale_stage / "abandoned.bin", b"abandoned")
    second = import_runtime_assets(data_root, **source)
    assert second == result
    assert {path: _sha256(path) for path in source["whisper_release"].rglob("*") if path.is_file()} == source_hashes
    assert not list(runtime.rglob("*.partial"))
    assert not list(runtime.rglob("*.previous"))
    assert (data_root / ".runtime-import.lock").stat().st_size == 1


def test_runtime_manifest_hashes_every_whisper_release_file(packaging_root: Path) -> None:
    release = packaging_root / "Release"
    _write(release / "whisper-cli.exe", b"exe")
    _write(release / "whisper.dll", b"dll")
    _write(release / "ggml-cpu-x64.dll", b"cpu dll")
    row = _directory_row("whisper_runtime", release)
    assert row["kind"] == "directory"
    assert row["file_count"] == 3
    assert {entry["path"] for entry in row["entries"]} == {"ggml-cpu-x64.dll", "whisper-cli.exe", "whisper.dll"}


def test_windows_lock_has_exact_runtime_pins() -> None:
    lock = Path("requirements-windows-py314.lock").read_text(encoding="utf-8")
    for requirement in (
        "fastapi==0.141.1",
        "piper-tts==1.8.0",
        "torch==2.14.0+cpu",
        "transformers==5.16.1",
        "uvicorn==0.52.4",
    ):
        assert requirement in lock
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in lock


def test_runtime_import_refuses_an_active_service(packaging_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Setup must not replace a model tree while a local service owns it."""

    from backend import runtime_import

    source = _sources(packaging_root / "source")
    data_root = packaging_root / "managed"
    monkeypatch.setattr(runtime_import, "_active_runtime_process", lambda _root: "owned test supervisor")
    with pytest.raises(runtime_import.RuntimeImportError, match="managed runtime is in use"):
        import_runtime_assets(data_root, **source)
    assert not (data_root / "runtime" / "whisper" / "Release" / "whisper-cli.exe").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range lock contract")
def test_runtime_import_refuses_real_cross_process_runtime_lock(packaging_root: Path) -> None:
    """The importer must see the exact lock a supervisor would hold."""

    source = _sources(packaging_root / "source")
    data_root = packaging_root / "managed"
    runtime = data_root / "runtime"
    runtime.mkdir(parents=True)
    lock = runtime / ".runtime-active.lock"
    child_code = """\
import msvcrt, sys, time
p = sys.argv[1]
with open(p, 'a+b') as h:
    h.seek(0); h.write(b'0'); h.flush(); h.seek(0)
    msvcrt.locking(h.fileno(), msvcrt.LK_NBLCK, 1)
    print('READY', flush=True)
    time.sleep(20)
"""
    process = subprocess.Popen([sys.executable, "-c", child_code, str(lock)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "READY"
        with pytest.raises(RuntimeImportError, match="managed runtime is in use"):
            import_runtime_assets(data_root, **source)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=10)
