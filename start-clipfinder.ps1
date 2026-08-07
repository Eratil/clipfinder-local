$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'Virtual environment is missing. Follow the README installation steps first.' -ForegroundColor Red
    exit 1
}

while ($true) {
    Write-Host 'ClipFinder is starting at http://127.0.0.1:8000' -ForegroundColor Green
    & '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    Write-Host 'Server stopped. Restarting in 5 seconds. Close this window to stop the launcher.' -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
