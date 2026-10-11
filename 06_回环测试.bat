@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Loopback Audio Test

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] .venv not found. Run the install script 01 first.
  pause
  exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HF_HOME=%CD%\models"

echo ================================================================
echo  Loopback end-to-end test
echo.
echo  This will:
echo    1. start capturing your system audio (WASAPI loopback)
echo    2. play a short Japanese sample through your speakers
echo    3. run VAD segmentation and speech recognition on what it captured
echo.
echo  Turn your volume UP (not muted) before continuing.
echo ================================================================
echo.
pause

"%PY%" -m scripts.test_loopback %*
echo.
echo If the level (rms) is very low, pick a different device:
echo   %PY% -m backend.main --list-devices
echo   then add its id to config.yaml under audio.device, e.g.  device: "pa:70"
echo.
pause
