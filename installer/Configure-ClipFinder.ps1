<#
Post-install bootstrap for the packaged ClipFinder desktop application.
It installs only public Windows runtime components that are missing. CUDA and
cuDNN are intentionally not downloaded automatically: the correct NVIDIA
combination depends on the tester's driver and is optional because CPU mode
keeps the application usable.
#>
[CmdletBinding()]
param(
    [string]$CompatibilityPath = '',
    [string]$ResultPath = '',
    [switch]$RequireGpu,
    [switch]$PreserveDeviceChoice
)

$ErrorActionPreference = 'Continue'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateRoot = Join-Path $env:LOCALAPPDATA 'ClipFinder'
$statusPath = Join-Path $stateRoot 'setup-status.txt'
$runtimePath = Join-Path $stateRoot 'runtime.json'
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
$messages = [System.Collections.Generic.List[string]]::new()

$compatibilityCandidates = @(@(
        $CompatibilityPath,
        (Join-Path $appRoot '_internal\assets\runtime-compatibility.json'),
        (Join-Path $appRoot 'runtime-compatibility.json')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
$compatibilityFile = $compatibilityCandidates | Select-Object -First 1
if (-not $compatibilityFile) {
    throw 'Missing runtime-compatibility.json. Reinstall ClipFinder before configuring its runtime.'
}
try {
    $compatibility = Get-Content -Raw -LiteralPath $compatibilityFile -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
} catch {
    throw "Invalid runtime-compatibility.json: $($_.Exception.Message)"
}
if ($compatibility.schema -ne 1 -or -not $compatibility.contract_id -or
    -not $compatibility.cuda.required_dlls -or -not $compatibility.cudnn.required_dlls) {
    throw 'runtime-compatibility.json has an unsupported or incomplete format.'
}

$previousProfile = $null
if ($PreserveDeviceChoice -and (Test-Path -LiteralPath $runtimePath)) {
    try {
        $previousProfile = Get-Content -Raw -LiteralPath $runtimePath -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch {
        $previousProfile = $null
    }
}

function Write-Setup([string]$Message, [string]$Color = 'Gray') {
    $messages.Add($Message)
    Write-Host $Message -ForegroundColor $Color
}

function Find-ExecutableDirectory([string]$FileName) {
    $command = Get-Command $FileName -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return Split-Path -Parent $command.Source }
    return $null
}

function Find-WingetFfmpegDirectory {
    $packagesRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    if (-not (Test-Path $packagesRoot)) { return $null }
    $package = Get-ChildItem -Path $packagesRoot -Directory -Filter 'Gyan.FFmpeg.Shared_*' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $package) { return $null }
    $ffmpeg = Get-ChildItem -Path $package.FullName -Filter 'ffmpeg.exe' -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($ffmpeg) { return $ffmpeg.DirectoryName }
    return $null
}

function Install-WingetPackage([string]$Id, [string]$Name) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        Write-Setup "[warning] $Name is missing and winget is unavailable. Install it manually, then run Configure ClipFinder runtime from the Start menu." 'Yellow'
        return $false
    }
    Write-Setup "Installing missing component: $Name" 'Cyan'
    & $winget.Source install --id $Id --exact --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        Write-Setup "[warning] Could not install $Name automatically (winget exit code $LASTEXITCODE)." 'Yellow'
        return $false
    }
    return $true
}

function Test-WebView2Runtime {
    $keys = @(
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients',
        'HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients'
    )
    foreach ($key in $keys) {
        if (Test-Path $key) {
            $match = Get-ChildItem $key -ErrorAction SilentlyContinue | Get-ItemProperty -ErrorAction SilentlyContinue |
                Where-Object { $_.name -match 'WebView2' } | Select-Object -First 1
            if ($match) { return $true }
        }
    }
    return $false
}

function Test-VCRedist {
    $runtime = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64' -ErrorAction SilentlyContinue
    return $runtime -and $runtime.Installed -eq 1
}

function Test-RequiredDlls([string]$Directory, $Names) {
    if (-not $Directory -or -not (Test-Path -LiteralPath $Directory -PathType Container)) { return $false }
    foreach ($name in @($Names)) {
        if (-not (Test-Path -LiteralPath (Join-Path $Directory ([string]$name)) -PathType Leaf)) { return $false }
    }
    return $true
}

