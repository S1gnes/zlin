@echo off
rem ASCII only on purpose: cmd reads .bat in the OEM codepage and Cyrillic breaks parsing.
rem Adds a shortcut to the user's Startup folder, so the bot starts at every logon.
rem No administrator rights needed (Task Scheduler would ask for them).
setlocal
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LINK=%STARTUP%\zlinbot.lnk"
set "TARGET=%~dp0run.bat"

if not exist "%TARGET%" (
    echo [!] run.bat not found next to this file.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath = '%TARGET%';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'zlinbot - Zlin news bot';" ^
  "$s.Save()"

if errorlevel 1 (
    echo [!] Could not create the shortcut.
    pause
    exit /b 1
)

echo [*] Autostart is ON.
echo     Shortcut: %LINK%
echo     The bot will start minimized at every logon.
echo.
echo     Start now:  run.bat
echo     Stop:       stop.bat
echo     Turn off:   uninstall-autostart.bat
echo.
echo     Note: this starts after YOU log in. If Windows reboots (updates) and
echo     nobody logs in, the bot stays down until the next logon.
pause
