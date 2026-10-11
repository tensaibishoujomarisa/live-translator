@echo off
chcp 65001 >nul
cd /d "%~dp0"
title JP Live Translator - Subtitle Window

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] .venv not found. Run the install script 01 first.
  pause
  exit /b 1
)

rem Electron ships a multi-hundred-MB binary; keep it off the system drive
set "ELECTRON_CACHE=%CD%\.cache\electron"
set "electron_config_cache=%CD%\.cache\electron"
set "npm_config_cache=%CD%\.cache\npm"
set "HF_HOME=%CD%\models"
rem GitHub releases are very slow from mainland China; the mirror is ~40x faster.
rem If the mirror ever goes away, comment this line out to fall back to GitHub.
set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"

rem Some launchers set this; it makes Electron run as plain Node and everything breaks
set "ELECTRON_RUN_AS_NODE="

if not exist "%CD%\ui\node_modules\electron" (
  echo [1/2] Installing the Electron runtime ^(one time, about 100 MB^) ...
  pushd "%CD%\ui"
  call npm install --no-audit --no-fund
  popd
  if errorlevel 1 (
    echo [ERROR] npm install failed. Is Node.js installed?
    echo         Or just run the text-mode script 04 instead.
    pause
    exit /b 1
  )
)

echo [2/2] Starting subtitle window ...
echo.
echo   Ctrl+Shift+H  show / hide window
echo   Ctrl+Shift+P  pause / resume recognition
echo   Ctrl+Shift+M  mouse click-through (so you can click the stream below)
echo   Ctrl+Shift+Up/Down  font size
echo.
pushd "%CD%\ui"
call npx electron .
popd
pause
