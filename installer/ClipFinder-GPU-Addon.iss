; Optional NVIDIA support installer. Compile through Build-Installer.ps1 -GpuAddon.
; This is intentionally separate from the application installer: CUDA and cuDNN
; are large, require their own administrator prompts, and are not needed for CPU mode.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef CudaInstallerPath
  #define CudaInstallerPath "..\..\cuda_12.9.2_576.57_windows.exe"
#endif
#ifndef CudnnInstallerPath
  #define CudnnInstallerPath "..\..\cudnn_9.24.0_windows_x86_64.exe"
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
; CUDA and cuDNN write to Program Files. Request elevation once at the start
; instead of letting either NVIDIA installer fail late with access denied.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\installer-output
OutputBaseFilename={#MyInstallerName}
; NVIDIA's installers are already compressed; fast compression avoids spending
; tens of minutes trying to shrink multi-gigabyte binary payloads further.
Compression=lzma2/fast
SolidCompression=yes
DiskSpanning=yes
DiskSliceSize=2000000000
WizardStyle=modern
SetupIconFile=..\assets\clipfinder.ico
InfoBeforeFile=GPU-ADDON-README.txt

[Files]
Source: "{#CudaInstallerPath}"; DestDir: "{tmp}\ClipFinder-GPU"; DestName: "cuda_12.9.2_576.57_windows.exe"; Flags: deleteafterinstall
Source: "{#CudnnInstallerPath}"; DestDir: "{tmp}\ClipFinder-GPU"; DestName: "cudnn_9.24.0_windows_x86_64.exe"; Flags: deleteafterinstall
Source: "Configure-ClipFinder.ps1"; DestDir: "{tmp}\ClipFinder-GPU"; Flags: deleteafterinstall
Source: "runtime-compatibility.json"; DestDir: "{tmp}\ClipFinder-GPU"; Flags: deleteafterinstall
Source: "GPU-ADDON-README.txt"; DestDir: "{tmp}\ClipFinder-GPU"; Flags: deleteafterinstall

[Run]
; Do not use the postinstall flag here. The two installers must run while their
; extracted temporary files are still present.
Filename: "{tmp}\ClipFinder-GPU\cuda_12.9.2_576.57_windows.exe"; Description: "Install NVIDIA CUDA 12.9"; Flags: waituntilterminated; Check: NeedCudaInstall
Filename: "{tmp}\ClipFinder-GPU\cudnn_9.24.0_windows_x86_64.exe"; Description: "Install NVIDIA cuDNN 9.24"; Flags: waituntilterminated; Check: NeedCudnnInstall
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\ClipFinder-GPU\Configure-ClipFinder.ps1"" -CompatibilityPath ""{tmp}\ClipFinder-GPU\runtime-compatibility.json"" -ResultPath ""{tmp}\ClipFinder-GPU\gpu-result.txt"" -RequireGpu"; Description: "Verify NVIDIA GPU support for ClipFinder"; Flags: waituntilterminated runascurrentuser; AfterInstall: VerifyGpuConfiguration

[Code]
var
  MissingInstallerNoticeShown: Boolean;

function CudaFilesPresent(Directory: String): Boolean;
begin
  Result :=
    FileExists(AddBackslash(Directory) + 'cudart64_12.dll') and
    FileExists(AddBackslash(Directory) + 'cublasLt64_12.dll') and
    FileExists(AddBackslash(Directory) + 'cublas64_12.dll');
end;

function CudnnFilesPresent(Directory: String): Boolean;
begin
  Result :=
    FileExists(AddBackslash(Directory) + 'cudnn_adv64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_cnn64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_engines_precompiled64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_engines_runtime_compiled64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_engines_tensor_ir64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_ext64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_graph64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_heuristic64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn_ops64_9.dll') and
    FileExists(AddBackslash(Directory) + 'cudnn64_9.dll');
end;

function HasSupportedCuda12(): Boolean;
var
  MinorVersion: Integer;
begin
  ; Current CTranslate2 wheels link against CUDA 12. CUDA 12.1 is too old for
  ; the cuDNN 9 package used by this add-on; accept an existing CUDA 12.3+
  ; toolkit and let the configuration step verify the matching cuDNN folder.
  Result := False;
  for MinorVersion := 3 to 9 do begin
    if CudaFilesPresent(ExpandConstant('{autopf}\NVIDIA GPU Computing Toolkit\CUDA\v12.' + IntToStr(MinorVersion) + '\bin')) then begin
      Result := True;
      exit;
    end;
  end;
end;

function DirectoryContainsMatchingCudnn(Directory, VersionPart: String): Boolean;
var
  FindRec: TFindRec;
  Candidate: String;
begin
  Result := False;
  if not DirExists(Directory) then exit;
  if CudnnFilesPresent(Directory) and
     (Pos(Lowercase('\bin\' + VersionPart + '\'), Lowercase(Directory + '\')) > 0) then begin
    Result := True;
    exit;
  end;
  if not FindFirst(AddBackslash(Directory) + '*', FindRec) then exit;
  try
    repeat
      if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
         (FindRec.Name <> '.') and (FindRec.Name <> '..') then begin
        Candidate := AddBackslash(Directory) + FindRec.Name;
        if DirectoryContainsMatchingCudnn(Candidate, VersionPart) then begin
          Result := True;
          exit;
        end;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function HasMatchingCudnn9(MinorVersion: Integer): Boolean;
var
  VersionPart: String;
begin
  VersionPart := '12.' + IntToStr(MinorVersion);
  Result := CudnnFilesPresent(ExpandConstant('{autopf}\NVIDIA GPU Computing Toolkit\CUDA\v' + VersionPart + '\bin')) or
    DirectoryContainsMatchingCudnn(ExpandConstant('{autopf}\NVIDIA\CUDNN'), VersionPart);
end;

function HasCompatibleRuntimePair(): Boolean;
var
  MinorVersion: Integer;
begin
  Result := False;
  for MinorVersion := 3 to 9 do begin
    if CudaFilesPresent(ExpandConstant('{autopf}\NVIDIA GPU Computing Toolkit\CUDA\v12.' + IntToStr(MinorVersion) + '\bin')) and
       HasMatchingCudnn9(MinorVersion) then begin
      Result := True;
      exit;
    end;
  end;
end;

function HasBundledCuda12_9(): Boolean;
begin
  Result := CudaFilesPresent(ExpandConstant('{autopf}\NVIDIA GPU Computing Toolkit\CUDA\v12.9\bin'));
end;

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

function NeedCudaInstall(): Boolean;
begin
  { A different supported CUDA minor without matching cuDNN cannot use the
    bundled cuDNN 12.9 package. Install the bundled CUDA as well so setup
    always creates a same-minor pair. }
  Result := (not HasCompatibleRuntimePair()) and (not HasBundledCuda12_9()) and
    CanRunGpuInstaller('cuda_12.9.2_576.57_windows.exe');
end;

function NeedCudnnInstall(): Boolean;
begin
  { This check runs after the CUDA installer entry, so a freshly installed
    CUDA 12.9 folder can already be matched against standalone cuDNN. }
  Result := (not HasCompatibleRuntimePair()) and CanRunGpuInstaller('cudnn_9.24.0_windows_x86_64.exe');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if HasCompatibleRuntimePair() then begin
    MsgBox(
      'A compatible CUDA 12.x and cuDNN 9 installation was detected. The GPU add-on will verify that their versions match before enabling ClipFinder GPU mode.',
      mbInformation, MB_OK);
  end
  else if HasSupportedCuda12() then begin
    MsgBox(
      'A CUDA 12.x installation was detected, but it has no matching complete cuDNN 9 runtime. The add-on will install its tested CUDA 12.9 and cuDNN 9.24 pair, then verify ClipFinder GPU mode.',
      mbInformation, MB_OK);
  end;
end;

procedure VerifyGpuConfiguration;
var
  ResultText: String;
begin
  if (not LoadStringFromFile(ExpandConstant('{tmp}\ClipFinder-GPU\gpu-result.txt'), ResultText)) or
     (Lowercase(Trim(ResultText)) <> 'cuda') then begin
    RaiseException(
      'ClipFinder could not verify GPU transcription after installing the NVIDIA components. ' +
      'ClipFinder remains usable in CPU mode. Open the setup-status report or create a diagnostic report in the app before retrying the add-on.');
  end;
end;
