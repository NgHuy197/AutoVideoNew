# One-click launcher behind the "Video Auto" shortcut (see create-shortcuts.ps1).
# Starts Ollama and the supervisor hidden, shows a loading page at once, and
# lets that page switch to the app as soon as the server answers.
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Url = 'http://127.0.0.1:8765/'
$Live = 'http://127.0.0.1:8765/api/v1/health/live'
$LoadingPage = Join-Path $PSScriptRoot 'launcher\loading.html'

function Test-Live {
    try { $null = Invoke-RestMethod $Live -TimeoutSec 2 -ErrorAction Stop; return $true } catch { return $false }
}

function Show-Error([string]$Text) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Text, 'Video Auto', 'OK', 'Error') | Out-Null
}

if (Test-Live) { Start-Process $Url; exit 0 }

# Ollama: start its server early so the first translation does not wait for it.
# Models load only when used, so an idle server costs little memory.
if (-not (Get-Process -Name 'ollama' -ErrorAction SilentlyContinue)) {
    $Ollama = (Get-Command 'ollama' -ErrorAction SilentlyContinue).Source
    if (-not $Ollama) {
        $Bundled = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
        if (Test-Path $Bundled) { $Ollama = $Bundled }
    }
    if ($Ollama) { Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden | Out-Null }
}

# The supervisor holds a single-instance lock, so a second start exits on its own.
$StartScript = Join-Path $ProjectRoot 'scripts\start.ps1'
Start-Process -FilePath 'powershell.exe' `
    -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$StartScript`"" `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null

if (Test-Path $LoadingPage) { Start-Process $LoadingPage }

$Deadline = (Get-Date).AddSeconds(300)
while (-not (Test-Live) -and (Get-Date) -lt $Deadline) { Start-Sleep -Seconds 2 }
if (-not (Test-Live)) {
    $DataRoot = Join-Path $env:LOCALAPPDATA 'VideoAuto'
    $EnvFile = Join-Path $ProjectRoot '.runtime\videoauto.env'
    if (Test-Path $EnvFile) {
        $Line = Get-Content $EnvFile | Where-Object { $_ -match '^VIDEOAUTO_DATA_ROOT=(.+)$' } | Select-Object -First 1
        if ($Line -match '^VIDEOAUTO_DATA_ROOT=(.+)$') { $DataRoot = $Matches[1] }
    }
    Show-Error "Video Auto chua khoi dong duoc sau 5 phut.`n`nXem nhat ky trong $(Join-Path $DataRoot 'logs') hoac chay scripts\status.ps1."
    exit 1
}
if (-not (Test-Path $LoadingPage)) { Start-Process $Url }
