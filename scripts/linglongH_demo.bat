@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "ASR_ROOT=%%~fI"
set "ISAACLAB_ROOT=%ASR_ROOT%\IsaacLab"
set "URDF_PATH=%ASR_ROOT%\..\URDF-linglong-h-20260114\urdf\linglong-h.urdf"
set "USD_PATH=%ASR_ROOT%\output\linglong-h.usd"

set "DEMO_HEADLESS="
set "FORCE_REBUILD_USD="
set "CHECK_ONLY="

:parse_args
if "%~1"=="" goto :args_done
if /I "%~1"=="--headless" (
    set "DEMO_HEADLESS=1"
) else if /I "%~1"=="--rebuild-usd" (
    set "FORCE_REBUILD_USD=1"
) else if /I "%~1"=="--check-only" (
    set "CHECK_ONLY=1"
) else (
    echo [ERROR] Unknown argument: %~1
    echo [ERROR] Supported arguments: --headless --rebuild-usd --check-only
    exit /b 1
)
shift
goto :parse_args

:args_done
if not exist "%ISAACLAB_ROOT%\isaaclab.bat" (
    echo [ERROR] IsaacLab launcher not found: "%ISAACLAB_ROOT%\isaaclab.bat"
    exit /b 1
)

if not exist "%URDF_PATH%" (
    echo [ERROR] URDF file not found: "%URDF_PATH%"
    exit /b 1
)

if defined CHECK_ONLY (
    echo [INFO] ASR_ROOT: %ASR_ROOT%
    echo [INFO] ISAACLAB_ROOT: %ISAACLAB_ROOT%
    echo [INFO] URDF_PATH: %URDF_PATH%
    echo [INFO] USD_PATH: %USD_PATH%
    echo [INFO] Mode: %DEMO_HEADLESS%
    echo [INFO] Rebuild USD: %FORCE_REBUILD_USD%
    exit /b 0
)

if defined FORCE_REBUILD_USD goto :rebuild_usd
if not exist "%USD_PATH%" goto :rebuild_usd
goto :run_demo

:rebuild_usd
echo [INFO] Building Linglong-H USD from URDF...
pushd "%ISAACLAB_ROOT%"
call .\isaaclab.bat -p scripts\tools\convert_urdf.py "%URDF_PATH%" "%USD_PATH%" --fix-base --headless
set "CONVERT_EXIT=!errorlevel!"
popd
if not "%CONVERT_EXIT%"=="0" (
    echo [ERROR] URDF to USD conversion failed with exit code %CONVERT_EXIT%.
    exit /b %CONVERT_EXIT%
)

:run_demo
echo [INFO] Launching Linglong-H demo...
echo [INFO] USD_PATH: %USD_PATH%
echo [INFO] Mode: %DEMO_HEADLESS%
pushd "%ISAACLAB_ROOT%"
if defined DEMO_HEADLESS (
    call .\isaaclab.bat -p scripts\tutorials\01_assets\test_linglong_h_joints.py --headless
) else (
    call .\isaaclab.bat -p scripts\tutorials\01_assets\test_linglong_h_joints.py
)
set "DEMO_EXIT=!errorlevel!"
popd
exit /b %DEMO_EXIT%
