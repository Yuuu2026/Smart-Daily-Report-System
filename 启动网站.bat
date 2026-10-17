@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  If the sites are already running, just open the browser.
rem ============================================================
netstat -ano | findstr "LISTENING" | findstr ":5000 " >nul 2>&1
if not errorlevel 1 (
  echo The sites are already running. Opening browser...
  start "" http://127.0.0.1:5000/archive
  start "" http://127.0.0.1:5001/login
  exit /b 0
)

rem ============================================================
rem  Pick a Python 3 interpreter that has Flask installed.
rem  (Python 2 is explicitly rejected: it cannot run this project.)
rem ============================================================
set "PYTHON_CMD="
set "PYTHON_ARGS="

if not defined PYTHON_CMD call :try_python "py" "-3.12"
if not defined PYTHON_CMD call :try_python "%USERPROFILE%\anaconda3\python.exe" ""
if not defined PYTHON_CMD call :try_python "py" "-3"
if not defined PYTHON_CMD call :try_python "python" ""
if not defined PYTHON_CMD call :try_python "%USERPROFILE%\miniconda3\python.exe" ""
if not defined PYTHON_CMD call :try_python "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" ""

if not defined PYTHON_CMD (
  echo Python 3 with Flask was not found.
  echo Install Python 3, then run:
  echo   python -m pip install -r web\requirements.txt
  echo Or add your Python 3 installation to PATH and run this script again.
  pause
  exit /b 1
)

echo Using interpreter: %PYTHON_CMD% %PYTHON_ARGS%
"%PYTHON_CMD%" %PYTHON_ARGS% start_web.py
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" pause
exit /b %APP_EXIT_CODE%

rem ============================================================
rem  Subroutine: accept an interpreter only if it is Python 3
rem  AND Flask is importable.
rem ============================================================
:try_python
set "_CAND=%~1"
set "_ARGS=%~2"
if not defined _CAND exit /b 0
"%_CAND%" %_ARGS% -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
"%_CAND%" %_ARGS% -c "import flask" >nul 2>&1
if errorlevel 1 exit /b 0
set "PYTHON_CMD=%_CAND%"
set "PYTHON_ARGS=%_ARGS%"
exit /b 0
