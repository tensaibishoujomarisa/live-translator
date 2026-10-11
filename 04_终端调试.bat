@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Console Mode

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] .venv not found. Run the install script 01 first.
  pause
  exit /b 1
)

set "HF_HOME=%CD%\models"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo ================================================================
echo  Console mode - Japanese and Chinese subtitles print right here.
echo  Good for checking that audio capture and recognition work.
echo  Press Ctrl+C to stop.
echo ================================================================
echo.

"%PY%" -m backend.main --pretty %*
pause
