@echo off
rem ASCII only on purpose (see install-autostart.bat).
rem Stops the bot and the run.bat restart loop, so it does not come back immediately.
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$targets = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -like '*scripts\bot.py*' -or $_.CommandLine -like '*run.bat*') };" ^
  "if (-not $targets) { Write-Host '[*] Bot is not running.'; exit 0 };" ^
  "foreach ($p in $targets) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host ('[*] stopped PID ' + $p.ProcessId) } catch {} };" ^
  "Start-Sleep -Seconds 3;" ^
  "$left = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -like '*scripts\bot.py*' -or $_.CommandLine -like '*run.bat*') });" ^
  "Write-Host ('[*] still running: ' + $left.Count)"

pause
