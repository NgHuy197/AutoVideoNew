$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Url = 'http://127.0.0.1:8765/'
$Health = 'http://127.0.0.1:8765/api/v1/health/ready'

function Test-Ready {
    try { $null = Invoke-RestMethod $Health -TimeoutSec 3 -ErrorAction Stop; return $true } catch { return $false }
}

if (-not (Test-Ready)) {
    # Start the supervisor hidden in the background; it keeps running after this window closes.
    # The path contains spaces, so it must be quoted inside the argument string.
    $StartScript = Join-Path $ProjectRoot 'scripts\start.ps1'
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`"" `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
    $deadline = (Get-Date).AddSeconds(300)
    while (-not (Test-Ready) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    if (-not (Test-Ready)) {
        Write-Host 'Video Auto chua san sang sau 300 giay. Kiem tra bang scripts\status.ps1.'
        Read-Host 'Nhan Enter de dong'
        exit 1
    }
}

Start-Process $Url