function Install-VCRedist {
    $installedWithWinget = Install-WingetPackage 'Microsoft.VCRedist.2015+.x64' 'Microsoft Visual C++ Redistributable 2015-2022 (x64)'
    if ($installedWithWinget) {
        return $true
    }
    $installer = Join-Path $env:TEMP 'ClipFinder-vc_redist.x64.exe'
    try {
        Write-Setup 'Downloading the official Microsoft Visual C++ Runtime installer...' 'Cyan'
        Invoke-WebRequest -Uri 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile $installer -UseBasicParsing
        Start-Process -FilePath $installer -ArgumentList '/install', '/quiet', '/norestart' -Verb RunAs -Wait
        return (Test-VCRedist)
    } catch {
        Write-Setup "[warning] Could not install Microsoft Visual C++ Redistributable automatically: $($_.Exception.Message)" 'Yellow'
        return $false
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

function Get-CudaProfile {
    $programFilesRoot = if ($env:ProgramFiles) { $env:ProgramFiles } else { 'C:\Program Files' }
    $cudaRoot = Join-Path $programFilesRoot 'NVIDIA GPU Computing Toolkit\CUDA'
    $cudnnRoot = Join-Path $programFilesRoot 'NVIDIA\CUDNN'
    # CTranslate2 4.5+ currently uses CUDA 12 and cuDNN 9. Match cuDNN to the
    # same CUDA minor version; finding arbitrary DLLs is not enough.
    $cudaCandidates = @()
    if (Test-Path $cudaRoot) {
        $cudaCandidates = Get-ChildItem -Path $cudaRoot -Directory -Filter 'v12.*' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $versionText = $_.Name.TrimStart('v')
                $version = $null
                try { $version = [version]$versionText } catch { }
                $bin = Join-Path $_.FullName 'bin'
                if ($version -and
                    $version.Major -eq [int]$compatibility.cuda.major -and
                    $version.Minor -ge [int]$compatibility.cuda.minimum_minor -and
                    $version.Minor -le [int]$compatibility.cuda.maximum_tested_minor -and
                    (Test-RequiredDlls $bin $compatibility.cuda.required_dlls)) {
                    [pscustomobject]@{ Version = $version; Bin = $bin }
                }
            } | Sort-Object Version -Descending
    }

    $cudnnCandidates = @()
    foreach ($candidate in $cudaCandidates) {
        if (Test-RequiredDlls $candidate.Bin $compatibility.cudnn.required_dlls) {
            $cudnnCandidates += [pscustomobject]@{ Version = $candidate.Version; Bin = $candidate.Bin }
        }
    }
    if (Test-Path $cudnnRoot) {
        Get-ChildItem -Path $cudnnRoot -Recurse -Filter 'cudnn64_9.dll' -File -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.FullName -match '[\\/]bin[\\/](12\.\d+)[\\/]' -and
                (Test-RequiredDlls $_.DirectoryName $compatibility.cudnn.required_dlls)) {
                try { $cudnnCandidates += [pscustomobject]@{ Version = [version]$Matches[1]; Bin = $_.DirectoryName } } catch { }
            }
        }
    }

    $pair = $null
    foreach ($cuda in $cudaCandidates) {
        $matchingCudnn = $cudnnCandidates | Where-Object {
            $_.Version.Major -eq $cuda.Version.Major -and $_.Version.Minor -eq $cuda.Version.Minor
        } | Sort-Object Version -Descending | Select-Object -First 1
        if ($matchingCudnn) {
            $pair = [pscustomobject]@{ Version = $cuda.Version; CudaBin = $cuda.Bin; CudnnBin = $matchingCudnn.Bin }
            break
        }
    }
    # The packaged CTranslate2 probe is the real compatibility test. Do not
    # reject a working runtime merely because nvidia-smi is not available on
    # this user's PATH.
    if ($pair -and (Test-PackagedGpuRuntime $pair)) {
        return [ordered]@{
            runtime_schema = 2
            gpu_runtime_contract = [string]$compatibility.contract_id
            whisper_device = 'cuda'
            whisper_compute_type = 'float16'
            whisper_model = 'large-v3'
            cuda_bin_dir = $pair.CudaBin
            cudnn_bin_dir = $pair.CudnnBin
            profile_message = "NVIDIA GPU mode enabled (CUDA $($pair.Version) + matching cuDNN 9 verified by CTranslate2)."
        }
    }
    return [ordered]@{
        runtime_schema = 2
        gpu_runtime_contract = [string]$compatibility.contract_id
        whisper_device = 'cpu'
        whisper_compute_type = 'int8'
        whisper_model = 'small'
        cuda_bin_dir = ''
        cudnn_bin_dir = ''
        profile_message = 'CPU mode enabled. Install a matching supported CUDA 12 and cuDNN 9 pair, then run Configure ClipFinder runtime again.'
    }
}

