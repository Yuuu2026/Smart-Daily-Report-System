@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_CMD="
set "PYTHON_ARGS="

where python >nul 2>&1
if not errorlevel 1 (
  python -c "import flask" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import flask" >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_CMD=py"
      set "PYTHON_ARGS=-3"
    )
  )
)

if not defined PYTHON_CMD if exist "%USERPROFILE%\anaconda3\python.exe" (
  "%USERPROFILE%\anaconda3\python.exe" -c "import flask" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=%USERPROFILE%\anaconda3\python.exe"
)

if not defined PYTHON_CMD if exist "%USERPROFILE%\miniconda3\python.exe" (
  "%USERPROFILE%\miniconda3\python.exe" -c "import flask" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=%USERPROFILE%\miniconda3\python.exe"
)

if not defined PYTHON_CMD if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -c "import flask" >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)

if not defined PYTHON_CMD (
  echo Python 3 with Flask was not found.
  echo Install Python 3, then run:
  echo   python -m pip install -r web\requirements.txt
  echo Or add your Python installation to PATH and run this script again.
  pause
  exit /b 1
)

"%PYTHON_CMD%" %PYTHON_ARGS% start_web.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" pause
exit /b %APP_EXIT_CODE%
