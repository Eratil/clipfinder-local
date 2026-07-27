<#!
One-time setup for a fresh Windows computer.
Installs what can be safely installed from the console, then prepares this copy
of ClipFinder. CUDA/cuDNN are deliberately not installed here because the right
version depends on the NVIDIA driver and hardware.
#>
param(
    [switch]$CpuFallback
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Set-Location $projectRoot

function Write-Step([string]$text) {
    Write-Host "`n=== $text ===" -ForegroundColor Cyan
}

function Get-Python311 {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = (& py -3.11 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1).Trim()
        if ($LASTEXITCODE -eq 0 -and $candidate -and (Test-Path $candidate)) { return $candidate }
    }
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    if (Test-Path $candidate) { return $candidate }
    return $null
}

function Install-WithWinget([string]$id, [string]$label) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Nie znaleziono winget. Zainstaluj App Installer z Microsoft Store, a potem uruchom ten instalator ponownie."
    }
    Write-Host "Instalowanie: $label" -ForegroundColor Yellow
    & winget install --id $id --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie zainstalowac: $label (winget id: $id)." }
}

try {
    if (-not (Test-Path (Join-Path $projectRoot 'requirements.txt'))) {
        throw 'Nie znaleziono requirements.txt. Uruchom instalator z katalogu ClipFinder Local.'
    }

    Write-Step 'Python 3.11'
    $python = Get-Python311
    if (-not $python) {
        Install-WithWinget 'Python.Python.3.11' 'Python 3.11 (64-bit)'
        $python = Get-Python311
    }
    if (-not $python) {
        throw 'Python zostal zainstalowany, ale nie jest jeszcze widoczny. Zamknij PowerShell, otworz go ponownie i uruchom Install-ClipFinder.cmd jeszcze raz.'
    }
    Write-Host "Uzywany Python: $python"

    Write-Step 'FFmpeg'
    if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
        Install-WithWinget 'Gyan.FFmpeg.Shared' 'FFmpeg'
    }
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
        Write-Host 'FFmpeg jest dostepny w PATH.' -ForegroundColor Green
    } else {
        Write-Host 'FFmpeg zostal zlecony do instalacji. Moze byc widoczny dopiero w nowym oknie PowerShell.' -ForegroundColor Yellow
    }

    Write-Step 'Srodowisko ClipFinder'
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Nie udalo sie utworzyc .venv.' }
    }
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'Aktualizacja pip nie powiodla sie.' }
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Instalacja pakietow aplikacji nie powiodla sie.' }
    if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }

    if ($CpuFallback) {
        Write-Step 'Tryb CPU'
        $envFile = Join-Path $projectRoot '.env'
        $content = Get-Content $envFile -Raw
        $content = $content -replace '(?m)^WHISPER_DEVICE=.*$', 'WHISPER_DEVICE=cpu'
        $content = $content -replace '(?m)^WHISPER_COMPUTE_TYPE=.*$', 'WHISPER_COMPUTE_TYPE=int8'
        Set-Content -Path $envFile -Value $content -NoNewline
        Write-Host 'Ustawiono tryb CPU. Analiza bedzie wolniejsza, ale nie wymaga CUDA.' -ForegroundColor Yellow
    }

    Write-Step 'Kontrola instalacji'
    & $venvPython scripts\doctor.py
    $doctorExit = $LASTEXITCODE
    if ($doctorExit -ne 0 -and -not $CpuFallback) {
        Write-Host "`nPython i pakiety aplikacji sa gotowe. Do analizy na GPU zainstaluj jeszcze CUDA 12.x oraz cuDNN 9, potem uruchom: python scripts\doctor.py" -ForegroundColor Yellow
        Write-Host 'Bez GPU mozesz uruchomic: Install-ClipFinder.cmd -CpuFallback' -ForegroundColor Yellow
    }

    Write-Host "`nGotowe. Uruchom teraz Start-ClipFinder-Desktop.cmd" -ForegroundColor Green
} catch {
    Write-Host "`nINSTALACJA NIE POWIODLA SIE: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
