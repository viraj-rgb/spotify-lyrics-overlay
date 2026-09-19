; Inno Setup script for Spotify Lyrics Overlay.
;
; Produces a single Setup .exe: the user downloads one file, double-clicks it,
; and the app is installed with Desktop and Start Menu shortcuts. That is the
; "one click" path a zip cannot give, since a zip still requires the user to
; extract it and then find the right executable inside.
;
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
; Output: installer_output\SpotifyLyricsOverlay-Setup.exe

#define AppName        "Spotify Lyrics Overlay"
#define AppShortName   "SpotifyLyricsOverlay"
#define AppVersion     "1.1.0"
#define AppPublisher   "viraj-rgb"
#define AppURL         "https://github.com/viraj-rgb/spotify-lyrics-overlay"
#define AppExeName     "SpotifyLyricsOverlay.exe"

[Setup]
; Stable GUID. Never change it -- it is how Windows recognises an existing
; install and upgrades it in place rather than stacking a second copy.
AppId={{8F3C2A41-6D9E-4B27-9C55-1E7A0D4B8F62}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} installer

; Install per-user under LocalAppData so no administrator prompt appears.
; Requiring admin would put a UAC wall in front of the one-click promise, and
; nothing here needs machine-wide access.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#AppShortName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; The app has no options to choose at install time, so the directory page is
; just friction. Advanced users can still pass /DIR= on the command line.
DisableDirPage=yes
DisableReadyPage=no

OutputDir=installer_output
OutputBaseFilename={#AppShortName}-Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}

; The payload is a bundled Chromium runtime, so it compresses well but slowly.
; Worth it: this is downloaded far more often than it is built.
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4

WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start {#AppName} when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole PyInstaller one-directory build. recursesubdirs picks up _internal,
; which holds the Qt runtime, the web assets and QtWebEngineProcess.exe.
Source: "dist\SpotifyLyricsOverlay\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller writes nothing back into {app}, but Qt may leave a cache there.
; The user's own settings live in %USERPROFILE%\.lyric-overlay and are
; deliberately left alone, so reinstalling keeps their position and preferences.
Type: filesandordirs; Name: "{app}\_internal\QtWebEngine"

[Code]
{ Refuse to install over a copy that is currently running -- the files would be
  locked and the upgrade would half-apply. }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if Exec('cmd.exe', '/C tasklist /FI "IMAGENAME eq {#AppExeName}" | find /I "{#AppExeName}"',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
    begin
      if MsgBox('{#AppName} is currently running.' + #13#10#13#10 +
                'Quit it from the tray icon, then click OK to continue.',
                mbConfirmation, MB_OKCANCEL) = IDOK then
        Exec('taskkill.exe', '/F /IM "{#AppExeName}"', '', SW_HIDE,
             ewWaitUntilTerminated, ResultCode)
      else
        Result := False;
    end;
  end;
end;
