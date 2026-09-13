; Build with Inno Setup 6 after running build.ps1.
; This creates an optional desktop shortcut and a Start-menu entry.
#define AppName "Media Downloader"
#define AppVersion "1.0.0"
#define AppExeName "application.exe"

[Setup]
AppId={{E4E65D0F-EE58-4856-BA37-FE6612A95C5C}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=release
OutputBaseFilename=MediaDownloaderSetup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
