@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo Virtual environment is missing. Run Install-ClipFinder.cmd first.
  pause
  exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0clipfinder_desktop.py"
