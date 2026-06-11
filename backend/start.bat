@echo off
setlocal EnableExtensions

echo ============================================
echo  ASR Bridge Service Startup
echo ============================================

cd /d "%~dp0"

if defined PYTHON_EXE (
    set "PYTHON_CMD=%PYTHON_EXE%"
) else (
    set "PYTHON_CMD=python"
    if exist "%~dp0..\..\.venv\Scripts\python.exe" (
        set "PYTHON_CMD=%~dp0..\..\.venv\Scripts\python.exe"
    )
)

%PYTHON_CMD% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ or create .venv.
    pause
    exit /b 1
)

if "%LOCAL_ASR_ENABLED%"=="" set "LOCAL_ASR_ENABLED=1"
if "%LOCAL_ASR_MODEL_SIZE%"=="" set "LOCAL_ASR_MODEL_SIZE=small"
if "%LOCAL_ASR_DEVICE%"=="" set "LOCAL_ASR_DEVICE=auto"
if "%ASR_HOST%"=="" set "ASR_HOST=0.0.0.0"
if "%ASR_PORT%"=="" set "ASR_PORT=8765"
if "%USB_DEBUG_PREFERRED%"=="" set "USB_DEBUG_PREFERRED=1"

echo [1/3] Installing core Python dependencies...
%PYTHON_CMD% -m pip install httpx python-multipart pyyaml
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)

if "%LOCAL_ASR_ENABLED%"=="1" (
    echo [2/3] Installing optional local ASR dependency: faster-whisper...
    %PYTHON_CMD% -m pip install faster-whisper
    if errorlevel 1 (
        echo [WARN] faster-whisper installation failed, fallback to remote ASR.
        set "LOCAL_ASR_ENABLED=0"
    )
) else (
    echo [2/3] Local ASR disabled by LOCAL_ASR_ENABLED=%LOCAL_ASR_ENABLED%.
)

echo [3/3] Starting ASR Bridge on port %ASR_PORT% ...
echo         local ASR: enabled=%LOCAL_ASR_ENABLED%, model=%LOCAL_ASR_MODEL_SIZE%, device=%LOCAL_ASR_DEVICE%
echo         host=%ASR_HOST%  usb_debug_preferred=%USB_DEBUG_PREFERRED%
echo         stop with Ctrl+C
echo.
%PYTHON_CMD% server.py

pause
