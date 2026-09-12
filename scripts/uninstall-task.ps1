param([string]$TaskName = 'VideoAuto Local Service')
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed scheduled task '$TaskName'. Model and output files were retained."
