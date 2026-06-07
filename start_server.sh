#!/bin/bash

echo "========================================"
echo "A股量化交易系统 - 后台服务"
echo "========================================"
echo ""

echo "[1] 检查依赖..."
pip install -r requirements.txt -q

echo ""
echo "[2] 启动后台服务..."
echo ""
echo "Web界面: http://localhost:8000"
echo "API文档: http://localhost:8000/docs"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""

python server.py