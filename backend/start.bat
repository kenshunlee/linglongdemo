@echo off
chcp 65001 > nul
echo ============================================
echo  ASR Bridge Service 启动脚本
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

:: 安装依赖
echo [1/2] 安装 Python 依赖...
pip install -r requirements.txt -q

:: 启动服务
echo [2/2] 启动 ASR Bridge 服务（端口 8765）...
echo        当前默认引擎: 智谱 GLM-ASR-2512
echo        请先配置环境变量: ZHIPU_API_KEY
echo        小程序请配置服务器为: http://你的电脑IP:8765
echo        按 Ctrl+C 停止服务
echo.
python server.py

pause
