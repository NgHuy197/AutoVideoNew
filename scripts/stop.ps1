$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvFile = Join-Path (Join-Path $ProjectRoot '.runtime') 'videoauto.env'
if (Test-Path $EnvFile) { Get-Content $EnvFile | Where-Object { $_ -match '^(VIDEOAUTO_[^=]+)=(.*)$' } | ForEach-Object { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] } }
$PidRoot = if ($env:VIDEOAUTO_DATA_ROOT) { $env:VIDEOAUTO_DATA_ROOT } else { Join-Path $env:LOCALAPPDATA 'VideoAuto' }
$PidFile = Join-Path $PidRoot 'supervisor.pid.json'
$processes = @()
if (Test-Path $PidFile) {
    try {
        $record = Get-Content -Raw -LiteralPath $PidFile | ConvertFrom-Json
        $candidate = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
        if ($candidate) {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.Id)").CommandLine
            $sameStart = $true
            if ($null -ne $record.create_time) {
                try { $sameStart = [Math]::Abs(([DateTimeOffset]$candidate.StartTime).ToUnixTimeSeconds() - [double]$record.create_time) -le 1.0 } catch { $sameStart = $false }
            }
            if ($sameStart -and $cmd -and $cmd -match 'backend\.supervisor' -and $cmd -like "*$ProjectRoot*") { $processes += $candidate }
        }
    } catch { }
}
if (!$processes) {
    $processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -match 'backend\.supervisor' -and $_.CommandLine -like "*$ProjectRoot*" }
}
foreach ($process in $processes) { & taskkill.exe /PID $process.Id /T /F | Out-Null }
if ($processes) { Write-Host "Stopped Video Auto supervisor, API, and owned worker trees." }
else { Write-Host "Video Auto supervisor is not running." }
