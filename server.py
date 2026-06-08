"""
A股量化交易系统 - 后端服务
基于FastAPI，提供REST API接口和Web页面
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict
import uvicorn
import json
import uuid
import asyncio
from datetime import datetime
from pathlib import Path

from backtest.engine import BacktestEngine, BacktestConfig
from strategy import get_strategy, get_all_strategies, STRATEGY_REGISTRY
from analysis.market_regime import MarketRegimeDetector, StrategyRegimeAdapter, MarketRegime
from analysis.ai_analyzer import analyze_fed_event, AIAnalyzer
from data.fetcher import DataFetcher, DataCache
from config.settings import BACKTEST_CONFIG, LIVE_TRADING_CONFIG

# 获取当前目录
BASE_DIR = Path(__file__).parent


# ============================================================
# FastAPI应用
# ============================================================

app = FastAPI(
    title="A股量化交易系统 API",
    description="量化交易策略回测和分析接口",
    version="1.0.0"
)

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局状态
tasks = {}  # 存储后台任务
results = {}  # 存储回测结果
data_fetcher = DataFetcher()
data_cache = DataCache()


# ============================================================
# 数据模型
# ============================================================

class BacktestRequest(BaseModel):
    """回测请求"""
    stock_pool: List[str]
    start_date: str
    end_date: str
    strategy_type: str = "eight_factor"  # eight_factor / position / both
    initial_capital: float = 1000000
    max_position: int = 20
    stop_loss: float = -0.08
    move_stop: float = -0.10
    rebalance_frequency: str = "weekly"

class TaskStatus(BaseModel):
    """任务状态"""
    task_id: str
    status: str  # pending / running / completed / failed
    progress: float
    message: str
    created_at: str
    completed_at: Optional[str] = None


# ============================================================
# API接口
# ============================================================

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def root():
    """返回主页（导航枢纽）"""
    html_file = BASE_DIR / "web" / "index.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    # 回退到旧版首页
    html_file = BASE_DIR / "web" / "app.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>请先创建 web/index.html 文件</h1>")

@app.get("/app.html", response_class=HTMLResponse)
async def app_page():
    """返回回测分析页面"""
    html_file = BASE_DIR / "web" / "app.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>请先创建 web/app.html 文件</h1>")

@app.get("/vis.html", response_class=HTMLResponse)
async def vis_page():
    """返回 Streamlit 可视化入口说明页"""
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>可视化回测 - A股量化交易系统</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        body { background: #f0f2f5; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
        .container { max-width: 600px; margin: 80px auto; text-align: center; }
        .card { border: none; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); padding: 40px; }
        .btn-launch { padding: 14px 40px; font-size: 1.1rem; border-radius: 10px; margin: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h2 style="font-size:2rem;margin-bottom:8px;">📊 Streamlit 可视化回测</h2>
            <p class="text-muted mb-4">独立 Streamlit 应用，提供交互式净值曲线、回撤分析和每日操作统计</p>
            <div id="status-area" class="mb-3">
                <div class="spinner-border spinner-border-sm text-secondary"></div>
                <span class="ms-2 text-muted">检测 Streamlit 服务状态...</span>
            </div>
            <a href="http://localhost:8501" class="btn btn-primary btn-launch" id="btn-streamlit" target="_blank">
                <i class="bi bi-box-arrow-up-right"></i> 打开 Streamlit 界面
            </a>
            <br>
            <a href="/index.html" class="btn btn-outline-secondary btn-launch">
                <i class="bi bi-house-door"></i> 返回主页
            </a>
            <p class="text-muted mt-4" style="font-size:0.85rem;">
                如果上面按钮不能打开，请在终端运行：<br>
                <code>streamlit run app.py --server.port 8501</code>
            </p>
        </div>
    </div>
    <script>
        fetch('http://localhost:8501/healthz', {mode:'no-cors'})
            .then(() => {
                document.getElementById('status-area').innerHTML = '<span style="color:#059669;">✅ Streamlit 服务运行中</span>';
            })
            .catch(() => {
                document.getElementById('status-area').innerHTML = '<span style="color:#ef4444;">❌ Streamlit 服务未启动</span>';
            });
    </script>
</body>
</html>""")

@app.get("/live.html", response_class=HTMLResponse)
async def live_page():
    """返回实盘交易页面"""
    html_file = BASE_DIR / "web" / "live.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>请先创建 web/live.html 文件</h1>")

@app.get("/mobile.html", response_class=HTMLResponse)
async def mobile_page():
    """返回移动端信号页面"""
    html_file = BASE_DIR / "web" / "mobile.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>请先创建 web/mobile.html 文件</h1>")

