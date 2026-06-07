@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo A股量化交易系统 - 调试模式
echo ========================================
echo.
echo 当前目录: %CD%
echo.

echo [1] 检查Python环境...
where python
python --version
echo.

echo [2] 检查依赖包...
python -c "import fastapi; print('fastapi:', fastapi.__version__)"
python -c "import uvicorn; print('uvicorn:', uvicorn.__version__)"
python -c "import akshare; print('akshare:', akshare.__version__)"
echo.

echo [3] 测试server.py导入...
python -c "import sys; sys.path.insert(0, '.'); from server import app; print('server.py导入成功')"
echo.

echo [4] 启动服务...
echo Web界面: http://localhost:8000
echo.
python server.py

echo.
echo 服务退出
pause