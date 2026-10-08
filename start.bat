@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10 or later is required. Install from https://www.python.org/downloads/
  pause
  exit /b 1
)
python run.py
pause
