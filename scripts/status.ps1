$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$EnvFile = Join-Path $ProjectRoot '.runtime\videoauto.env'
if (Test-Path $EnvFile) { Get-Content $EnvFile | Where-Object { $_ -match '^(VIDEOAUTO_[^=]+)=(.*)$' } | ForEach-Object { Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2] } }
$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match 'backend\.supervisor' -and $_.CommandLine -like "*$ProjectRoot*" })
$workers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match 'backend\.worker' -and $_.CommandLine -like "*$ProjectRoot*" })
$ready = $null
try { $ready = Invoke-RestMethod 'http://127.0.0.1:8765/api/v1/health/ready' -ErrorAction Stop } catch { }
$supervisorIds = @($processes | ForEach-Object { [int]$_.ProcessId })
$workerIds = @($workers | ForEach-Object { [int]$_.ProcessId })
[pscustomobject]@{ ready = [bool]$ready; supervisor_pids = $supervisorIds; worker_pids = $workerIds; api = $ready } | ConvertTo-Json -Depth 8
if (!$ready -and !$processes) { exit 1 }
