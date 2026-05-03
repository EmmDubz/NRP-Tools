@echo off
setlocal EnableDelayedExpansion
title NirvaliStat — Analyze Overlay
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
echo === STEP 1: Resource Overlay (slow) ===
echo   Input:  maps\Political Map.png + maps\Resource Map aligned.png
echo   Config: config.yaml
echo   Output: output\results.csv , output\results.json
echo.

python scripts\analyze_resources.py --political "maps\Political Map.png" --resources "maps\Resource Map aligned.png" --config config.yaml --halo-km 80 --out output\results.csv --json output\results.json

if errorlevel 1 (
  echo.
  echo STEP 1 FAILED
  pause
  exit /b 1
)

echo.
echo === STEP 2: Rebuild Commodity Markdown (fast) ===
python scripts\build_commodity_view_md.py

if errorlevel 1 (
  echo.
  echo STEP 2 FAILED
  pause
  exit /b 1
)

echo.
echo OK: output\results.* and cycles\IRP_2008\PROVISIONAL_commodity_view.md
echo.
pause
