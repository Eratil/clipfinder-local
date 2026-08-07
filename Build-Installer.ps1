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
    [switch]$GpuAddon,
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$GpuAddonVersion = '1.0.0',
    [string]$CudaInstallerPath = '',
    [string]$CudnnInstallerPath = '',
    [switch]$AllowDirtyTree,
    [string]$PreviousVersion = '',
    [string]$PreviousArchive = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$bootstrapPython = ''
$innoCandidates = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
)
$iscc = $innoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $GpuAddon) {
    if ($PythonPath) {
        $bootstrapPython = $PythonPath
    }
    else {
        $launcher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
        if ($launcher) {
            $detected = @(& $launcher.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null) | Select-Object -Last 1
            if ($LASTEXITCODE -eq 0 -and $detected -and (Test-Path -LiteralPath $detected)) {
                $bootstrapPython = [string]$detected
            }
        }
        if (-not $bootstrapPython) {
            $developmentPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
            if (Test-Path -LiteralPath $developmentPython) { $bootstrapPython = $developmentPython }
        }
    }
    if (-not $bootstrapPython -or -not (Test-Path -LiteralPath $bootstrapPython)) {
        throw 'Python 3.11 x64 was not found. Install it or pass its python.exe with -PythonPath.'
    }
    & $bootstrapPython -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) and sys.maxsize > 2**32 else 1)'
    if ($LASTEXITCODE -ne 0) {
        throw 'Release builds require 64-bit Python 3.11.'
    }
}
if (-not $iscc) {
    throw 'Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php, then run this script again.'
}

Set-Location $projectRoot
$compatibilityPath = Join-Path $projectRoot 'installer\runtime-compatibility.json'
try {
    $compatibility = Get-Content -Raw -LiteralPath $compatibilityPath -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw "Could not read runtime-compatibility.json: $($_.Exception.Message)"
}
if ($compatibility.schema -ne 1 -or -not $compatibility.contract_id) {
    throw 'runtime-compatibility.json has an unsupported format.'
}
if (-not $GpuAddon) {
    if (-not $AllowDirtyTree -and (Get-Command git -ErrorAction SilentlyContinue)) {
        $dirtyFiles = @(git status --porcelain)
        if ($LASTEXITCODE -ne 0) {
            throw 'Git could not inspect the worktree. Refusing to build release provenance from an unknown state.'
        }
        if ($dirtyFiles.Count -gt 0) {
            throw 'The Git worktree is not clean. Commit the release changes first, or use -AllowDirtyTree only for a local test build.'
        }
    }
}

