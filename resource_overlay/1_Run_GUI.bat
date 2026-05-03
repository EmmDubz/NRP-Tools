@echo off
title NirvaliStat — Resource Overlay GUI
cd /d "%~dp0"

echo Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is not installed or not added to PATH.
  echo Please install Python 3.10+ and check "Add Python to PATH" during installation.
  pause
  exit /b 1
)

if not exist "scripts\.venv\" (
  echo ===================================================
  echo First-time setup: Creating virtual environment...
  echo This may take a minute or two. Please be patient!
  echo ===================================================
  python -m venv scripts\.venv
  call scripts\.venv\Scripts\activate.bat
  echo.
  echo Installing required packages...
  pip install -r scripts\requirements.txt
) else (
  call scripts\.venv\Scripts\activate.bat
)

echo.
echo Starting GUI...
python scripts\deposit_tuner_gui.py
if errorlevel 1 (
  echo.
  echo FAILED - Check the error message above.
  pause
  exit /b 1
)
