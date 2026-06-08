@echo off
chcp 65001 > nul
echo ============================================
echo  ASR Bridge Service 启动脚本（本地GPU优先）
echo ============================================

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

:: 切换到脚本目录
cd /d "%~dp0"

:: 默认启用本地 ASR（faster-whisper）
if "%LOCAL_ASR_ENABLED%"=="" set LOCAL_ASR_ENABLED=1
if "%LOCAL_ASR_MODEL_SIZE%"=="" set LOCAL_ASR_MODEL_SIZE=small
if "%LOCAL_ASR_DEVICE%"=="" set LOCAL_ASR_DEVICE=auto
if "%ASR_HOST%"=="" set ASR_HOST=0.0.0.0
if "%ASR_PORT%"=="" set ASR_PORT=8765
if "%USB_DEBUG_PREFERRED%"=="" set USB_DEBUG_PREFERRED=1

:: 安装依赖
echo [1/2] 安装 Python 依赖...
pip install -r requirements.txt -q

:: 启动服务
echo [2/2] 启动 ASR Bridge 服务（端口 %ASR_PORT%）...
echo        本地 ASR: enabled=%LOCAL_ASR_ENABLED%, model=%LOCAL_ASR_MODEL_SIZE%, device=%LOCAL_ASR_DEVICE%
echo        USB 调试优先: %USB_DEBUG_PREFERRED%, 监听地址优先使用可达 IPv4
echo        远程兜底: GLM-ASR-2512（配置 ZHIPU_API_KEY 后可用）
echo        小程序 LAN 调试:  http://你的电脑局域网IP:%ASR_PORT%
echo        小程序 USB 调试:  http://USB网段电脑IP:%ASR_PORT%（非 127.0.0.1）
echo        若手机连不上，请检查 Windows 防火墙 8765 入站规则
echo        按 Ctrl+C 停止服务
echo.
python server.py

pause
