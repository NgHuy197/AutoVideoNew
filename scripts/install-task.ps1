param([string]$TaskName = 'VideoAuto Local Service')
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$StartScript = Join-Path $ProjectRoot 'scripts\start.ps1'
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$StartScript`"" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtStartup
$RecoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ([string]::IsNullOrWhiteSpace($Identity) -or $Identity -notmatch '\\') { throw "Unable to resolve a qualified Windows account identity." }
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($Trigger, $RecoveryTrigger) -Settings $Settings -Principal $Principal -Force | Out-Null
Write-Host "Registered '$TaskName'."
