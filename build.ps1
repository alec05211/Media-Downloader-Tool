[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSCommandPath
Set-Location $projectRoot

# The built executable contains Python and all of these packages; end users do
# not need to install dependencies or have internet access to launch it.
py -m pip install --upgrade -r requirements.txt pyinstaller
$userSite = py -c "import site; print(site.getusersitepackages())"
if ($env:PYTHONPATH) {
  $env:PYTHONPATH = "$userSite;$env:PYTHONPATH"
} else {
  $env:PYTHONPATH = $userSite
}
py -m PyInstaller --noconfirm --clean --onefile --noconsole --name application `
  --collect-all imageio_ffmpeg `
  --collect-all yt_dlp `
  app.py
Copy-Item -LiteralPath "$projectRoot\install.ps1" -Destination "$projectRoot\dist\install.ps1" -Force

Write-Host "Built: $projectRoot\dist\application.exe"