function Test-PackagedGpuRuntime($Pair) {
    $appCandidates = [System.Collections.Generic.List[string]]::new()
    $appCandidates.Add((Join-Path $appRoot 'ClipFinder.exe'))
    $uninstallRoots = @(
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    foreach ($root in $uninstallRoots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | ForEach-Object {
            $entry = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            if ($entry.DisplayName -eq 'ClipFinder' -and $entry.InstallLocation) {
                $appCandidates.Add((Join-Path ([string]$entry.InstallLocation) 'ClipFinder.exe'))
            }
        }
    }
    $appCandidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\ClipFinder\ClipFinder.exe'))
    $appCandidates = @($appCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique)
    if (-not $appCandidates) {
        Write-Setup '[warning] ClipFinder.exe is not installed, so the CUDA pair could not be tested with CTranslate2.' 'Yellow'
        return $false
    }
    try {
        $probeArguments = "--gpu-runtime-probe `"$($Pair.CudaBin)`" `"$($Pair.CudnnBin)`""
        $probe = Start-Process -FilePath $appCandidates[0] -ArgumentList $probeArguments -Wait -PassThru -WindowStyle Hidden
        if ($probe.ExitCode -eq 0) {
            Write-Setup "[ok] CTranslate2 loaded CUDA $($Pair.Version) and matching cuDNN successfully." 'Green'
            return $true
        }
        Write-Setup "[warning] CUDA files were found, but the packaged CTranslate2 probe failed (exit code $($probe.ExitCode))." 'Yellow'
    }
    catch {
        Write-Setup "[warning] Could not run the packaged CTranslate2 GPU probe: $($_.Exception.Message)" 'Yellow'
    }
    return $false
}

Write-Setup 'ClipFinder post-install setup started.' 'Cyan'

if (-not (Find-ExecutableDirectory 'ffmpeg.exe')) {
    Install-WingetPackage 'Gyan.FFmpeg.Shared' 'FFmpeg' | Out-Null
}
$ffmpegDirectory = Find-ExecutableDirectory 'ffmpeg.exe'
if (-not $ffmpegDirectory) { $ffmpegDirectory = Find-WingetFfmpegDirectory }
if ($ffmpegDirectory) { Write-Setup "[ok] FFmpeg: $ffmpegDirectory" 'Green' }
else { Write-Setup '[warning] FFmpeg was not found. Video import and export will not work until it is installed.' 'Yellow' }

if (-not (Test-WebView2Runtime)) {
    Install-WingetPackage 'Microsoft.EdgeWebView2Runtime' 'Microsoft Edge WebView2 Runtime' | Out-Null
}
if (Test-WebView2Runtime) { Write-Setup '[ok] Microsoft Edge WebView2 Runtime is available.' 'Green' }
else { Write-Setup '[warning] WebView2 Runtime could not be verified. Install it manually if the desktop window does not open.' 'Yellow' }

if (-not (Test-VCRedist)) {
    Install-VCRedist | Out-Null
}
if (Test-VCRedist) { Write-Setup '[ok] Microsoft Visual C++ Redistributable is available.' 'Green' }
else { Write-Setup '[warning] Visual C++ Redistributable could not be verified.' 'Yellow' }

$profile = Get-CudaProfile
if ($previousProfile -and ([string]$previousProfile.whisper_device).ToLowerInvariant() -eq 'cpu') {
    $profile['whisper_device'] = 'cpu'
    $profile['whisper_compute_type'] = if ($previousProfile.whisper_compute_type) { [string]$previousProfile.whisper_compute_type } else { 'int8' }
    $profile['whisper_model'] = if ($previousProfile.whisper_model) { [string]$previousProfile.whisper_model } else { 'small' }
    $profile['cuda_bin_dir'] = ''
    $profile['cudnn_bin_dir'] = ''
    $profile['profile_message'] = 'CPU mode preserved from the existing ClipFinder configuration.'
}
if ($null -eq $ffmpegDirectory) { $ffmpegDirectory = '' }
$profile['ffmpeg_bin_dir'] = $ffmpegDirectory
$profile | ConvertTo-Json | Set-Content -Path $runtimePath -Encoding UTF8
Write-Setup "[ok] $($profile.profile_message)" $(if ($profile.whisper_device -eq 'cuda') { 'Green' } else { 'Yellow' })
Write-Setup 'The transcription model downloads on the first analysis, so the tester needs an internet connection for that first run.' 'Gray'
if ($ResultPath) {
    Set-Content -LiteralPath $ResultPath -Value ([string]$profile.whisper_device) -Encoding ASCII
}
if ($RequireGpu -and $profile.whisper_device -ne 'cuda') {
    Write-Setup '[error] The GPU add-on could not verify a working CUDA transcription runtime. ClipFinder remains usable in CPU mode.' 'Red'
    $messages | Set-Content -Path $statusPath -Encoding UTF8
    exit 2
}
$messages | Set-Content -Path $statusPath -Encoding UTF8
Write-Setup "Setup report saved to: $statusPath" 'Gray'
