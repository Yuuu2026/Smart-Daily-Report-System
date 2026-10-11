@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_CMD="

where python >nul 2>&1
if not errorlevel 1 (
  python -c "import keyring" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import keyring" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_CMD=py"
      set "PYTHON_ARGS=-3"
    )
  )
)

if not defined PYTHON_CMD if exist "%USERPROFILE%\anaconda3\python.exe" (
  "%USERPROFILE%\anaconda3\python.exe" -c "import keyring" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=%USERPROFILE%\anaconda3\python.exe"
)

if not defined PYTHON_CMD if exist "%USERPROFILE%\miniconda3\python.exe" (
  "%USERPROFILE%\miniconda3\python.exe" -c "import keyring" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=%USERPROFILE%\miniconda3\python.exe"
)

if not defined PYTHON_CMD (
  echo Python 3 with keyring was not found.
  echo Install Python 3, then run:
  echo   python -m pip install -r web\requirements.txt
  pause
  exit /b 1
)

"%PYTHON_CMD%" %PYTHON_ARGS% configure_email.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %APP_EXIT_CODE%
