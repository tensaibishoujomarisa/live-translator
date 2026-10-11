@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Install

set "VENV=%CD%\.venv"
set "PY=%VENV%\Scripts\python.exe"

echo ================================================================
echo   Japanese Live Translator - one-time setup
echo ================================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] python not found in PATH.
  echo         Install Python 3.10-3.12 first, then run this again.
  pause
  exit /b 1
)

for /f "delims=" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set "PYVER=%%v"
echo [1/4] Python %PYVER% detected
echo       %CD%

if exist "%PY%" (
  echo [2/4] venv already exists, reusing it
) else (
  echo [2/4] creating virtual environment at .venv ...
  rem Reuse global site-packages to avoid a multi-GB torch download
  python -m venv --system-site-packages "%VENV%"
  if errorlevel 1 (
    echo [ERROR] failed to create venv
    pause
    exit /b 1
  )
)

rem Keep model caches off the system drive
set "HF_HOME=%CD%\models"
set "HUGGINGFACE_HUB_CACHE=%CD%\models"
set "TORCH_HOME=%CD%\models\torch"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo [3/4] installing runtime dependencies ...
echo       (models and caches go to %CD%\models, not the system drive)
echo.
"%PY%" -m pip install --no-input --progress-bar off --timeout 60 --retries 5 -r requirements.txt
if errorlevel 1 (
  echo.
  echo [WARN] some packages failed. Retrying the critical ones individually...
  "%PY%" -m pip install --no-input --progress-bar off faster-whisper
  "%PY%" -m pip install --no-input --progress-bar off sounddevice
)

echo.
echo [4/4] verifying ...
"%PY%" -c "import faster_whisper, sounddevice, numpy; print('core deps OK')"

echo.
echo ================================================================
echo  Next step: run script 02 to download the models.
echo ================================================================
pause
