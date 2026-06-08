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
echo [1/3] 安装 Python 依赖...
pip install -r requirements.txt -q

:: 检查 Ollama
echo [2/3] 检查 Ollama 服务...
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [警告] Ollama 未运行，请先启动 Ollama
    echo        执行: ollama serve
    echo        然后: ollama pull whisper
    echo.
)

:: 启动服务
echo [3/3] 启动 ASR Bridge 服务（端口 8765）...
echo        小程序请配置服务器为: http://你的电脑IP:8765
echo        按 Ctrl+C 停止服务
echo.
python server.py

pause