@app.get("/api")
async def api_info():
    """API信息"""
    return {
        "name": "A股量化交易系统 API",
        "version": "2.0.0",
        "status": "running",
        "strategies": list(STRATEGY_REGISTRY.keys()),
        "endpoints": {
            "docs": "/docs",
            "backtest": "/api/backtest",
            "tasks": "/api/tasks",
            "results": "/api/results",
            "stocks": "/api/stocks",
            "strategies": "/api/strategies",
            "regime": "/api/analyze/regime",
            "event": "/api/analyze/event"
        }
    }


class EventAnalysisRequest(BaseModel):
    """事件分析请求"""
    event: str
    related_stocks: Optional[List[str]] = None


@app.post("/api/analyze/regime")
async def analyze_market_regime(stock_pool: List[str], start_date: str, end_date: str):
    """分析市场环境"""
    try:
        # 获取数据
        cache_filename = f'market_data_{start_date}_{end_date}_{len(stock_pool)}stocks'
        market_data = data_cache.load_market_data(cache_filename)

        if not market_data or not isinstance(market_data, dict) or len(market_data) < 10:
            market_data = data_fetcher.build_market_data_by_date(stock_pool, start_date, end_date)
            if market_data:
                data_cache.save_market_data(market_data, cache_filename)

        if not market_data or len(market_data) == 0:
            return {"status": "error", "message": "获取数据失败"}

        # 取最新一天的数据
        latest_date = sorted(market_data.keys())[-1]
        latest_data = market_data[latest_date]

        # 分析市场环境
        detector = MarketRegimeDetector()
        analysis = detector.detect(latest_data)

        # 获取推荐策略的评分
        strategy_scores = {}
        for strategy_name in STRATEGY_REGISTRY:
            score = StrategyRegimeAdapter.get_strategy_score(strategy_name, analysis.regime)
            strategy_scores[strategy_name] = score

        # 排序推荐策略
        sorted_strategies = sorted(strategy_scores.items(), key=lambda x: x[1], reverse=True)

        return {
            "status": "success",
            "data": {
                "regime": analysis.regime.value,
                "confidence": analysis.confidence,
                "description": analysis.description,
                "indicators": analysis.indicators,
                "risk_level": analysis.risk_level,
                "position_advice": analysis.position_advice,
                "recommended_strategies": analysis.recommended_strategies,
                "strategy_scores": dict(sorted_strategies),
                "analysis_date": latest_date
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/analyze/event")
async def analyze_event(request: EventAnalysisRequest):
    """分析事件影响"""
    try:
        # 使用预设分析
        if '非农' in request.event or '美联储' in request.event or '加息' in request.event:
            result = analyze_fed_event('non_farm')
        else:
            # 使用AI分析（如果有配置）
            try:
                analyzer = AIAnalyzer(provider='deepseek', api_key='')
                result = analyzer.analyze_event_impact(request.event, request.related_stocks)
            except:
                # 降级到预设分析
                result = analyze_fed_event('non_farm')

        return {
            "status": "success",
            "data": {
                "market_sentiment": result.market_sentiment,
                "key_events": result.key_events,
                "impact_analysis": result.impact_analysis,
                "strategy_suggestion": result.strategy_suggestion,
                "risk_warning": result.risk_warning,
                "confidence": result.confidence
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/strategies")
async def get_strategies():
    """获取所有可用策略"""
    return {
        "status": "success",
        "data": get_all_strategies()
    }


@app.get("/api/stocks")
async def get_stock_list():
    """获取股票列表"""
    try:
        stock_list = data_fetcher.get_stock_list()
        return {
            "status": "success",
            "data": stock_list.to_dict('records') if not stock_list.empty else []
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stocks/{code}")
async def get_stock_info(code: str):
    """获取股票信息"""
    try:
        info = data_fetcher.get_stock_info(code)
        return {
            "status": "success",
            "data": info
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/backtest")
async def run_backtest(request: BacktestRequest, background_tasks: BackgroundTasks):
    """运行回测（异步）"""
    task_id = str(uuid.uuid4())[:8]

    # 创建任务
    tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "message": "任务已创建，等待执行",
        "created_at": datetime.now().isoformat(),
        "request": request.dict()
    }

    # 添加后台任务
    background_tasks.add_task(execute_backtest, task_id, request)

    return {
        "status": "success",
        "task_id": task_id,
        "message": "回测任务已提交，请通过 /api/tasks/{task_id} 查询进度"
    }


@app.get("/api/tasks")
async def get_all_tasks():
    """获取所有任务"""
    return {
        "status": "success",
        "data": list(tasks.values())
    }


@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    """获取任务状态"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "status": "success",
        "data": tasks[task_id]
    }


@app.get("/api/results/{task_id}")
async def get_backtest_result(task_id: str):
    """获取回测结果"""
    if task_id not in results:
        raise HTTPException(status_code=404, detail="结果不存在")

    return {
        "status": "success",
        "data": results[task_id]
    }


@app.get("/api/results/{task_id}/daily")
async def get_daily_operations(task_id: str, strategy: str = "eight_factor", date: Optional[str] = None):
    """获取每日操作详情"""
    print(f"[DEBUG] daily endpoint called: task_id={task_id}, strategy={strategy}, date={date}")
    print(f"[DEBUG] results keys: {list(results.keys())}")

    if task_id not in results:
        raise HTTPException(status_code=404, detail="结果不存在")

    result = results[task_id]
    print(f"[DEBUG] result keys: {list(result.keys())}")

    # 从strategy_results获取数据
    strategy_results = result.get("strategy_results", {})
    print(f"[DEBUG] strategy_results keys: {list(strategy_results.keys())}")
    print(f"[DEBUG] strategy '{strategy}' in strategy_results: {strategy in strategy_results}")

    # 选择策略
    if strategy not in strategy_results:
        # 默认使用第一个策略
        strategy = list(strategy_results.keys())[0] if strategy_results else None

    if not strategy or strategy not in strategy_results:
        print(f"[DEBUG] strategy not found, returning empty")
        return {"status": "success", "data": [] if not date else None}

    daily_ops = strategy_results[strategy].get("daily_operations", [])
    print(f"[DEBUG] daily_ops length: {len(daily_ops)}")

    if date:
        # 返回指定日期的操作
        for op in daily_ops:
            if op.get("date") == date:
                return {"status": "success", "data": op}
        raise HTTPException(status_code=404, detail=f"日期 {date} 不存在")

    # 返回所有日期列表
    dates = [op["date"] for op in daily_ops]
    print(f"[DEBUG] returning dates: {dates}")
    return {"status": "success", "data": dates}


@app.get("/api/results/{task_id}/chart")
async def get_chart_data(task_id: str, strategy: str = "eight_factor"):
    """获取图表数据"""
    if task_id not in results:
        raise HTTPException(status_code=404, detail="结果不存在")

    result = results[task_id]

    # 从strategy_results获取数据
    strategy_results = result.get("strategy_results", {})

    # 选择策略
    if strategy not in strategy_results:
        strategy = list(strategy_results.keys())[0] if strategy_results else None

    if not strategy or strategy not in strategy_results:
        return {"status": "success", "data": {"dates": [], "total_value": [], "daily_return": [], "cumulative_return": [], "position_count": []}}

    daily_nav = strategy_results[strategy].get("daily_nav", [])

    # 转换为图表格式
    chart_data = {
        "dates": [nav["date"] for nav in daily_nav],
        "total_value": [nav["total_value"] for nav in daily_nav],
        "daily_return": [nav.get("daily_return", 0) for nav in daily_nav],
        "cumulative_return": [nav["total_return"] for nav in daily_nav],
        "position_count": [nav["position_count"] for nav in daily_nav]
    }

    return {"status": "success", "data": chart_data}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除任务"""
    if task_id in tasks:
        del tasks[task_id]
    if task_id in results:
        del results[task_id]

    return {"status": "success", "message": "任务已删除"}


# ============================================================
# 股票池导入导出
# ============================================================

class StockPoolImport(BaseModel):
    """股票池导入"""
    name: Optional[str] = "导入的股票池"
    codes: List[str]


@app.get("/api/pool/export")
async def export_stock_pool(codes: str):
    """导出股票池（codes为逗号分隔的股票代码）"""
    try:
        code_list = [c.strip() for c in codes.split(',') if c.strip()]
        if not code_list:
            return {"status": "error", "message": "股票代码不能为空"}

        # 获取每只股票的名称
        stock_details = []
        for code in code_list:
            if not code.isdigit() or len(code) != 6:
                continue
            try:
                info = data_fetcher.get_stock_info(code)
                stock_details.append({
                    "code": code,
                    "name": info.get('name', code),
                    "industry": info.get('industry', '未知')
                })
            except:
                stock_details.append({"code": code, "name": code, "industry": "未知"})

        return {
            "status": "success",
            "data": {
                "name": "股票池导出",
                "export_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "count": len(stock_details),
                "stocks": stock_details,
                "codes": [s["code"] for s in stock_details]
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/pool/import")
async def import_stock_pool(pool: StockPoolImport):
    """导入股票池"""
    try:
        valid_codes = []
        invalid_codes = []

        for code in pool.codes:
            code = code.strip()
            if code.isdigit() and len(code) == 6:
                valid_codes.append(code)
            else:
                invalid_codes.append(code)

        if not valid_codes:
            return {"status": "error", "message": "没有有效的股票代码"}

        # 获取股票信息
        stock_details = []
        for code in valid_codes:
            try:
                info = data_fetcher.get_stock_info(code)
                stock_details.append({
                    "code": code,
                    "name": info.get('name', code),
                    "industry": info.get('industry', '未知')
                })
            except:
                stock_details.append({"code": code, "name": code, "industry": "未知"})

        return {
            "status": "success",
            "data": {
                "name": pool.name,
                "count": len(stock_details),
                "stocks": stock_details,
                "codes": valid_codes,
                "invalid_codes": invalid_codes
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# 股票池跨面板同步
# ============================================================

@app.get("/api/pool/sync-from-live")
async def sync_pool_from_live():
    """从实盘股票池读取代码（供回测面板同步用）"""
    pool = _load_stock_pool()
    codes = [s['code'] for s in pool]
    names = {s['code']: s.get('name', '') for s in pool}
    return {
        "status": "success",
        "data": {
            "codes": codes,
            "names": names,
            "count": len(codes),
            "source": "实盘股票池",
            "stocks": pool,
        }
    }


class SyncPoolToLiveRequest(BaseModel):
    """同步到实盘股票池"""
    codes: List[str]


@app.post("/api/pool/sync-to-live")
async def sync_pool_to_live(req: SyncPoolToLiveRequest):
    """将回测面板的股票池同步到实盘"""
    valid_codes = [c.strip() for c in req.codes if c.strip().isdigit() and len(c.strip()) == 6]
    if not valid_codes:
        return {"status": "error", "message": "没有有效的股票代码"}

    # 查询股票名称
    result = []
    for code in valid_codes:
        name = ''
        industry = ''
        try:
            info = data_fetcher.get_stock_info(code)
            name = info.get('name', code)
            industry = info.get('industry', '未知')
        except Exception:
            name = code
            industry = '未知'
        result.append({'code': code, 'name': name, 'industry': industry})

    _save_stock_pool(result)
    _sync_stock_pool_to_config()

    return {
        "status": "success",
        "data": {"stocks": result, "count": len(result)},
        "message": f"已同步 {len(result)} 只股票到实盘股票池"
    }


# ============================================================
# 次日操作建议
# ============================================================

class SuggestionRequest(BaseModel):
    """次日建议请求"""
    stock_pool: List[str]
    strategy_type: str = "eight_factor"
    positions: Optional[Dict[str, dict]] = None  # 当前持仓 {code: {cost_price, quantity}}


@app.post("/api/suggestion")
async def get_suggestion(request: SuggestionRequest):
    """获取次日操作建议"""
    try:
        # 1. 获取最近30个交易日数据
        from datetime import datetime, timedelta
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')

        cache_filename = f'market_data_{start_date}_{end_date}_{len(request.stock_pool)}stocks'
        market_data = data_cache.load_market_data(cache_filename)

        if not market_data or not isinstance(market_data, dict) or len(market_data) < 10:
            market_data = data_fetcher.build_market_data_by_date(
                request.stock_pool, start_date, end_date
            )
            if market_data:
                data_cache.save_market_data(market_data, cache_filename)

        if not market_data or len(market_data) == 0:
            return {"status": "error", "message": "获取行情数据失败，请检查股票代码或网络"}

        # 2. 取最新一天的数据
        latest_date = sorted(market_data.keys())[-1]
        latest_data = market_data[latest_date]

        # 3. 构造 portfolio
        portfolio = {'cash': 1_000_000, 'positions': {}}
        if request.positions:
            for code, pos in request.positions.items():
                if code in latest_data:
                    stock = latest_data[code]
                    portfolio['positions'][code] = {
                        'ts_code': code,
                        'quantity': pos.get('quantity', 100),
                        'cost_price': pos.get('cost_price', stock.get('close', 0)),
                        'current_price': stock.get('close', 0),
                        'profit_rate': 0,
                        'highest_price': stock.get('close', 0),
                    }

        # 4. 运行策略生成信号
        if request.strategy_type == 'both':
            strategies_to_run = ['eight_factor', 'position']
        else:
            strategies_to_run = [request.strategy_type]

        all_suggestions = {}
        for strategy_name in strategies_to_run:
            try:
                strategy = get_strategy(strategy_name)
                signals = strategy.generate_signals(latest_date, latest_data, portfolio)

                suggestions = []
                for sig in signals:
                    code = sig['ts_code']
                    stock = latest_data.get(code, {})
                    suggestions.append({
                        'code': code,
                        'name': stock.get('name', code),
                        'signal': sig['signal'],
                        'weight': sig.get('weight', 0),
                        'reason': sig.get('reason', ''),
                        'price': stock.get('close', 0),
                        'change_pct': stock.get('pct_chg', 0),
                        'ma5': stock.get('ma5', 0),
                        'ma20': stock.get('ma20', 0),
                        'volume': stock.get('volume', 0),
                    })

                # 没有信号的股票标记为持有
                signaled_codes = set(s['code'] for s in suggestions)
                for code in request.stock_pool:
                    if code not in signaled_codes and code in latest_data:
                        stock = latest_data[code]
                        suggestions.append({
                            'code': code,
                            'name': stock.get('name', code),
                            'signal': 'HOLD',
                            'weight': 0,
                            'reason': '无明确信号，建议持有观望',
                            'price': stock.get('close', 0),
                            'change_pct': stock.get('pct_chg', 0),
                            'ma5': stock.get('ma5', 0),
                            'ma20': stock.get('ma20', 0),
                            'volume': stock.get('volume', 0),
                        })

                strategy_label = STRATEGY_REGISTRY.get(strategy_name, {}).get('name', strategy_name)
                all_suggestions[strategy_name] = {
                    'strategy_name': strategy_label,
                    'date': latest_date,
                    'suggestions': suggestions
                }
            except Exception as e:
                all_suggestions[strategy_name] = {
                    'strategy_name': strategy_name,
                    'error': str(e),
                    'suggestions': []
                }

        return {
            "status": "success",
            "data": {
                "analysis_date": latest_date,
                "strategies": all_suggestions
            }
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
# 后台任务执行
# ============================================================

def execute_backtest(task_id: str, request: BacktestRequest):
    """执行回测（后台任务）"""
    try:
        # 更新状态
        tasks[task_id]["status"] = "running"
        tasks[task_id]["message"] = "正在获取数据..."
        tasks[task_id]["progress"] = 0.1

        # 获取数据
        cache_filename = f'market_data_{request.start_date}_{request.end_date}_{len(request.stock_pool)}stocks'
        market_data = data_cache.load_market_data(cache_filename)

        if not market_data or not isinstance(market_data, dict) or len(market_data) < 50:
            tasks[task_id]["message"] = "正在从AKShare获取数据..."
            market_data = data_fetcher.build_market_data_by_date(
                request.stock_pool,
                request.start_date,
                request.end_date
            )
            if market_data:
                data_cache.save_market_data(market_data, cache_filename)

        if not market_data or len(market_data) == 0:
            tasks[task_id]["status"] = "failed"
            tasks[task_id]["message"] = "获取数据失败"
            return

        tasks[task_id]["progress"] = 0.4
        tasks[task_id]["message"] = "正在运行策略回测..."

        # 配置
        config = BacktestConfig(
            initial_capital=request.initial_capital,
            max_position_num=request.max_position,
            stop_loss_rate=request.stop_loss,
            move_stop_rate=request.move_stop,
            rebalance_frequency=request.rebalance_frequency
        )

        # 运行策略
        strategy_results = {}

        # 确定要运行的策略列表
        strategies_to_run = []
        if request.strategy_type == "both":
            strategies_to_run = ["eight_factor", "position"]
        elif request.strategy_type in STRATEGY_REGISTRY:
            strategies_to_run = [request.strategy_type]
        else:
            strategies_to_run = ["eight_factor"]  # 默认

        for idx, strategy_name in enumerate(strategies_to_run):
            tasks[task_id]["progress"] = 0.4 + 0.4 * (idx / len(strategies_to_run))
            tasks[task_id]["message"] = f"正在运行{STRATEGY_REGISTRY[strategy_name]['name']}..."

            engine = BacktestEngine(config)
            strategy = get_strategy(strategy_name)
            result = engine.run(market_data, strategy, print_report=False)
            strategy_results[strategy_name] = {
                "metrics": result["metrics"],
                "daily_nav": engine.daily_nav,
                "trade_records": [
                    {
                        "order_id": t.order_id,
                        "ts_code": t.ts_code,
                        "side": t.side,
                        "price": t.price,
                        "quantity": t.quantity,
                        "amount": t.amount,
                        "commission": t.commission,
                        "trade_date": t.trade_date,
                        "reason": t.reason
                    } for t in engine.trade_records
                ],
                "daily_operations": [
                    {
                        "date": op.date,
                        "buys": op.buys,
                        "sells": op.sells,
                        "holds": op.holds,
                        "portfolio_value": op.portfolio_value,
                        "cash": op.cash,
                        "position_count": op.position_count,
                        "daily_return": op.daily_return,
                        "cumulative_return": op.cumulative_return
                    } for op in engine.daily_operations
                ],
                "final_portfolio": engine.get_portfolio()
            }

        tasks[task_id]["progress"] = 0.9
        tasks[task_id]["message"] = "正在生成报告..."

        # 保存结果
        results[task_id] = {
            "task_id": task_id,
            "request": request.dict(),
            "strategy_results": strategy_results,
            "market_data_count": len(market_data),
            "created_at": tasks[task_id]["created_at"],
            "completed_at": datetime.now().isoformat()
        }

        # 更新任务状态
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["progress"] = 1.0
        tasks[task_id]["message"] = "回测完成"
        tasks[task_id]["completed_at"] = datetime.now().isoformat()

    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["message"] = f"回测失败: {str(e)}"


# ============================================================
# 实盘交易 API
# ============================================================

# 初始化实盘服务（懒加载）
_live_server = None

def get_live_server():
    """获取实盘服务实例（懒加载单例）"""
    global _live_server
    if _live_server is None:
        from live_server import LiveTradingServer
        from config.settings import LIVE_TRADING_CONFIG
        _live_server = LiveTradingServer(LIVE_TRADING_CONFIG)
    return _live_server


class LiveOrderRequest(BaseModel):
    """手动下单请求"""
    ts_code: str
    side: str  # BUY or SELL
    quantity: int
    price: float = 0
    reason: str = ''


class SignalConfirmRequest(BaseModel):
    """信号确认请求"""
    ts_code: str
    strategy: str
    signal: str  # BUY or SELL
    confirmed: bool = True


class LiveConfigUpdate(BaseModel):
    """实盘配置更新"""
    broker: Optional[str] = None
    mode: Optional[str] = None
    stock_pool: Optional[List[str]] = None
    strategy: Optional[str] = None
    interval_seconds: Optional[int] = None


@app.get("/api/live/status")
async def live_status():
    """获取实盘服务状态"""
    server = get_live_server()
    return {"status": "success", "data": server.get_status()}


@app.get("/api/live/account")
async def live_account():
    """获取实盘账户信息"""
    server = get_live_server()
    return {"status": "success", "data": server.get_account()}


@app.get("/api/live/positions")
async def live_positions():
    """获取当前持仓"""
    server = get_live_server()
    return {"status": "success", "data": server.get_positions()}


@app.get("/api/live/orders")
async def live_orders(status: Optional[str] = None, limit: int = 50):
    """获取订单列表"""
    server = get_live_server()
    return {"status": "success", "data": server.get_orders(status, limit)}


@app.post("/api/live/order")
async def live_submit_order(request: LiveOrderRequest):
    """提交订单（手动下单）"""
    server = get_live_server()
    result = server.submit_order(
        ts_code=request.ts_code,
        side=request.side,
        quantity=request.quantity,
        price=request.price,
        reason=request.reason or '手动下单'
    )
    return {"status": "success" if result.get('success') else "error", "data": result}


@app.post("/api/live/order/{order_id}/cancel")
async def live_cancel_order(order_id: str):
    """撤销订单"""
    server = get_live_server()
    result = server.cancel_order(order_id)
    return {"status": "success" if result.get('success') else "error", "data": result}


@app.get("/api/live/signals")
async def live_signals():
    """获取当前信号"""
    server = get_live_server()
    return {"status": "success", "data": server.get_signals()}


@app.get("/api/live/signals/history")
async def live_signal_history(limit: int = 100):
    """获取信号历史"""
    server = get_live_server()
    history = server.get_signal_history()
    return {"status": "success", "data": history[:limit]}


@app.post("/api/live/signal/confirm")
async def live_confirm_signal(request: SignalConfirmRequest):
    """确认/拒绝信号"""
    server = get_live_server()
    result = server.confirm_signal(
        ts_code=request.ts_code,
        strategy=request.strategy,
        signal_type=request.signal,
        confirmed=request.confirmed
    )
    return {"status": "success" if result.get('success') else "error", "data": result}


@app.post("/api/live/scan")
async def live_scan():
    """手动触发一次策略扫描"""
    server = get_live_server()
    result = server.scan_and_trade()
    return {"status": "success", "data": result}


@app.post("/api/live/start")
async def live_start(background_tasks: BackgroundTasks,
                     broker: Optional[str] = None,
                     mode: Optional[str] = None):
    """启动实盘交易服务（可指定券商和模式）"""
    global _live_server

    # 如果指定了不同券商/模式，重建服务实例
    if broker or mode:
        if _live_server and _live_server.running:
            _live_server.stop()

        from live_server import LiveTradingServer
        from config.settings import LIVE_TRADING_CONFIG
        import copy
        config = copy.deepcopy(LIVE_TRADING_CONFIG)
        if broker:
            config['broker'] = broker
        if mode:
            config['mode'] = mode
        _live_server = LiveTradingServer(config)

    server = get_live_server()
    result = server.start(background=True)

    # 同步更新实时行情
    async def update_prices_periodically():
        while server.running:
            await asyncio.sleep(30)
            server.update_market_prices()

    if result.get('status') == 'started':
        background_tasks.add_task(update_prices_periodically)

    return {"status": "success", "data": result}


@app.post("/api/live/stop")
async def live_stop():
    """停止实盘交易服务"""
    server = get_live_server()
    result = server.stop()
    return {"status": "success", "data": result}


@app.post("/api/live/reset")
async def live_reset():
    """重置模拟账户"""
    server = get_live_server()
    if hasattr(server.broker, 'reset_account'):
        server.broker.reset_account()
        server.risk_manager.reset_daily_state()
        return {"status": "success", "data": {"message": "账户已重置"}}
    return {"status": "error", "data": {"message": "当前券商不支持重置"}}


# ============================================================
# 股票池管理 API
# ============================================================

# 股票池持久化文件
STOCK_POOL_FILE = BASE_DIR / "data_cache" / "live_stock_pool.json"

def _load_stock_pool() -> List[dict]:
    """从文件加载股票池"""
    if STOCK_POOL_FILE.exists():
        try:
            with open(STOCK_POOL_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    # 从配置读取默认值
    default_codes = LIVE_TRADING_CONFIG.get('scan', {}).get('stock_pool', [])
    return [{'code': c, 'name': '', 'industry': ''} for c in default_codes]

def _save_stock_pool(pool: List[dict]):
    """保存股票池到文件"""
    STOCK_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STOCK_POOL_FILE, 'w', encoding='utf-8') as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)

def _sync_stock_pool_to_config():
    """同步股票池代码到 live_server 配置"""
    pool = _load_stock_pool()
    codes = [s['code'] for s in pool]
    server = get_live_server()
    if 'scan' in server.config:
        server.config['scan']['stock_pool'] = codes


class StockPoolItem(BaseModel):
    """股票池条目"""
    code: str
    name: str = ''
    industry: str = ''


@app.get("/api/live/pool")
async def get_stock_pool(refresh: bool = False):
    """获取实盘股票池"""
    pool = _load_stock_pool()

    # 刷新股票名称和行业
    if refresh:
        for item in pool:
            if not item.get('name') or not item.get('industry'):
                try:
                    info = data_fetcher.get_stock_info(item['code'])
                    item['name'] = info.get('name', item['code'])
                    item['industry'] = info.get('industry', '未知')
                except Exception:
                    if not item.get('name'):
                        item['name'] = item['code']
                    if not item.get('industry'):
                        item['industry'] = '未知'
        _save_stock_pool(pool)

    return {
        "status": "success",
        "data": {
            "stocks": pool,
            "count": len(pool),
            "codes": [s['code'] for s in pool],
        }
    }


@app.post("/api/live/pool/add")
async def add_stock_to_pool(item: StockPoolItem):
    """添加股票到池"""
    code = item.code.strip()
    if not code.isdigit() or len(code) != 6:
        return {"status": "error", "message": f"无效的股票代码: {code}"}

    pool = _load_stock_pool()
    existing_codes = [s['code'] for s in pool]
    if code in existing_codes:
        return {"status": "error", "message": f"股票 {code} 已在池中"}

    # 获取名称
    name = item.name
    industry = item.industry
    if not name:
        try:
            info = data_fetcher.get_stock_info(code)
            name = info.get('name', code)
            industry = info.get('industry', '未知')
        except Exception:
            name = code
            industry = '未知'

    pool.append({'code': code, 'name': name, 'industry': industry})
    _save_stock_pool(pool)
    _sync_stock_pool_to_config()

    return {
        "status": "success",
        "data": {"code": code, "name": name, "industry": industry},
        "message": f"已添加 {code} {name}"
    }


@app.delete("/api/live/pool/{code}")
async def remove_stock_from_pool(code: str):
    """从池中移除股票"""
    pool = _load_stock_pool()
    before = len(pool)
    pool = [s for s in pool if s['code'] != code]
    after = len(pool)

    if before == after:
        return {"status": "error", "message": f"股票 {code} 不在池中"}

    _save_stock_pool(pool)
    _sync_stock_pool_to_config()

    return {"status": "success", "message": f"已移除 {code}"}


@app.post("/api/live/pool/import")
async def import_stock_pool(pool: List[StockPoolItem]):
    """批量导入股票池（替换现有池）"""
    result = []
    skipped = []
    for item in pool:
        code = item.code.strip()
        if not code.isdigit() or len(code) != 6:
            skipped.append(code)
            continue
        name = item.name
        industry = item.industry
        if not name:
            try:
                info = data_fetcher.get_stock_info(code)
                name = info.get('name', code)
                industry = info.get('industry', '未知')
            except Exception:
                name = code
                industry = '未知'
        result.append({'code': code, 'name': name, 'industry': industry})

    if not result:
        return {"status": "error", "message": "没有有效的股票代码"}

    _save_stock_pool(result)
    _sync_stock_pool_to_config()

    return {
        "status": "success",
        "data": {"stocks": result, "count": len(result)},
        "message": f"已导入 {len(result)} 只股票" + (f"，跳过 {len(skipped)} 个无效代码" if skipped else "")
    }


class StockPoolTextImport(BaseModel):
    """文本导入"""
    content: str = ''  # 每行一个代码，或 代码,名称,行业


@app.post("/api/live/pool/import-text")
async def import_stock_pool_text(req: StockPoolTextImport):
    """从文本导入股票池（每行一个代码或 代码,名称）"""
    lines = req.content.strip().split('\n')
    result = []
    skipped = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',')]
        code = parts[0]
        if not code.isdigit() or len(code) != 6:
            skipped.append(code)
            continue
        name = parts[1] if len(parts) > 1 else ''
        industry = parts[2] if len(parts) > 2 else ''
        if not name:
            try:
                info = data_fetcher.get_stock_info(code)
                name = info.get('name', code)
                industry = info.get('industry', '未知')
            except Exception:
                name = code
                industry = '未知'
        result.append({'code': code, 'name': name, 'industry': industry})

    if not result:
        return {"status": "error", "message": "没有有效的股票代码"}

    _save_stock_pool(result)
    _sync_stock_pool_to_config()

    return {
        "status": "success",
        "data": {"stocks": result, "count": len(result)},
        "message": f"已导入 {len(result)} 只股票"
    }


@app.get("/api/live/pool/export")
async def export_stock_pool(format: str = "json"):
    """导出股票池"""
    pool = _load_stock_pool()
    if format == "csv":
        csv_lines = ["代码,名称,行业"]
        for s in pool:
            csv_lines.append(f"{s['code']},{s['name']},{s.get('industry', '')}")
        return {"status": "success", "data": {"format": "csv", "content": "\n".join(csv_lines)}}
    elif format == "text":
        text_lines = [f"{s['code']},{s['name']},{s.get('industry', '')}" for s in pool]
        return {"status": "success", "data": {"format": "text", "content": "\n".join(text_lines)}}
    else:
        return {"status": "success", "data": {"format": "json", "stocks": pool, "codes": [s['code'] for s in pool]}}

# ============================================================
# 交易执行记录 API（手动执行后记录）
# ============================================================

class TradeRecordRequest(BaseModel):
    """手动执行记录"""
    ts_code: str
    side: str = 'BUY'           # BUY / SELL
    price: float = 0            # 实际成交价
    quantity: int = 0           # 实际成交数量
    reason: str = ''            # 备注


@app.post("/api/live/trade/record")
async def record_manual_trade(req: TradeRecordRequest):
    """记录一笔手动执行的交易（用户在APP操作后回来记录）"""
    server = get_live_server()
    result = server.record_manual_trade(
        ts_code=req.ts_code,
        side=req.side,
        price=req.price,
        quantity=req.quantity,
        reason=req.reason
    )
    return {"status": "success" if result.get('success') else "error", "data": result}


@app.get("/api/live/trade/checklist")
async def get_trade_checklist():
    """获取交易执行清单"""
    server = get_live_server()
    if not hasattr(server, 'checklist') or server.checklist is None:
        return {"status": "success", "data": {"items": [], "summary": {"total": 0, "pending": 0, "executed": 0, "skipped": 0}}}
    return {
        "status": "success",
        "data": {
            "items": server.checklist.get_all(),
            "summary": server.checklist.get_summary(),
            "history": server.checklist.get_history(20),
        }
    }


@app.post("/api/live/trade/checklist/{item_id}/done")
async def mark_checklist_done(item_id: str, price: float = 0, quantity: int = 0):
    """标记清单项已执行"""
    server = get_live_server()
    if not hasattr(server, 'checklist') or server.checklist is None:
        return {"status": "error", "message": "清单不存在"}
    item = server.checklist.mark_executed(item_id, price, quantity)
    if item:
        return {"status": "success", "data": item}
    return {"status": "error", "message": f"找不到 {item_id}"}


@app.post("/api/live/trade/checklist/{item_id}/skip")
async def mark_checklist_skipped(item_id: str):
    """标记清单项跳过"""
    server = get_live_server()
    if not hasattr(server, 'checklist') or server.checklist is None:
        return {"status": "error", "message": "清单不存在"}
    item = server.checklist.mark_skipped(item_id)
    if item:
        return {"status": "success", "data": item}
    return {"status": "error", "message": f"找不到 {item_id}"}

# ============================================================
# 启动服务
# ============================================================

if __name__ == "__main__":
    import socket

    # 获取局域网IP
    lan_ip = ''
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = '127.0.0.1'

    print("=" * 60)
    print("A股量化交易系统 - 后端服务")
    print("=" * 60)
    print()
    print("  本地访问:")
    print("    主页导航:   http://localhost:8000")
    print("    回测分析:   http://localhost:8000/app.html")
    print("    实盘交易:   http://localhost:8000/live.html")
    print("    手机看信号: http://localhost:8000/mobile.html")
    print("    可视化回测: http://localhost:8501")
    print("    API文档:    http://localhost:8000/docs")
    if lan_ip and lan_ip != '127.0.0.1':
        print()
        print("  手机扫码访问:")
        print(f"    http://{lan_ip}:8000/mobile.html")
        print(f"    (手机和电脑需在同一WiFi)")
    print()
    print("  按 Ctrl+C 停止服务")
    print()

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1
    )