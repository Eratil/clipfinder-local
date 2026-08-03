<#
Build ClipFinder's Windows installers from a clean checkout.

The normal installer packages the application only. The optional GPU add-on is
a separate, disk-spanning package containing CUDA and cuDNN, so testers without
NVIDIA hardware never need to download or extract those large installers.
#>
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.1.0',
    [string]$PythonPath = '',
    [Alias('IncludeGpuDependencies')]
    [switch]$GpuAddon
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = if ($PythonPath) { $PythonPath } else { Join-Path $projectRoot '.venv\Scripts\python.exe' }
$innoCandidates = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
)
$iscc = $innoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $GpuAddon -and -not (Test-Path -LiteralPath $python)) {
    throw 'Missing Python build environment. Run Install-ClipFinder.cmd first or provide -PythonPath.'
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
    $vcRedist = Join-Path $projectRoot 'installer\third_party\vc_redist.x64.exe'
    if (-not (Test-Path -LiteralPath $vcRedist)) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $vcRedist) | Out-Null
        Write-Host 'Downloading the official Microsoft Visual C++ Runtime for the installer...' -ForegroundColor Cyan
        Invoke-WebRequest -Uri 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile $vcRedist -UseBasicParsing
    }
    Write-Host 'Installing build dependencies...' -ForegroundColor Cyan
    & $python -m pip install --upgrade -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }

    # These packages are not used by ClipFinder.  They can be left behind by
    # an earlier GPU Python environment and then make Transformers try to load
    # incompatible CUDA extensions while building the otherwise CPU-only app.
    & $python -m pip uninstall -y torchaudio torchvision easyocr
    if ($LASTEXITCODE -ne 0) { throw 'Could not remove optional stale Torch packages.' }

    $torchRuntime = (& $python -c "import torch; print(torch.__version__ + '|' + str(torch.version.cuda or ''))" | Select-Object -Last 1).Trim()
    if ($torchRuntime -match '\|.+$') {
        throw "The base installer requires CPU-only PyTorch, but the build environment has $torchRuntime. Run the requirements installation again before building."
    }

    Write-Host 'Building the ClipFinder application folder...' -ForegroundColor Cyan
    & $python -m PyInstaller `
        --noconfirm --clean --onedir --windowed `
        --name ClipFinder `
        --icon "assets\clipfinder.ico" `
        --add-data "app\static;app\static" `
        --add-data "assets\clipfinder.ico;assets" `
        --add-data "assets\close-pop.wav;assets" `
        --add-data "assets\fonts;assets\fonts" `
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
        --collect-all certifi `
        --collect-all truststore `
        --hidden-import yt_dlp `
        clipfinder_desktop.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build the installer.' }

    Write-Host 'Building the small update helper...' -ForegroundColor Cyan
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name ClipFinderUpdateHelper `
        clipfinder_update_helper.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build the update helper.' }
    Copy-Item -LiteralPath 'dist\ClipFinderUpdateHelper.exe' -Destination 'dist\ClipFinder\ClipFinderUpdateHelper.exe' -Force

    # PyTorch's Windows wheel carries development archives and an entire CUDA
    # runtime. Text embeddings safely fall back to CPU; GPU transcription is
    # supplied by the optional CUDA/cuDNN add-on. Keeping these files here
    # makes the standard installer over 4 GB and too large for GitHub Releases.
    # Depending on PyInstaller's collection layout, Torch DLLs can end up
    # directly in _internal or in torch\lib. Remove CUDA binaries from both
    # places; the separate GPU add-on provides those at runtime when needed.
    $torchLibraryDirectories = @(
        (Join-Path $projectRoot 'dist\ClipFinder\_internal\torch\lib'),
        (Join-Path $projectRoot 'dist\ClipFinder\_internal'),
        (Join-Path $projectRoot 'dist\ClipFinder\_internal\ctranslate2')
    ) | Where-Object { Test-Path -LiteralPath $_ }
    $unneededTorchFiles = @(
        '*.lib', '*.exp', '*.pdb',
        'c10_cuda.dll', 'torch_cuda.dll', 'caffe2_nvrtc.dll',
        'cublas*.dll', 'cudart*.dll', 'cudnn*.dll', 'cufft*.dll',
        'cupti*.dll', 'curand*.dll', 'cusolver*.dll', 'cusparse*.dll',
        'nvrtc*.dll', 'nvJitLink*.dll', 'nvToolsExt*.dll'
    )
    $removedBytes = 0
    foreach ($directory in $torchLibraryDirectories) {
        foreach ($pattern in $unneededTorchFiles) {
            Get-ChildItem -Path $directory -Filter $pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
                $removedBytes += $_.Length
                Remove-Item -LiteralPath $_.FullName -Force
            }
        }
    }
    Write-Host ("Removed {0:N1} MB of optional Torch CUDA/development files." -f ($removedBytes / 1MB)) -ForegroundColor DarkGray
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
