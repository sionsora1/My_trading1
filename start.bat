@echo off
echo ========================================
echo A股量化交易系统 - 启动脚本
echo ========================================
echo.

echo [1] 检查依赖...
pip install -r requirements.txt -q

echo.
echo [2] 启动可视化界面...
echo 浏览器将自动打开 http://localhost:8501
echo 按 Ctrl+C 停止服务
echo.

streamlit run app.py

pause