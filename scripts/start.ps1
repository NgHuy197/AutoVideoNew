$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvFile = Join-Path $ProjectRoot '.runtime\videoauto.env'
if (Test-Path $EnvFile) { Get-Content $EnvFile | Where-Object { $_ -match '^(VIDEOAUTO_[^=]+)=(.*)$' } | ForEach-Object { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] } }
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'; if (!(Test-Path $Python)) { $Python = 'python' }
Set-Location $ProjectRoot
& $Python -X utf8 -m backend.supervisor
exit $LASTEXITCODE
