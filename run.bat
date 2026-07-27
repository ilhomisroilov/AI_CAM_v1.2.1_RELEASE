@echo off
setlocal
set "RELEASE_ROOT=%~dp0"
set "VENV_PYTHON=%RELEASE_ROOT%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
  echo AI_CAM v1.2.1 local environment is missing.
  echo Run: powershell -ExecutionPolicy Bypass -File "%RELEASE_ROOT%setup.ps1"
  exit /b 2
)

"%VENV_PYTHON%" "%RELEASE_ROOT%run.py" %*
set "AI_CAM_EXIT=%ERRORLEVEL%"
exit /b %AI_CAM_EXIT%
