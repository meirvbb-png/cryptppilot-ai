@echo off
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title CryptoPilot bots

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

rem (re)install when the venv is new, a previous install failed, or requirements.txt changed
if not exist .venv\Scripts\python.exe python -m venv .venv
fc /b requirements.txt .venv\installed.txt >nul 2>nul
if errorlevel 1 (
  echo Installing libraries...
  .venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo Library install failed. Check the internet connection and run start.bat again.
    pause
    exit /b 1
  )
  copy /y requirements.txt .venv\installed.txt >nul
)
if not exist .env copy .env.example .env >nul

rem keep the bots running: restart automatically after a crash or a network failure
:run
.venv\Scripts\python -m app
rem 2 = no free port, 3 = the bots already run in another window: restarting would not help
if errorlevel 2 if not errorlevel 4 (
  pause
  exit /b 2
)
echo.
echo The app stopped. Restarting in 15 seconds - close this window to stop the bots.
timeout /t 15 /nobreak >nul
goto run
