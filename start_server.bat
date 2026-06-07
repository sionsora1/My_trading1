@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo A股量化交易系统 - 后台服务
echo ========================================
echo.

echo [1] 检查Python环境...
python --version
if errorlevel 1 (
    echo [错误] Python未安装或未配置环境变量
    pause
    exit /b 1
)

echo.
echo [2] 检查依赖...
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo.
echo [3] 启动后台服务...
echo.
echo ========================================
echo Web界面: http://localhost:8000
echo API文档: http://localhost:8000/docs
echo ========================================
echo.
echo 按 Ctrl+C 停止服务
echo.

python server.py

echo.
echo 服务已停止
pause