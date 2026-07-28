<#
Post-install bootstrap for the packaged ClipFinder desktop application.
It installs only public Windows runtime components that are missing. CUDA and
cuDNN are intentionally not downloaded automatically: the correct NVIDIA
combination depends on the tester's driver and is optional because CPU mode
keeps the application usable.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateRoot = Join-Path $env:LOCALAPPDATA 'ClipFinder'
$statusPath = Join-Path $stateRoot 'setup-status.txt'
$runtimePath = Join-Path $stateRoot 'runtime.json'
New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
$messages = [System.Collections.Generic.List[string]]::new()

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

function Get-CudaProfile {
    $nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    $cudaRoot = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
    $cudaBins = @()
    if (Test-Path $cudaRoot) {
        $cudaBins = Get-ChildItem -Path $cudaRoot -Directory -Filter 'v12*' -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'bin' } | Where-Object { Test-Path $_ }
    }
    $readyBin = $cudaBins | Where-Object {
        (Test-Path (Join-Path $_ 'cublas64_12.dll')) -and (Test-Path (Join-Path $_ 'cudnn64_9.dll'))
    } | Select-Object -First 1
    if ($nvidia -and $readyBin) {
        return [ordered]@{ whisper_device = 'cuda'; whisper_compute_type = 'float16'; whisper_model = 'large-v3'; cuda_bin_dir = $readyBin; profile_message = 'NVIDIA GPU mode enabled (CUDA 12 + cuDNN 9 detected).' }
    }
    return [ordered]@{ whisper_device = 'cpu'; whisper_compute_type = 'int8'; whisper_model = 'small'; cuda_bin_dir = ''; profile_message = 'CPU test mode enabled. Install CUDA 12 and cuDNN 9 later to enable faster NVIDIA transcription.' }
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
    Install-WingetPackage 'Microsoft.VCRedist.2015+.x64' 'Microsoft Visual C++ Redistributable 2015-2022 (x64)' | Out-Null
}
if (Test-VCRedist) { Write-Setup '[ok] Microsoft Visual C++ Redistributable is available.' 'Green' }
else { Write-Setup '[warning] Visual C++ Redistributable could not be verified.' 'Yellow' }

$profile = Get-CudaProfile
if ($null -eq $ffmpegDirectory) { $ffmpegDirectory = '' }
$profile['ffmpeg_bin_dir'] = $ffmpegDirectory
$profile | ConvertTo-Json | Set-Content -Path $runtimePath -Encoding UTF8
Write-Setup "[ok] $($profile.profile_message)" $(if ($profile.whisper_device -eq 'cuda') { 'Green' } else { 'Yellow' })
Write-Setup 'The transcription model downloads on the first analysis, so the tester needs an internet connection for that first run.' 'Gray'
$messages | Set-Content -Path $statusPath -Encoding UTF8
Write-Setup "Setup report saved to: $statusPath" 'Gray'
