[CmdletBinding()]
param(
  [switch]$NoDesktopShortcut
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'application.exe'
if (-not (Test-Path -LiteralPath $source)) {
  throw 'application.exe must be in the same folder as install.ps1.'
}

$appName = 'Media Downloader'
$installDir = Join-Path $env:LOCALAPPDATA $appName
$installedExe = Join-Path $installDir 'application.exe'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $installedExe -Force

$shell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$appName.lnk"
$startMenuShortcut = $shell.CreateShortcut($startMenu)
$startMenuShortcut.TargetPath = $installedExe
$startMenuShortcut.WorkingDirectory = $installDir
$startMenuShortcut.Save()

if (-not $NoDesktopShortcut) {
  $desktop = [Environment]::GetFolderPath('Desktop')
  $desktopShortcut = $shell.CreateShortcut((Join-Path $desktop "$appName.lnk"))
  $desktopShortcut.TargetPath = $installedExe
  $desktopShortcut.WorkingDirectory = $installDir
  $desktopShortcut.Save()
}

Write-Host "$appName is installed. Launch it from the Start menu or desktop shortcut."
