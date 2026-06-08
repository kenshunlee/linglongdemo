@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "ROOT_DIR=%~dp0.."
set "PYTHON_EXE=%ROOT_DIR%\.venv\Scripts\python.exe"
set "ENV_FILE=%~dp0cloud.env"
set "DEBUG_PORT=5678"
set "WAIT_FLAG=--wait-for-client"

if /I "%~1"=="--no-wait" (
  set "WAIT_FLAG="
)

if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python not found: %PYTHON_EXE%
  exit /b 1
)

if exist "%ENV_FILE%" (
  for /f "usebackq delims=" %%L in ("%ENV_FILE%") do (
    set "line=%%L"
    if not "!line!"=="" (
      if not "!line:~0,1!"=="#" (
        for /f "tokens=1,* delims==" %%K in ("!line!") do (
          if not "%%K"=="" set "%%K=%%M"
        )
      )
    )
  )
)

set "PHI3_FIRST=0"
set "LOCAL_ASR_ENABLED=0"
set "ASR_OUTPUT_DIR=%ROOT_DIR%\output"

set "DEBUGPY_MAIN="
for /f "delims=" %%D in ('dir /b /ad "%USERPROFILE%\.vscode\extensions\ms-python.debugpy-*" 2^>nul') do (
  set "DEBUGPY_MAIN=%USERPROFILE%\.vscode\extensions\%%D\bundled\libs\debugpy\__main__.py"
)

if "%DEBUGPY_MAIN%"=="" (
  echo [ERROR] VS Code debugpy extension not found under %USERPROFILE%\.vscode\extensions
  exit /b 1
)

if not exist "%DEBUGPY_MAIN%" (
  echo [ERROR] debugpy entry not found: %DEBUGPY_MAIN%
  exit /b 1
)

echo [INFO] Python   : %PYTHON_EXE%
echo [INFO] debugpy  : %DEBUGPY_MAIN%
echo [INFO] App      : backend/server.py
echo [INFO] Listen   : 127.0.0.1:%DEBUG_PORT%
if "%WAIT_FLAG%"=="" (
  echo [INFO] Start without wait-for-client
) else (
  echo [INFO] Waiting for VS Code attach...
)

"%PYTHON_EXE%" "%DEBUGPY_MAIN%" --listen 127.0.0.1:%DEBUG_PORT% %WAIT_FLAG% "%ROOT_DIR%\backend\server.py"
