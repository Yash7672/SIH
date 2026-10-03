@echo off
REM ---------------------------------------------------------------------------
REM  RAKSHAK - double-click friendly launcher for scripts\start.ps1
REM
REM  Any argument is forwarded, e.g.
REM     start.bat -Mode docker
REM     start.bat -HostIp 192.168.1.20
REM     start.bat -NoMobile
REM     start.bat -Reset
REM     start.bat -ResetDb
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "scripts\start.ps1" (
  echo Could not find scripts\start.ps1 - run this from the repository root.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\start.ps1" %*

set "RAKSHAK_EXIT=%ERRORLEVEL%"
echo.
if not "%RAKSHAK_EXIT%"=="0" (
  echo RAKSHAK start finished with exit code %RAKSHAK_EXIT% - see the messages above
) else (
  echo RAKSHAK is running. Keep this window open, or close it: the services keep running.
)
echo Press any key to close this window.
pause >nul
endlocal