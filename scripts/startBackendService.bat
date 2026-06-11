@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "ASR_ROOT=%%~fI"
set "BACKEND_ROOT=%ASR_ROOT%\backend"
set "BACKEND_START_BAT=%BACKEND_ROOT%\start.bat"
set "PYTHON_EXE=py -3.12"

if not exist "%BACKEND_START_BAT%" (
    echo [ERROR] Backend startup script not found: "%BACKEND_START_BAT%"
    exit /b 1
)

py -3.12 -V >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.12 not found via Python Launcher: %PYTHON_EXE%
    exit /b 1
)

if "%LOCAL_ASR_ENABLED%"=="" set "LOCAL_ASR_ENABLED=0"

echo [INFO] Launching backend service from "%BACKEND_ROOT%" ...
echo [INFO] Python 3.12  : %PYTHON_EXE%
echo [INFO] LOCAL_ASR_ENABLED=%LOCAL_ASR_ENABLED%
pushd "%BACKEND_ROOT%"
call .\start.bat
set "BACKEND_EXIT=%errorlevel%"
popd
exit /b %BACKEND_EXIT%
