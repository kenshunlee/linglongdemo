@REM start-debug-attach.bat --no-wait

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
          if not "%%K"=="" set "%%K=%%L"
        )
      )
    )
  )
)

set "PHI3_FIRST=0"
set "LOCAL_ASR_ENABLED=0"
set "ASR_OUTPUT_DIR=%ROOT_DIR%\output"
set "DEBUGPY_SOURCE=.venv"

set "DEBUGPY_BUNDLED_LIBS="
for /f "delims=" %%D in ('dir /b /ad "%USERPROFILE%\.vscode\extensions\ms-python.debugpy-*" 2^>nul') do (
  set "DEBUGPY_BUNDLED_LIBS=%USERPROFILE%\.vscode\extensions\%%D\bundled\libs"
)

"%PYTHON_EXE%" -c "import debugpy" >nul 2>nul
if errorlevel 1 (
  set "DEBUGPY_SOURCE=vscode-extension"
  if "%DEBUGPY_BUNDLED_LIBS%"=="" (
    echo [ERROR] debugpy is not available in .venv and VS Code debugpy extension was not found.
    exit /b 1
  )
  if not exist "%DEBUGPY_BUNDLED_LIBS%\debugpy\__init__.py" (
    echo [ERROR] debugpy package not found: %DEBUGPY_BUNDLED_LIBS%\debugpy
    exit /b 1
  )
  set "PYTHONPATH=%DEBUGPY_BUNDLED_LIBS%;%PYTHONPATH%"
)

echo [INFO] Python   : %PYTHON_EXE%
if /I "%DEBUGPY_SOURCE%"==".venv" (
  echo [INFO] debugpy  : .venv via python -m debugpy
) else (
  echo [INFO] debugpy  : %DEBUGPY_BUNDLED_LIBS%\debugpy
)
echo [INFO] App      : backend/server.py
echo [INFO] Listen   : 127.0.0.1:%DEBUG_PORT%
if "%WAIT_FLAG%"=="" (
  echo [INFO] Start without wait-for-client
) else (
  echo [INFO] Waiting for VS Code attach...
)

"%PYTHON_EXE%" -m debugpy --listen 127.0.0.1:%DEBUG_PORT% %WAIT_FLAG% "%ROOT_DIR%\backend\server.py"
