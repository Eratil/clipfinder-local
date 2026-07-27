<#
Build a Windows beta installer from a clean ClipFinder checkout.

The resulting installer is written to installer-output. It packages application
code and Python dependencies, but deliberately excludes recordings, exports,
the database and downloaded AI models.
#>
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '0.1.0'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$innoCandidates = @(
    'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
    'C:\Program Files\Inno Setup 6\ISCC.exe'
)
$iscc = $innoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv. Run Install-ClipFinder.cmd first.'
}
if (-not $iscc) {
    throw 'Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php, then run this script again.'
}

Set-Location $projectRoot
Write-Host 'Installing build dependencies...' -ForegroundColor Cyan
& $python -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies.' }

Write-Host 'Building the ClipFinder application folder...' -ForegroundColor Cyan
& $python -m PyInstaller `
    --noconfirm --clean --onedir --windowed `
    --name ClipFinder `
    --add-data "app\static;app\static" `
    --collect-submodules app `
    --collect-all webview `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all sentence_transformers `
    --collect-all transformers `
    --collect-all tokenizers `
    --collect-all torch `
    --collect-all cv2 `
    --hidden-import yt_dlp `
    clipfinder_desktop.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller could not build ClipFinder.' }

Write-Host 'Building the setup executable...' -ForegroundColor Cyan
& $iscc "/DMyAppVersion=$Version" 'installer\ClipFinder.iss'
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup could not build the installer.' }

Write-Host "`nReady: installer-output\ClipFinder-Setup-$Version.exe" -ForegroundColor Green
