@echo off
rem ASCII only on purpose: cmd reads .bat in the OEM codepage, and Cyrillic bytes here
rem break command parsing. Human-facing Russian text is printed by Python instead.
setlocal
cd /d "%~dp0"
title zlinbot

echo === zlinbot ===

py -3.13 --version >nul 2>&1
if errorlevel 1 goto no_python

if not exist ".venv\Scripts\python.exe" goto setup
goto check_env

:setup
echo [*] Creating venv (Python 3.13)...
py -3.13 -m venv .venv
if errorlevel 1 goto venv_failed
echo [*] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto deps_failed
echo [*] Installing Chromium for Playwright (~200 MB, once)...
".venv\Scripts\python.exe" -m playwright install chromium

:check_env
if exist ".env" goto loop
echo [*] No .env found - creating from .env.example
copy /y ".env.example" ".env" >nul
start "" notepad ".env"
echo [!] Fill in BOT_TOKEN, ADMIN_ID, CHANNEL_ID, GEMINI_API_KEY and run again.
pause
exit /b 1

:loop
".venv\Scripts\python.exe" "scripts\bot.py"
set code=%errorlevel%
if "%code%"=="0" goto stopped
echo [!] Bot crashed (exit %code%). Restarting in 15 s. Ctrl+C to quit.
timeout /t 15 /nobreak >nul
goto loop

:stopped
echo [*] Bot exited normally.
exit /b 0

:no_python
echo [!] Python 3.13 not found. Install it from python.org (3.14+ does not build pydantic-core).
pause
exit /b 1

:venv_failed
echo [!] Could not create venv.
pause
exit /b 1

:deps_failed
echo [!] Could not install dependencies. See the output above.
pause
exit /b 1
