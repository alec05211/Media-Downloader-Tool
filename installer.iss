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
Source: "dist\media_downloader_native_host.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "native-messaging-host.json"; DestDir: "{app}"; Flags: ignoreversion; AfterInstall: UpdateNativeMessagingManifest

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCR; Subkey: "media-downloader"; ValueType: string; ValueName: ""; ValueData: "URL:Media Downloader Protocol"; Flags: uninsdeletekey
Root: HKCR; Subkey: "media-downloader"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCR; Subkey: "media-downloader\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"",0"
Root: HKCR; Subkey: "media-downloader\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\native-messaging-host.json"; Flags: uninsdeletekey 64bit
Root: HKCU; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\native-messaging-host.json"; Flags: uninsdeletekey 32bit
Root: HKLM; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\native-messaging-host.json"; Flags: uninsdeletekey 64bit
Root: HKLM; Subkey: "Software\Google\Chrome\NativeMessagingHosts\com.media_downloader.launcher"; ValueType: string; ValueName: ""; ValueData: "{app}\native-messaging-host.json"; Flags: uninsdeletekey 32bit
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExeName}"" --background"; Flags: uninsdeletevalue

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "--background"; Flags: nowait runhidden
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure UpdateNativeMessagingManifest;
var
  HostPath: string;
  ManifestJson: string;
begin
  HostPath := ExpandConstant('{app}\media_downloader_native_host.exe');
  StringChangeEx(HostPath, '\', '\\', True);
  ManifestJson :=
    '{' + #13#10 +
    '  "name": "com.media_downloader.launcher",' + #13#10 +
    '  "description": "Launch the local Media Downloader application",' + #13#10 +
    '  "path": "' + HostPath + '",' + #13#10 +
    '  "type": "stdio",' + #13#10 +
    '  "allowed_origins": [' + #13#10 +
    '    "chrome-extension://ohajoejdjjakbicandnlljppfpkhimcf/"' + #13#10 +
    '  ]' + #13#10 +
    '}' + #13#10;
  SaveStringToFile(ExpandConstant('{app}\native-messaging-host.json'), ManifestJson, False);
end;
