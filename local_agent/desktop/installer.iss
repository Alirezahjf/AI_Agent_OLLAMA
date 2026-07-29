; ============================================================================
;  Persian Local Assistant — Inno Setup script
;
;  Build the executable first, then compile this script:
;
;      python local_agent_setup.py build-desktop
;      iscc local_agent\desktop\installer.iss
;
;  Or do both in one go:
;
;      python local_agent_setup.py build-desktop --installer
;
;  Output: dist\installer\PersianLocalAssistant-Setup-<version>.exe
; ============================================================================

#define AppName "Persian Local Assistant"
#define AppNameFa "دستیار محلی ویندوز"
#define AppVersion "2.0.0"
#define AppPublisher "Alirezahjf"
#define AppURL "https://github.com/Alirezahjf/AI_Agent_OLLAMA"
#define ExeName "PersianLocalAssistant.exe"
#define SourceExe "..\..\dist\" + ExeName

[Setup]
AppId={{8E2C4A17-6B93-4F5E-9A21-3D7C5B8F1E42}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}

; Per-user install: no administrator rights required.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

OutputDir=..\..\dist\installer
OutputBaseFilename=PersianLocalAssistant-Setup-{#AppVersion}
SetupIconFile=..\..\build\desktop\icon.ico
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
; Persian is not bundled with Inno Setup; add Persian.isl to the Languages
; folder to enable this line:
; Name: "persian"; MessagesFile: "compiler:Languages\Persian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startupicon"; Description: "Start {#AppName} when Windows starts"; GroupDescription: "Startup"; Flags: unchecked
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion isreadme
Source: "..\README.md"; DestDir: "{app}\docs"; DestName: "LocalAgent.md"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\DESKTOP.md"; DestDir: "{app}\docs"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\WEB_UI.md"; DestDir: "{app}\docs"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: quicklaunchicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#ExeName}"; Parameters: "--hidden"; Tasks: startupicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
Filename: "{#AppURL}"; Description: "Open the project page"; Flags: nowait postinstall skipifsilent shellexec unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\docs"
Type: dirifempty; Name: "{app}"

[Registry]
; Auto-start is normally managed inside the app (Settings → auto-start);
; this entry only exists when the user ticks the startup task above and is
; removed on uninstall so no orphan Run key is left behind.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "PersianLocalAssistant"; \
    ValueData: """{app}\{#ExeName}"" --hidden"; \
    Tasks: startupicon; Flags: uninsdeletevalue

[Code]
// The app uses the Edge WebView2 runtime. It ships with Windows 11 and with
// current Windows 10 builds; warn (do not block) when it is missing.
function WebView2Installed(): Boolean;
var
  Value: String;
begin
  Result :=
    RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value) or
    RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Value);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Installed() then
  begin
    if MsgBox(
      'Microsoft Edge WebView2 Runtime was not detected.' + #13#10 +
      'The application needs it to display its interface.' + #13#10#13#10 +
      'Continue anyway? (You can install WebView2 later from' + #13#10 +
      'https://developer.microsoft.com/microsoft-edge/webview2/)',
      mbConfirmation, MB_YESNO) = IDNO then
      Result := False;
  end;
end;
