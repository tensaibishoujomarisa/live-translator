@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Diagnostics

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo .venv not found, falling back to the system python.
  set "PY=python"
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "HF_HOME=%CD%\models"

echo ================================================================
echo  1. Environment check
echo ================================================================
"%PY%" -m backend.main --doctor
echo.
echo ================================================================
echo  2. Audio capture devices (loopback capable ones are marked)
echo ================================================================
"%PY%" -m backend.main --list-devices
echo.
echo ================================================================
echo  3. Live audio level test - play something with sound NOW
echo ================================================================
"%PY%" -m backend.main --probe-audio 4
echo.
pause
