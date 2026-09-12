param([switch]$SkipFrontend)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PythonExe = if (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { 'python' }
$Venv = Join-Path $ProjectRoot '.venv'
if (!(Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
    & $PythonExe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed' }
}
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$LockFile = Join-Path $ProjectRoot 'requirements-windows-py314.lock'
if (!(Test-Path -LiteralPath $LockFile -PathType Leaf)) { throw "Dependency lock is missing: $LockFile" }
# The lock pins the complete tested Python 3.14 CPU environment. It has no
# hashes because the wheel hashes have not been independently verified for
# every Windows build; exact versions still prevent resolver drift.
& $VenvPython -m pip install --disable-pip-version-check --upgrade-strategy only-if-needed -r $LockFile
if ($LASTEXITCODE -ne 0) { throw 'locked application dependency installation failed' }
if (!$SkipFrontend) {
    $FrontendRoot = Join-Path $ProjectRoot 'frontend'
    $FrontendLock = Join-Path $FrontendRoot 'package-lock.json'
    if (!(Test-Path -LiteralPath $FrontendLock -PathType Leaf)) { throw "Frontend lock is missing: $FrontendLock" }
    Push-Location $FrontendRoot
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
    } finally { Pop-Location }
}

$RuntimeBin = Join-Path $ProjectRoot '.runtime\ffmpeg\ffmpeg-9.0.1-essentials_build\bin'
$WhisperRelease = 'C:\Users\PC\Downloads\whisper-bin-x64\Release'
$ModelRoot = 'C:\Users\PC\Documents\Codex\2026-09-05\ti\work\local-speech\models'
$FontSource = Join-Path $ProjectRoot '.runtime\fonts'
$EnvFile = Join-Path $ProjectRoot '.runtime\videoauto.env'
New-Item (Split-Path $EnvFile) -ItemType Directory -Force | Out-Null

$RawDataRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'VideoAuto'
$RawOutputRoot = Join-Path ([Environment]::GetFolderPath('MyVideos')) 'Video Auto Exports'
# Resolve through the same Python runtime that launches the app. Packaged
# Windows environments can redirect AppData; storing this physical value
# prevents the scheduled supervisor and API from opening different databases.
$env:VIDEOAUTO_SETUP_DATA = $RawDataRoot
$env:VIDEOAUTO_SETUP_OUTPUT = $RawOutputRoot
$CanonicalDataRoot = $null
$CanonicalOutputRoot = $null
# Reuse paths already written by an earlier packaged invocation. The outer
# PowerShell process may see the unredirected AppData spelling on a rerun,
# while the service must continue using the existing physical LocalCache path.
if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^VIDEOAUTO_DATA_ROOT=(.*)$') { $CanonicalDataRoot = $Matches[1] }
        if ($line -match '^VIDEOAUTO_OUTPUT_ROOT=(.*)$') { $CanonicalOutputRoot = $Matches[1] }
    }
}
if (!$CanonicalDataRoot) {
    $CanonicalDataRoot = (& $VenvPython -X utf8 -c "from pathlib import Path; import os; print(Path(os.environ['VIDEOAUTO_SETUP_DATA']).resolve())").Trim()
}
if (!$CanonicalOutputRoot) {
    $CanonicalOutputRoot = (& $VenvPython -X utf8 -c "from pathlib import Path; import os; print(Path(os.environ['VIDEOAUTO_SETUP_OUTPUT']).resolve())").Trim()
}
if ($LASTEXITCODE -ne 0 -or !$CanonicalDataRoot -or !$CanonicalOutputRoot) { throw 'failed to resolve managed data paths' }
Remove-Item Env:VIDEOAUTO_SETUP_DATA
Remove-Item Env:VIDEOAUTO_SETUP_OUTPUT

$RequiredSources = @(
    (Join-Path $RuntimeBin 'ffmpeg.exe'),
    (Join-Path $RuntimeBin 'ffprobe.exe'),
    (Join-Path $WhisperRelease 'whisper-cli.exe'),
    (Join-Path $ModelRoot 'whisper\ggml-small-q5_1.bin'),
    (Join-Path $ModelRoot 'nllb'),
    (Join-Path $ModelRoot 'piper\banmai.onnx'),
    (Join-Path $ModelRoot 'piper\banmai.onnx.json'),
    $FontSource
)
foreach ($required in $RequiredSources) {
    if (!(Test-Path -LiteralPath $required)) { throw "Required local runtime asset is missing: $required" }
}

# Import complete runtime trees and validate hashes before setup points the
# service at them. Originals remain in place for recovery/re-import.
$ImportOutput = & $VenvPython -X utf8 -m backend.runtime_import `
    '--data-root' $CanonicalDataRoot `
    '--model-root' $ModelRoot `
    '--whisper-release' $WhisperRelease `
    '--ffmpeg' (Join-Path $RuntimeBin 'ffmpeg.exe') `
    '--ffprobe' (Join-Path $RuntimeBin 'ffprobe.exe') `
    '--fonts' $FontSource
if ($LASTEXITCODE -ne 0) { throw 'managed runtime import failed' }
$Imported = ($ImportOutput -join [Environment]::NewLine) | ConvertFrom-Json
foreach ($key in @('runtime_root', 'whisper_runtime', 'whisper_cli', 'whisper_model', 'nllb_model',
                   'piper_model', 'piper_config', 'ffmpeg', 'ffprobe', 'font_dir')) {
    if (!$Imported.$key) { throw "managed runtime import returned no '$key' path" }
}

$lines = @(
    "VIDEOAUTO_DATA_ROOT=$CanonicalDataRoot",
    "VIDEOAUTO_OUTPUT_ROOT=$CanonicalOutputRoot",
    "VIDEOAUTO_RUNTIME_ROOT=$($Imported.runtime_root)",
    "VIDEOAUTO_FONT_DIR=$($Imported.font_dir)",
    "VIDEOAUTO_FFMPEG=$($Imported.ffmpeg)",
    "VIDEOAUTO_FFPROBE=$($Imported.ffprobe)",
    "VIDEOAUTO_WHISPER=$($Imported.whisper_cli)",
    "VIDEOAUTO_WHISPER_MODEL=$($Imported.whisper_model)",
    "VIDEOAUTO_NLLB_MODEL=$($Imported.nllb_model)",
    "VIDEOAUTO_PIPER_MODEL=$($Imported.piper_model)",
    "VIDEOAUTO_PIPER_CONFIG=$($Imported.piper_config)"
)
Set-Content -LiteralPath $EnvFile -Value $lines -Encoding utf8
Get-Content $EnvFile | Where-Object { $_ -match '^(VIDEOAUTO_[^=]+)=(.*)$' } | ForEach-Object {
    Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
}

& $VenvPython -X utf8 -c "from backend.app.db import init_db; init_db()"
if ($LASTEXITCODE -ne 0) { throw 'database initialization failed' }
& $VenvPython -X utf8 -m alembic -c (Join-Path $ProjectRoot 'alembic.ini') upgrade head
if ($LASTEXITCODE -ne 0) { throw 'database migration failed' }
& $VenvPython -X utf8 -m backend.manifest
if ($LASTEXITCODE -ne 0) { throw 'runtime manifest creation failed' }
& $VenvPython -X utf8 -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Python dependency consistency check failed' }
Write-Host "Installed Video Auto. Environment: $EnvFile"
Write-Host "Managed runtime: $($Imported.runtime_root)"
Write-Host 'Run scripts\start.ps1 to launch the local service.'
