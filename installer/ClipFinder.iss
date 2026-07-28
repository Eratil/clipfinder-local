; ClipFinder Windows beta installer. Compile through Build-Installer.ps1.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef MyIncludeGpuDependencies
  #define MyIncludeGpuDependencies 0
#endif

#define MyAppName "ClipFinder"
#define MyAppPublisher "ClipFinder"
#define MyAppExeName "ClipFinder.exe"
#if MyIncludeGpuDependencies
  #define MyInstallerSuffix "-GPU"
#else
  #define MyInstallerSuffix ""
#endif

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
#if MyIncludeGpuDependencies
Source: "..\..\cuda_12.9.2_576.57_windows.exe"; DestDir: "{tmp}\ClipFinder-GPU"; DestName: "cuda_12.9.2_576.57_windows.exe"; Flags: deleteafterinstall
Source: "..\..\cudnn_9.24.0_windows_x86_64.exe"; DestDir: "{tmp}\ClipFinder-GPU"; DestName: "cudnn_9.24.0_windows_x86_64.exe"; Flags: deleteafterinstall
#endif

[Icons]
Name: "{autoprograms}\ClipFinder"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\ClipFinder\Configure ClipFinder runtime"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-ClipFinder.ps1"""; WorkingDir: "{app}"
Name: "{autoprograms}\ClipFinder\Tester installation guide"; Filename: "{sys}\notepad.exe"; Parameters: """{app}\TESTER-INSTALLATION.md"""; WorkingDir: "{app}"
Name: "{autodesktop}\ClipFinder"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
#if MyIncludeGpuDependencies
Name: "gpu"; Description: "Install NVIDIA GPU support - CUDA 12.9 and cuDNN 9 (requires administrator approval)"; GroupDescription: "Optional GPU support:"; Flags: unchecked
#endif

[Run]
#if MyIncludeGpuDependencies
Filename: "{tmp}\ClipFinder-GPU\cuda_12.9.2_576.57_windows.exe"; Description: "Install NVIDIA CUDA 12.9"; Flags: postinstall waituntilterminated; Tasks: gpu
Filename: "{tmp}\ClipFinder-GPU\cudnn_9.24.0_windows_x86_64.exe"; Description: "Install NVIDIA cuDNN 9.24"; Flags: postinstall waituntilterminated; Tasks: gpu
#endif
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Configure-ClipFinder.ps1"""; Description: "Install missing Windows components and configure ClipFinder"; Flags: postinstall waituntilterminated runascurrentuser
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ClipFinder"; Flags: nowait postinstall skipifsilent
