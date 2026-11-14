[CmdletBinding()]
param(
  [switch]$NoDesktopShortcut
)

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'application.exe'
if (-not (Test-Path -LiteralPath $source)) {
  $source = Join-Path $PSScriptRoot 'Media Downloader.exe'
}
if (-not (Test-Path -LiteralPath $source)) {
  $source = Join-Path (Split-Path -Parent $PSScriptRoot) 'application.exe'
}
if (-not (Test-Path -LiteralPath $source)) {
  $source = Join-Path (Split-Path -Parent $PSScriptRoot) 'Media Downloader.exe'
}
if (-not (Test-Path -LiteralPath $source)) {
  throw 'application.exe must be present in the project directory.'
}

$nativeHostSource = Join-Path $PSScriptRoot 'media_downloader_native_host.exe'
if (-not (Test-Path -LiteralPath $nativeHostSource)) {
  $nativeHostSource = Join-Path $PSScriptRoot 'dist\media_downloader_native_host.exe'
}
$nativeManifestSource = Join-Path $PSScriptRoot 'native-messaging-host.json'
if (-not (Test-Path -LiteralPath $nativeManifestSource)) {
  $nativeManifestSource = Join-Path (Split-Path -Parent $PSScriptRoot) 'native-messaging-host.json'
}
if (-not (Test-Path -LiteralPath $nativeHostSource) -or -not (Test-Path -LiteralPath $nativeManifestSource)) {
  throw 'The Chrome native-messaging host files must be built before installation.'
}

$appName = 'Media Downloader'
$installDir = Join-Path $env:LOCALAPPDATA $appName
$installedExe = Join-Path $installDir 'application.exe'
$installedNativeHost = Join-Path $installDir 'media_downloader_native_host.exe'
$installedNativeManifest = Join-Path $installDir 'native-messaging-host.json'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $installedExe -Force
Copy-Item -LiteralPath $nativeHostSource -Destination $installedNativeHost -Force
Copy-Item -LiteralPath $nativeManifestSource -Destination $installedNativeManifest -Force
$nativeManifest = Get-Content -Raw -LiteralPath $installedNativeManifest | ConvertFrom-Json
$nativeManifest.path = $installedNativeHost
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
  $installedNativeManifest,
  ($nativeManifest | ConvertTo-Json -Depth 5),
  $utf8NoBom
)

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

$protocolKey = 'HKCU:\Software\Classes\media-downloader'
New-Item -Path $protocolKey -Force | Out-Null
Set-Item -Path $protocolKey -Value 'URL:Media Downloader Protocol'
New-ItemProperty -Path $protocolKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$protocolKey\DefaultIcon" -Force | Out-Null
Set-Item -Path "$protocolKey\DefaultIcon" -Value "`"$installedExe`",0"
New-Item -Path "$protocolKey\shell\open\command" -Force | Out-Null
Set-Item -Path "$protocolKey\shell\open\command" -Value "`"$installedExe`" `"%1`""

$nativeHostKeys = @(
  'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher',
  'HKCU:\Software\WOW6432Node\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher'
)
foreach ($nativeHostKey in $nativeHostKeys) {
  New-Item -Path $nativeHostKey -Force | Out-Null
  Set-Item -Path $nativeHostKey -Value $installedNativeManifest
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  & reg.exe add 'HKLM\Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher' /ve /t REG_SZ /d $installedNativeManifest /f /reg:64 | Out-Null
  & reg.exe add 'HKLM\Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher' /ve /t REG_SZ /d $installedNativeManifest /f /reg:32 | Out-Null
}

$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $appName -Value "`"$installedExe`" --background" -PropertyType String -Force | Out-Null
Start-Process -FilePath $installedExe -ArgumentList '--background' -WindowStyle Hidden

Write-Host "$appName is installed with a local background launcher on http://127.0.0.1:38471/."