if ($GpuAddon) {
    if ($GpuAddonVersion -ne [string]$compatibility.gpu_addon_version) {
        throw "GPU add-on version mismatch: runtime-compatibility.json requires $($compatibility.gpu_addon_version), but -GpuAddonVersion is $GpuAddonVersion."
    }
    $gpuInstallers = @(
        $(if ($CudaInstallerPath) { $CudaInstallerPath } else { Join-Path $projectRoot '..\cuda_12.9.2_576.57_windows.exe' }),
        $(if ($CudnnInstallerPath) { $CudnnInstallerPath } else { Join-Path $projectRoot '..\cudnn_9.24.0_windows_x86_64.exe' })
    )
    foreach ($installer in $gpuInstallers) {
        if (-not (Test-Path -LiteralPath $installer)) {
            throw "GPU installer was not found: $installer. Put the CUDA 12.9.2 and cuDNN 9.24 installers in the parent outputs folder."
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'NVIDIA') {
            throw "GPU installer does not have a valid NVIDIA signature: $installer"
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
    $vcSignature = Get-AuthenticodeSignature -LiteralPath $vcRedist
    if ($vcSignature.Status -ne 'Valid' -or $vcSignature.SignerCertificate.Subject -notmatch 'Microsoft') {
        throw 'The Visual C++ Runtime installer does not have a valid Microsoft Authenticode signature.'
    }

    # Release builds never reuse the development environment. A clean, local
    # build venv prevents stale OpenCV/CUDA/Pydantic packages from leaking into
    # the installer and changing its behaviour between machines.
    $buildEnvironment = Join-Path $projectRoot '.build-venv'
    $python = Join-Path $buildEnvironment 'Scripts\python.exe'
    Write-Host 'Creating a clean Python 3.11 build environment...' -ForegroundColor Cyan
    & $bootstrapPython -m venv --clear $buildEnvironment
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the clean build environment.' }
    Write-Host 'Installing pinned build dependencies...' -ForegroundColor Cyan
    # Use the Windows certificate store. This keeps certificate verification
    # enabled while working on systems with an antivirus or enterprise HTTPS
    # certificate installed locally.
    & $python -m pip install --use-feature=truststore --upgrade 'pip==26.1.2'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned pip version.' }
    & $python -m pip install --use-feature=truststore 'setuptools==83.0.0' 'wheel==0.45.1'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the pinned Python build tools.' }
    # proxy-tools is a tiny pure-Python dependency of pywebview which PyPI
    # publishes as an sdist only. Keep every native dependency wheel-only,
    # while allowing pip to build this one deterministic pure-Python wheel.
    & $python -m pip install --use-feature=truststore --no-build-isolation --only-binary=:all: --no-binary=proxy-tools -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'The pinned build environment has dependency conflicts.' }
    & $python tools\verify_release.py preflight --project-root $projectRoot --version $Version
    if ($LASTEXITCODE -ne 0) { throw 'Release preflight failed.' }

    Write-Host 'Building the ClipFinder application folder...' -ForegroundColor Cyan
    & $python -m PyInstaller --noconfirm --clean ClipFinder.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build the installer.' }

    Write-Host 'Building the small update helper...' -ForegroundColor Cyan
    & $python -m PyInstaller --noconfirm --clean ClipFinderUpdateHelper.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build the update helper.' }
    Copy-Item -LiteralPath 'dist\ClipFinderUpdateHelper.exe' -Destination 'dist\ClipFinder\ClipFinderUpdateHelper.exe' -Force
    # These support files are part of the installed application too. Keep them
    # inside the release folder so compact patches update them just like the
    # frozen Python files instead of leaving an old repair script behind.
    Copy-Item -LiteralPath 'installer\Configure-ClipFinder.ps1' -Destination 'dist\ClipFinder\Configure-ClipFinder.ps1' -Force
    Copy-Item -LiteralPath 'TESTER-INSTALLATION.md' -Destination 'dist\ClipFinder\TESTER-INSTALLATION.md' -Force

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

    $gitSha = ''
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $gitResult = @(git rev-parse HEAD 2>$null) | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $gitResult) { $gitSha = ([string]$gitResult).Trim() }
    }
    & $python tools\verify_release.py write-build-info --project-root $projectRoot --dist 'dist\ClipFinder' --version $Version --git-sha $gitSha
    if ($LASTEXITCODE -ne 0) { throw 'Could not write build provenance.' }
    & $python tools\verify_release.py verify-dist --dist 'dist\ClipFinder'
    if ($LASTEXITCODE -ne 0) { throw 'The packaged folder failed structural validation.' }
    $smoke = Start-Process -FilePath (Join-Path $projectRoot 'dist\ClipFinder\ClipFinder.exe') -ArgumentList '--packaged-smoke-check' -Wait -PassThru
    if ($smoke.ExitCode -ne 0) { throw "The packaged application smoke test failed with exit code $($smoke.ExitCode)." }
}

Write-Host 'Building the setup executable...' -ForegroundColor Cyan
$effectiveInstallerVersion = if ($GpuAddon) { $GpuAddonVersion } else { $Version }
$isccArguments = @(
    "/DMyAppVersion=$effectiveInstallerVersion",
    "/DRuntimeContract=$([string]$compatibility.contract_id)"
)
if ($GpuAddon) {
    $isccArguments += "/DCudaInstallerPath=$($gpuInstallers[0])"
    $isccArguments += "/DCudnnInstallerPath=$($gpuInstallers[1])"
}
$installerScript = if ($GpuAddon) { 'installer\ClipFinder-GPU-Addon.iss' } else { 'installer\ClipFinder.iss' }
& $iscc @isccArguments $installerScript
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup could not build the installer.' }

