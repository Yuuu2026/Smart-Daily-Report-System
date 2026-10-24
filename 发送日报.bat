@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 发送今日日报

rem ============================================================
rem  Double-click this file to send today's report.
rem  Put it on the desktop as a shortcut if you like.
rem ============================================================
set "PYTHON_CMD="
set "PYTHON_ARGS="

call :try_python "py" "-3.12"
if not defined PYTHON_CMD call :try_python "py" "-3"
if not defined PYTHON_CMD call :try_python "python" ""
if not defined PYTHON_CMD call :try_python "%USERPROFILE%\anaconda3\python.exe" ""
if not defined PYTHON_CMD call :try_python "%USERPROFILE%\miniconda3\python.exe" ""

if not defined PYTHON_CMD (
  echo.
  echo   Python 3 with Flask was not found.
  echo   Install it, then run:
  echo     python -m pip install -r web\requirements.txt
  echo.
  pause
  exit /b 1
)

"%PYTHON_CMD%" %PYTHON_ARGS% scripts\report_cli.py %*
set "CODE=%ERRORLEVEL%"
echo.
if "%~1"=="" pause
exit /b %CODE%

rem ============================================================
rem  Subroutine: accept an interpreter only if it is Python 3
rem  AND Flask is importable.
rem ============================================================
:try_python
set "_CAND=%~1"
set "_ARGS=%~2"
if not defined _CAND exit /b 0
"%_CAND%" %_ARGS% -c "import sys, flask; sys.exit(0 if sys.version_info[0] == 3 else 1)" >nul 2>&1
if errorlevel 1 exit /b 0
set "PYTHON_CMD=%_CAND%"
set "PYTHON_ARGS=%_ARGS%"
exit /b 0
