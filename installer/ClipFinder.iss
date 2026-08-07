; ClipFinder Windows beta installer. Compile through Build-Installer.ps1.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef RuntimeContract
  #define RuntimeContract "missing-build-contract"
#endif

#define MyAppName "ClipFinder"
#define MyAppPublisher "ClipFinder"
#define MyAppExeName "ClipFinder.exe"
#define MyInstallerSuffix ""

[Setup]
AppId={{A91CCB62-8BDE-45D3-A5B7-1F4E9EB00E85}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ClipFinder
DefaultGroupName=ClipFinder
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
; A normal Restart Manager request did not reliably close the embedded local
; server. Force only applications locking files that this installer replaces.
CloseApplications=force
RestartApplications=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\installer-output
OutputBaseFilename=ClipFinder-Setup-{#MyAppVersion}{#MyInstallerSuffix}
; AI libraries are already compressed. A fast setting keeps release builds
; practical without changing what the tester installs.
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\clipfinder.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\ClipFinder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\ClipFinder\Configure-ClipFinder.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\ClipFinder\TESTER-INSTALLATION.md"; DestDir: "{app}"; Flags: ignoreversion
; PyTorch's native CPU libraries need the MSVC runtime. Keep the official x64
; installer in the base setup so a tester does not have to have winget.
Source: "third_party\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\ClipFinder"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\ClipFinder\Configure ClipFinder runtime"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-ClipFinder.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\ClipFinder\Tester installation guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\TESTER-INSTALLATION.md"""; WorkingDir: "{app}"
Name: "{autodesktop}\ClipFinder"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; Description: "Install Microsoft Visual C++ Runtime (required by ClipFinder AI libraries)"; Flags: waituntilterminated; Check: VCRedistMissing
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-ClipFinder.ps1"" -PreserveDeviceChoice"; Description: "Install missing Windows components and configure ClipFinder"; Flags: postinstall waituntilterminated runascurrentuser; Check: RuntimeConfigurationNeedsRepair
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ClipFinder"; Flags: nowait; Check: ShouldLaunchClipFinder

[Code]
function VCRedistMissing(): Boolean;
var
  Installed: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64', 'Installed', Installed) then
    Result := Installed <> 1;
end;

function ShouldLaunchClipFinder(): Boolean;
begin
  { The update helper launches the application itself after a silent update.
    A normal, interactive installer always opens ClipFinder when it finishes. }
  Result := not WizardSilent;
end;

function RuntimeConfigurationNeedsRepair(): Boolean;
var
  RuntimeText: AnsiString;
  RuntimePath: String;
begin
  { Updating application files must not reset a user's chosen CPU/GPU mode,
    model or verified CUDA paths. The Start-menu repair action remains
    available when the user intentionally wants to detect the runtime again. }
  RuntimePath := ExpandConstant('{localappdata}\ClipFinder\runtime.json');
  if not FileExists(RuntimePath) then begin
    Result := True;
    exit;
  end;
  LoadStringFromFile(RuntimePath, RuntimeText);
  Result := False;
  if not Result then begin
    Result :=
      (Pos('"runtime_schema"', RuntimeText) = 0) or
      (Pos('"gpu_runtime_contract"', RuntimeText) = 0) or
      (Pos('{#RuntimeContract}', RuntimeText) = 0) or
      (Pos('"whisper_device"', RuntimeText) = 0);
  end;
end;
