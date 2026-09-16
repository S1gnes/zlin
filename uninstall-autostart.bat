@echo off
rem ASCII only on purpose (see install-autostart.bat).
rem Removes the Startup shortcut. The running bot is not touched - use stop.bat for that.
setlocal
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\zlinbot.lnk"

if not exist "%LINK%" (
    echo [*] Autostart was not enabled - nothing to remove.
    pause
    exit /b 0
)

del /f /q "%LINK%"
if exist "%LINK%" (
    echo [!] Could not remove %LINK%
    pause
    exit /b 1
)

echo [*] Autostart is OFF. The bot will no longer start at logon.
echo     A running bot keeps working until you close it or run stop.bat.
pause
