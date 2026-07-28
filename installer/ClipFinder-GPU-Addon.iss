; Optional NVIDIA support installer. Compile through Build-Installer.ps1 -GpuAddon.
; This is intentionally separate from the application installer: CUDA and cuDNN
; are large, require their own administrator prompts, and are not needed for CPU mode.

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

#define MyAppName "ClipFinder NVIDIA GPU Add-on"
#define MyInstallerName "ClipFinder-GPU-Addon-" + MyAppVersion

[Setup]
AppId={{3E133261-2A3E-4455-B0EF-8C7A408E6A5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=ClipFinder
DefaultDirName={localappdata}\ClipFinder\GPU-Addon
DisableProgramGroupPage=yes
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\installer-output
OutputBaseFilename={#MyInstallerName}
Compression=lzma2/ultra64
SolidCompression=yes
DiskSpanning=yes
DiskSliceSize=2000000000
WizardStyle=modern
SetupIconFile=..\assets\clipfinder.ico
InfoBeforeFile=GPU-ADDON-README.txt

[Files]
Source: "..\..\cuda_12.9.2_576.57_windows.exe"; DestDir: "{tmp}\ClipFinder-GPU"; DestName: "cuda_12.9.2_576.57_windows.exe"; Flags: deleteafterinstall
Source: "..\..\cudnn_9.24.0_windows_x86_64.exe"; DestDir: "{tmp}\ClipFinder-GPU"; DestName: "cudnn_9.24.0_windows_x86_64.exe"; Flags: deleteafterinstall
Source: "Configure-ClipFinder.ps1"; DestDir: "{tmp}\ClipFinder-GPU"; Flags: deleteafterinstall
Source: "GPU-ADDON-README.txt"; DestDir: "{tmp}\ClipFinder-GPU"; Flags: deleteafterinstall

[Run]
; Do not use the postinstall flag here. The two installers must run while their
; extracted temporary files are still present.
Filename: "{tmp}\ClipFinder-GPU\cuda_12.9.2_576.57_windows.exe"; Description: "Install NVIDIA CUDA 12.9"; Flags: waituntilterminated; Check: CanRunGpuInstaller('cuda_12.9.2_576.57_windows.exe')
Filename: "{tmp}\ClipFinder-GPU\cudnn_9.24.0_windows_x86_64.exe"; Description: "Install NVIDIA cuDNN 9.24"; Flags: waituntilterminated; Check: CanRunGpuInstaller('cudnn_9.24.0_windows_x86_64.exe')
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\ClipFinder-GPU\Configure-ClipFinder.ps1"""; Description: "Verify NVIDIA GPU support for ClipFinder"; Flags: waituntilterminated runascurrentuser

[Code]
var
  MissingInstallerNoticeShown: Boolean;

function CanRunGpuInstaller(FileName: String): Boolean;
var
  FilePath: String;
begin
  FilePath := ExpandConstant('{tmp}\ClipFinder-GPU\') + FileName;
  Result := FileExists(FilePath);
  if (not Result) and (not MissingInstallerNoticeShown) then begin
    MissingInstallerNoticeShown := True;
    MsgBox(
      'The GPU add-on files could not be extracted. ClipFinder itself will still work in CPU mode. ' +
      'Close this setup and download the complete GPU add-on ZIP again, keeping every .bin file next to the setup EXE.',
      mbError, MB_OK);
  end;
end;
