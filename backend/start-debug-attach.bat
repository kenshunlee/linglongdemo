@REM start-debug-attach.bat --no-wait

@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT_DIR=%~dp0.."
set "PYTHON_EXE=%ROOT_DIR%\.venv\Scripts\python.exe"
set "PYTHON_ARGS="
set "ENV_FILE=%~dp0cloud.env"
set "DEBUG_PORT=5678"
set "WAIT_FLAG=--wait-for-client"

if not exist "%PYTHON_EXE%" (
  set "PYTHON_EXE=py"
  set "PYTHON_ARGS=-3.12"
)

if /I "%~1"=="--no-wait" (
  set "WAIT_FLAG="
)

%PYTHON_EXE% %PYTHON_ARGS% -V >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python 3.12 not found via Python Launcher.
  exit /b 1
)

if exist "%ENV_FILE%" (
  for /f "usebackq delims=" %%L in ("%ENV_FILE%") do (
    set "line=%%L"
    if not "!line!"=="" (
      if not "!line:~0,1!"=="#" (
        for /f "tokens=1,* delims==" %%A in ("!line!") do (
          if not "%%A"=="" set "%%A=%%B"
        )
      )
    )
  )
)

set "PHI3_FIRST=0"
set "LOCAL_ASR_ENABLED=0"
set "ASR_OUTPUT_DIR=%ROOT_DIR%\output"

%PYTHON_EXE% %PYTHON_ARGS% -c "import debugpy" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] debugpy is not available for Python 3.12.
  echo [ERROR] Suggestion: install debugpy into this Python environment.
  exit /b 1
)

%PYTHON_EXE% %PYTHON_ARGS% -m debugpy --version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] debugpy runtime check failed.
  echo [ERROR] Python: %PYTHON_EXE% %PYTHON_ARGS%
  echo [ERROR] Suggestion: install debugpy for Python 3.12.
  exit /b 1
)

echo [INFO] Python   : %PYTHON_EXE%
echo [INFO] debugpy  : python -m debugpy
echo [INFO] App      : backend/server.py
echo [INFO] Listen   : 127.0.0.1:%DEBUG_PORT%
if "%WAIT_FLAG%"=="" (
  echo [INFO] Start without wait-for-client
) else (
  echo [INFO] Waiting for VS Code attach...
)

%PYTHON_EXE% %PYTHON_ARGS% -m debugpy --listen 127.0.0.1:%DEBUG_PORT% %WAIT_FLAG% "%ROOT_DIR%\backend\server.py"
set "DEBUG_EXIT=%errorlevel%"
if not "%DEBUG_EXIT%"=="0" (
  echo [ERROR] debugpy exited with code %DEBUG_EXIT%.
  if "%DEBUG_EXIT%"=="-1073741819" echo [ERROR] Access violation detected while launching debugpy.
)
exit /b %DEBUG_EXIT%
