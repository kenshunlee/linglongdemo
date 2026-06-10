@echo off
setlocal

cd /d "%~dp0"

set FRAMEWORK=%~1
if "%FRAMEWORK%"=="" set FRAMEWORK=none

echo Installing Isaac Lab with framework: %FRAMEWORK%
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\install-isaac-lab.ps1" -Framework %FRAMEWORK%
set ERR=%ERRORLEVEL%

if not "%ERR%"=="0" (
  echo.
  echo Isaac Lab installation failed. Exit code: %ERR%
  pause
  exit /b %ERR%
)

echo.
echo Isaac Lab installation succeeded.
endlocal
