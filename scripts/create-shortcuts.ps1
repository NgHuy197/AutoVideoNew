# Create "Video Auto" shortcuts on the Desktop and in the Start menu.
# Double-clicking one starts Ollama and the local service hidden, then opens the app.
param([switch]$NoDesktop)
$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Icon = Join-Path $ProjectRoot 'scripts\launcher\videoauto.ico'
$Shell = New-Object -ComObject WScript.Shell

function New-Shortcut([string]$Path, [string]$Script, [string]$Description, [string]$ScriptArgs = '') {
    $Link = $Shell.CreateShortcut($Path)
    $Link.TargetPath = $PowerShell
    $Link.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $ProjectRoot $Script)`" $ScriptArgs".TrimEnd()
    $Link.WorkingDirectory = $ProjectRoot
    $Link.IconLocation = "$Icon,0"
    $Link.Description = $Description
    $Link.WindowStyle = 7  # minimized, so the hidden console never flashes on screen
    $Link.Save()
    Write-Host "Created $Path"
}

$StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Video Auto'
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
New-Shortcut (Join-Path $StartMenu 'Video Auto.lnk') 'scripts\open-app.ps1' 'Mo Video Auto'
New-Shortcut (Join-Path $StartMenu 'Dung Video Auto.lnk') 'scripts\stop.ps1' 'Tat Video Auto va Ollama' '-All'
if (-not $NoDesktop) {
    $Desktop = [Environment]::GetFolderPath('Desktop')
    New-Shortcut (Join-Path $Desktop 'Video Auto.lnk') 'scripts\open-app.ps1' 'Mo Video Auto'
    New-Shortcut (Join-Path $Desktop 'Dung Video Auto.lnk') 'scripts\stop.ps1' 'Tat Video Auto va Ollama' '-All'
}
