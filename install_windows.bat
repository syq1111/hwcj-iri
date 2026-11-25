@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  set "PY_LAUNCHER=python"
) else (
  set "PY_LAUNCHER=py -3"
)
%PY_LAUNCHER% -m venv "%SCRIPT_DIR%.venv"
if errorlevel 1 exit /b 1
"%SCRIPT_DIR%.venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%SCRIPT_DIR%.venv\Scripts\python.exe" -m pip install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 exit /b 1
echo Installation completed: %SCRIPT_DIR%.venv
