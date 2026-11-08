; مثبّت PyDM: يُثبَّت للمستخدم الحالي فقط، بلا صلاحيات مدير.
; التثبيت الصامت:  PyDM-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif

[Setup]
AppId={{C31936CE-8910-54B5-A8E8-B8C46C270927}
AppName=PyDM
AppVersion={#AppVersion}
AppPublisher=Mustapha Djoghlal
DefaultDirName={localappdata}\Programs\PyDM
DefaultGroupName=PyDM
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=PyDM-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\PyDM.exe
Compression=lzma2/max
SolidCompression=yes
CloseApplications=force
RestartApplications=no
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"
Name: "startup"; Description: "تشغيل PyDM مع بدء الويندوز (لالتقاط التحميلات من المتصفح)"

[Files]
Source: "..\dist\PyDM\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\tools\yt-dlp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\tools\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\tools\deno.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\extension\*"; DestDir: "{app}\extension"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PyDM"; Filename: "{app}\PyDM.exe"
Name: "{autoprograms}\مجلد إضافة PyDM للمتصفح"; Filename: "{app}\extension"
Name: "{autodesktop}\PyDM"; Filename: "{app}\PyDM.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "PyDM"; ValueData: """{app}\PyDM.exe"""; Flags: uninsdeletevalue; Tasks: startup

[Run]
; يعمل أيضاً في التثبيت الصامت
Filename: "{app}\PyDM.exe"; Flags: nowait runasoriginaluser skipifdoesntexist

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM PyDM.exe"; Flags: runhidden; RunOnceId: "KillPyDM"
