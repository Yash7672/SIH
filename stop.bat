@echo off
REM ---------------------------------------------------------------------------
REM  RAKSHAK - double-click friendly launcher for scripts\stop.ps1
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if not exist "scripts\stop.ps1" (
  echo Could not find scripts\stop.ps1 - run this from the repository root.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "scripts\stop.ps1" %*

echo.
echo Press any key to close this window.
pause >nul
endlocal