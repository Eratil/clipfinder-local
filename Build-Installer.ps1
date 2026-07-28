<#
Build ClipFinder's Windows installers from a clean checkout.

The normal installer packages the application only. The optional GPU add-on is
a separate, disk-spanning package containing CUDA and cuDNN, so testers without
NVIDIA hardware never need to download or extract those large installers.
#>
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.1.0',
    [Alias('IncludeGpuDependencies')]
    [switch]$GpuAddon
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$innoCandidates = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
)
$iscc = $innoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $GpuAddon -and -not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Run Install-ClipFinder.cmd first.'
}
if (-not $iscc) {
    throw 'Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php, then run this script again.'
}

Set-Location $projectRoot
if (-not $GpuAddon) {
    $appVersion = (& $python -c "from app.version import __version__; print(__version__)" | Select-Object -Last 1).Trim()
    if ($appVersion -ne $Version) {
        throw "Version mismatch: app/version.py is $appVersion but -Version is $Version. Bump app/version.py before building a release."
    }
}

if ($GpuAddon) {
    $gpuInstallers = @(
        (Join-Path $projectRoot '..\cuda_12.9.2_576.57_windows.exe'),
        (Join-Path $projectRoot '..\cudnn_9.24.0_windows_x86_64.exe')
    )
    foreach ($installer in $gpuInstallers) {
        if (-not (Test-Path -LiteralPath $installer)) {
            throw "GPU installer was not found: $installer. Put the CUDA 12.9.2 and cuDNN 9.24 installers in the parent outputs folder."
        }
    }
    Write-Host 'Building the separate NVIDIA GPU add-on. It contains only CUDA and cuDNN.' -ForegroundColor Yellow
}
else {
    Write-Host 'Installing build dependencies...' -ForegroundColor Cyan
    & $python -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }

    Write-Host 'Building the ClipFinder application folder...' -ForegroundColor Cyan
    & $python -m PyInstaller `
        --noconfirm --clean --onedir --windowed `
        --name ClipFinder `
        --icon "assets\clipfinder.ico" `
        --add-data "app\static;app\static" `
        --add-data "assets\clipfinder.ico;assets" `
        --collect-submodules app `
        --hidden-import webview.platforms.winforms `
        --hidden-import webview.platforms.win32 `
        --hidden-import clr `
        --collect-all faster_whisper `
        --collect-all ctranslate2 `
        --collect-all sentence_transformers `
        --collect-all transformers `
        --collect-all tokenizers `
        --collect-all torch `
        --collect-all cv2 `
        --hidden-import yt_dlp `
        clipfinder_desktop.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build the installer.' }
}

Write-Host 'Building the setup executable...' -ForegroundColor Cyan
$isccArguments = @("/DMyAppVersion=$Version")
$installerScript = if ($GpuAddon) { 'installer\ClipFinder-GPU-Addon.iss' } else { 'installer\ClipFinder.iss' }
& $iscc @isccArguments $installerScript
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup could not build the installer.' }

$installerName = if ($GpuAddon) { "ClipFinder-GPU-Addon-$Version.exe" } else { "ClipFinder-Setup-$Version.exe" }
Write-Host "`nReady: installer-output\$installerName" -ForegroundColor Green
if ($GpuAddon) {
    Write-Host 'Keep every generated .bin file next to the GPU add-on executable. Send the whole GPU add-on folder or put all of its files into one ZIP.' -ForegroundColor Yellow
}
