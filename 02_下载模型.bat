@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Download Models

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] .venv not found. Run the install script 01 first.
  pause
  exit /b 1
)

set "HF_HOME=%CD%\models"
set "HUGGINGFACE_HUB_CACHE=%CD%\models"
set "TORCH_HOME=%CD%\models\torch"

echo ================================================================
echo  Downloading the speech recognition model (one time only)
echo  large-v3-turbo is about 1.6 GB, cache goes to %CD%\models
echo ================================================================
echo.
echo  This is the ONLY step that needs internet, and a proxy is fine.
echo  After this, recognition runs fully offline.
echo.

rem Keep the set /p prompt to plain ASCII: brackets or commas in it can be
rem mis-parsed by cmd. The 2^>nul guards against a non-interactive stdin.
set "MODEL=large-v3-turbo"
set /p MODEL="Model - press Enter for the default: " 2>nul
if not defined MODEL set "MODEL=large-v3-turbo"

echo.
"%PY%" -m scripts.setup_whisper --model %MODEL%
if errorlevel 1 (
  echo.
  echo [HINT] If the download stalled, try the mirror:
  echo        %PY% -m scripts.setup_whisper --model %MODEL% --mirror
  echo        or set a proxy first:  set HTTPS_PROXY=http://127.0.0.1:7890
  echo.
  pause
  exit /b 1
)

echo.
echo ================================================================
echo  Optional: local (offline, free) translation model, about 300 MB
echo  It runs on CPU so it does NOT compete with recognition for GPU.
echo ================================================================
set /p LOCALMT="Also download the local translation model? Type y then Enter: " 2>nul
if /i "%LOCALMT%"=="y" (
  "%PY%" -m scripts.setup_local_mt
  echo.
  echo  To use it, set translate.provider to "local" in config.yaml
)

echo.
echo ================================================================
echo  Done. Next: run script 03 to start the subtitle window.
echo ================================================================
pause
