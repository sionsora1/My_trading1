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
from datetime import datetime
from pathlib import Path

from backtest.engine import BacktestEngine, BacktestConfig
from strategy import get_strategy, get_all_strategies, STRATEGY_REGISTRY
from analysis.market_regime import MarketRegimeDetector, StrategyRegimeAdapter, MarketRegime
from analysis.ai_analyzer import analyze_fed_event, AIAnalyzer
from data.fetcher import DataFetcher, DataCache
from config.settings import BACKTEST_CONFIG

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
async def root():
    """返回Web界面"""
    html_file = BASE_DIR / "web" / "app.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>请先创建 web/app.html 文件</h1>")

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
# 启动服务
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("A股量化交易系统 - 后端服务")
    print("=" * 60)
    print("\n启动中...")
    print("API文档: http://localhost:8000/docs")
    print("接口地址: http://localhost:8000/api")
    print("\n按 Ctrl+C 停止服务\n")

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1
    )