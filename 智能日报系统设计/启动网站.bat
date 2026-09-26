@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if not errorlevel 1 (
  python start_web.py
) else (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 start_web.py
  ) else (
    echo Python 3 was not found. Install Python 3 and add it to PATH.
    pause
    exit /b 1
  )
)

if errorlevel 1 pause
