@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Install

rem NOTE: this file must stay pure ASCII. Chinese text in a .bat gets
rem mis-parsed by cmd before chcp takes effect, which breaks the script.

set "VENV=%CD%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

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
echo [1/5] Python %PYVER% detected
echo       %CD%

if exist "%PY%" (
  echo [2/5] venv already exists, reusing it
) else (
  echo [2/5] creating virtual environment at .venv ...
  rem Deliberately NOT using --system-site-packages.
  rem Borrowing the system Python's packages saved a download, but when the
  rem user cleaned the C: drive, torch\lib\cublasLt64_12.dll and the
  rem silero_vad package were deleted, which silently killed GPU inference
  rem and degraded VAD. Keep everything inside the project folder.
  python -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] failed to create venv
    pause
    exit /b 1
  )
)

rem Keep every cache off the system drive
set "HF_HOME=%CD%\models"
set "HUGGINGFACE_HUB_CACHE=%CD%\models"
set "TORCH_HOME=%CD%\models\torch"
set "PIP_CACHE_DIR=%CD%\.cache\pip"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo [3/5] installing runtime dependencies ...
echo       (models and caches go to %CD%, not the system drive)
echo.
rem Domestic mirror first (tens of times faster in mainland China),
rem fall back to the official PyPI only if it fails.
"%PY%" -m pip install -i "%MIRROR%" --trusted-host pypi.tuna.tsinghua.edu.cn --no-input --progress-bar off --timeout 30 --retries 3 -r requirements.txt
if errorlevel 1 (
  echo.
  echo [WARN] mirror failed, retrying with official PyPI...
  "%PY%" -m pip install --no-input --progress-bar off --timeout 60 --retries 5 -r requirements.txt
)
if errorlevel 1 (
  echo.
  echo [WARN] some packages failed. Retrying the critical ones individually...
  "%PY%" -m pip install -i "%MIRROR%" --trusted-host pypi.tuna.tsinghua.edu.cn --no-input --progress-bar off faster-whisper
  "%PY%" -m pip install -i "%MIRROR%" --trusted-host pypi.tuna.tsinghua.edu.cn --no-input --progress-bar off sounddevice
)

echo.
echo [4/5] CUDA runtime (needed for GPU recognition, about 1.2 GB)
echo       Without it recognition falls back to CPU and will lag behind
echo       a fast speaker. Skip with:  01_install.bat --no-gpu
set "SKIPGPU="
if /i "%1"=="--no-gpu" set "SKIPGPU=1"
if defined SKIPGPU (
  echo       skipped by --no-gpu
) else (
  "%PY%" -m pip install -i "%MIRROR%" --trusted-host pypi.tuna.tsinghua.edu.cn --no-input --progress-bar off --timeout 30 --retries 3 nvidia-cublas-cu12 nvidia-cudnn-cu12
  if errorlevel 1 (
    echo       [warn] CUDA runtime not installed, will use CPU recognition
  )
)

echo.
echo [5/5] verifying ...
"%PY%" -c "import faster_whisper, sounddevice, numpy; print('core deps OK')"
"%PY%" -m backend.main --doctor

echo.
echo ================================================================
echo  Next step: run script 02 to download the models.
echo ================================================================
pause