$installerName = if ($GpuAddon) { "ClipFinder-GPU-Addon-$GpuAddonVersion.exe" } else { "ClipFinder-Setup-$Version.exe" }
Write-Host "`nReady: installer-output\$installerName" -ForegroundColor Green
if ($GpuAddon) {
    Write-Host 'Keep every generated .bin file next to the GPU add-on executable. Send the whole GPU add-on folder or put all of its files into one ZIP.' -ForegroundColor Yellow
}
else {
    # Keep one compressed copy of the current application folder.  On the
    # next release it lets us create a compact, verified file-level patch
    # instead of making existing users download the full setup EXE again.
    $cacheRoot = Join-Path $projectRoot 'release-cache'
    New-Item -ItemType Directory -Force -Path $cacheRoot | Out-Null
    $cacheName = "ClipFinder-files-$Version.zip"
    if ([bool]$PreviousVersion -ne [bool]$PreviousArchive) {
        throw 'Use -PreviousVersion and -PreviousArchive together when selecting an explicit patch baseline.'
    }
    if ($PreviousArchive) {
        if (-not (Test-Path -LiteralPath $PreviousArchive)) { throw "Previous release archive was not found: $PreviousArchive" }
        $previousCache = Get-Item -LiteralPath $PreviousArchive
        $previousVersion = $PreviousVersion
    }
    else {
        $previousCandidate = Get-ChildItem -Path $cacheRoot -Filter 'ClipFinder-files-*.zip' -File -ErrorAction SilentlyContinue |
            ForEach-Object {
                if ($_.Name -match '^ClipFinder-files-(\d+\.\d+\.\d+)\.zip$') {
                    [pscustomobject]@{ File = $_; Version = [version]$Matches[1]; Text = $Matches[1] }
                }
            } |
            Where-Object { $_.Version -lt [version]$Version } |
            Sort-Object Version -Descending |
            Select-Object -First 1
        $previousCache = if ($previousCandidate) { $previousCandidate.File } else { $null }
        $previousVersion = if ($previousCandidate) { $previousCandidate.Text } else { '' }
    }
    $manifestPath = Join-Path $projectRoot "installer-output\ClipFinder-manifest-$Version.json"
    # The first app containing the compact-update client will be 0.1.18.
    # Older clients understand only full setup EXEs, so do not publish a
    # misleading patch for them even if we still have their cached build.
    if ($previousCache -and ([version]$previousVersion) -ge [version]'0.1.18') {
        Write-Host "Building a compact update patch from $previousVersion to $Version..." -ForegroundColor Cyan
        try {
            & $python 'tools\build_update_package.py' patch --from-archive $previousCache.FullName --from-version $previousVersion --to-directory 'dist\ClipFinder' --to-version $Version --output-dir 'installer-output'
            if ($LASTEXITCODE -ne 0) { throw 'Patch generator returned a non-zero exit code.' }
        }
        catch {
            Write-Warning "Could not build an update patch. The full installer remains ready: $($_.Exception.Message)"
            & $python 'tools\build_update_package.py' manifest --source 'dist\ClipFinder' --version $Version --output $manifestPath
            if ($LASTEXITCODE -ne 0) { throw 'Could not build the release manifest.' }
        }
    }
    else {
        & $python 'tools\build_update_package.py' manifest --source 'dist\ClipFinder' --version $Version --output $manifestPath
        if ($LASTEXITCODE -ne 0) { throw 'Could not build the release manifest.' }
    }
    $cachePath = Join-Path $cacheRoot $cacheName
    & $python 'tools\build_update_package.py' cache --source 'dist\ClipFinder' --version $Version --output $cachePath
    if ($LASTEXITCODE -ne 0) { throw 'Could not cache this release for the next update patch.' }
    # The updater needs only the direct predecessor. Older caches would use a
    # lot of disk space and still fall back safely to the full installer.
    Get-ChildItem -Path $cacheRoot -Filter 'ClipFinder-files-*.zip' -File |
        Where-Object { $_.Name -ne $cacheName } |
        Remove-Item -Force
    Write-Host 'Add the setup EXE and ClipFinder-manifest JSON to the GitHub Release. If present, add the generated ClipFinder-patch ZIP too.' -ForegroundColor Yellow
}
