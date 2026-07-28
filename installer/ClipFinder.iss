; ClipFinder Windows beta installer. Compile through Build-Installer.ps1.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
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
CloseApplications=yes
RestartApplications=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\installer-output
OutputBaseFilename=ClipFinder-Setup-{#MyAppVersion}{#MyInstallerSuffix}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\clipfinder.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\ClipFinder\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Configure-ClipFinder.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\TESTER-INSTALLATION.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ClipFinder"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\ClipFinder\Configure ClipFinder runtime"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-ClipFinder.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\ClipFinder\Tester installation guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\TESTER-INSTALLATION.md"""; WorkingDir: "{app}"
Name: "{autodesktop}\ClipFinder"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-ClipFinder.ps1"""; Description: "Install missing Windows components and configure ClipFinder"; Flags: postinstall waituntilterminated runascurrentuser
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ClipFinder"; Flags: nowait postinstall skipifsilent
