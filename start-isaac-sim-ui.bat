@echo off
setlocal

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\start-isaac-sim-safe.ps1" -Mode full -ResetUser -NoRosEnv
set ERR=%ERRORLEVEL%

if not "%ERR%"=="0" (
  echo.
  echo Isaac Sim failed to start. Exit code: %ERR%
  pause
)

endlocal
