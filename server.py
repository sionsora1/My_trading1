"""
A股量化交易系统 - 后端服务
基于FastAPI，提供REST API接口和Web页面
"""

import sys
import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ============================================================
# 日志配置 — 同时输出到控制台和文件
# ============================================================

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "server.log"

def setup_logging():
    """配置全局日志：控制台 + 滚动文件（单文件 5MB，保留 5 个）+ 错误汇总"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if root.handlers:
        return  # 已配置过

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 滚动文件：server.log（5MB/个，保留5个）
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # 全局错误汇总：error.log（ERROR+，5MB/个，保留5个）
    error_handler = RotatingFileHandler(
        LOG_DIR / 'error.log', maxBytes=5 * 1024 * 1024, backupCount=5,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    root.addHandler(error_handler)

setup_logging()
logger = logging.getLogger("server")

# 回测专用 Logger（JSON 格式写入 backtest.log，不污染 server.log）
from utils.logger import get_logger as _get_logger
backtest_logger = _get_logger('backtest', 'backtest.log')

from fastapi import FastAPI, HTTPException, BackgroundTasks, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional, Dict
import uvicorn
import json
import uuid
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from backtest.engine import BacktestEngine, BacktestConfig
from strategy import get_strategy, get_all_strategies, STRATEGY_REGISTRY
from analysis.market_regime import MarketRegimeDetector, StrategyRegimeAdapter, MarketRegime
from analysis.ai_analyzer import analyze_fed_event, AIAnalyzer
from data.fetcher import DataFetcher, DataCache
from config.settings import BACKTEST_CONFIG, LIVE_TRADING_CONFIG, API_KEY, PROTECTED_PATH_PREFIXES

# 获取当前目录
BASE_DIR = Path(__file__).parent


# ============================================================
# v2.0 Module Initialization Helpers
# ============================================================

from data.database import SQLiteManager
from data.calendar import TradeCalendar
from sigbus.bus import SignalBus
from broker.manual_broker import ManualBroker
from broker.monitor import MonitorEngine
from config.strategy_profiles import SIGNAL_BUS_CONFIG
from config.settings import LIVE_TRADING_CONFIG, DATA_CACHE_DIR
from data.sync_service import DataSyncService
from data.sources import get_data_source


def check_and_init_data(db, calendar, fetcher):
    """
    检查数据状态，必要时进行初始化
    Returns: dict with status for each data type
    """
    status = {}

    # 1. 交易日历
    try:
        cal_count = db.calendar_row_count()
        if cal_count == 0:
            logger.info("[Init] 正在加载交易日历...")
            calendar.sync_to_db()
            status['calendar'] = 'synced'
        else:
            status['calendar'] = f'ok ({cal_count} days)'
    except Exception as e:
        status['calendar'] = f'error: {e}'

    # 2. 日线数据
    try:
        daily_count = db._conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        if daily_count == 0:
            logger.info("[Init] 日线数据为空 — 从 JSON cache 同步...")
            sync_result = db.sync_from_cache()
            status['daily_bars'] = f"synced {sync_result['daily_bars']} rows"
        else:
            status['daily_bars'] = f'ok ({daily_count} rows)'
    except Exception as e:
        status['daily_bars'] = f'error: {e}'

    # 3. 股票信息
    try:
        stock_count = db._conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]
        if stock_count == 0:
            logger.info("[Init] 正在获取股票基本信息...")
            try:
                df = fetcher.get_stock_list()
                if df is not None and not df.empty:
                    rows = []
                    for _, r in df.iterrows():
                        code = str(r.get('symbol', r.get('code', ''))).strip()
                        name = str(r.get('name', '')).strip()
                        if code and len(code) == 6:
                            # Determine exchange suffix
                            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                            rows.append({'ts_code': ts_code, 'name': name, 'code': code})
                    if rows:
                        db.upsert_stock_info(rows)
                        status['stock_info'] = f'synced {len(rows)} stocks'
                    else:
                        status['stock_info'] = 'no data from fetcher'
                else:
                    status['stock_info'] = 'fetcher returned empty'
            except Exception as e:
                status['stock_info'] = f'fetcher error: {e}'
        else:
            status['stock_info'] = f'ok ({stock_count} stocks)'
    except Exception as e:
        status['stock_info'] = f'error: {e}'

    return status


def init_tdx_data(db, sync_service, fetcher):
    """
    Initialize TDX-specific data tables if empty.
    Runs after check_and_init_data.
    """
    status = {}

    # 1. Stock list from TDX (fast, 0.09s)
    try:
        stock_count = db._conn.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]
        if stock_count == 0:
            logger.info('[Init] 从 TDX 同步股票列表...')
            count = sync_service.sync_stock_list()
            status['stock_list'] = f'synced {count} stocks'
        else:
            status['stock_list'] = f'ok ({stock_count} stocks)'
    except Exception as e:
        status['stock_list'] = f'error: {e}'

    # 2. XDR data
    try:
        xdxr_count = db._conn.execute("SELECT COUNT(*) FROM xdxr").fetchone()[0]
        if xdxr_count == 0:
            logger.info('[Init] 从 TDX 同步除权除息...')
            # Get stock codes from DB
            codes = [row['ts_code'] for row in
                     db._conn.execute("SELECT DISTINCT ts_code FROM daily_bars LIMIT 500").fetchall()]
            if codes:
                count = sync_service.sync_xdxr(codes)
                status['xdxr'] = f'synced {count} records'
            else:
                status['xdxr'] = 'no stock codes to sync'
        else:
            status['xdxr'] = f'ok ({xdxr_count} records)'
    except Exception as e:
        status['xdxr'] = f'error: {e}'

    # 3. Block info (skip if already populated — it's large)
    try:
        block_count = db._conn.execute("SELECT COUNT(*) FROM block_info").fetchone()[0]
        if block_count == 0:
            status['block_info'] = 'skipped (use /api/sync for full sync)'
        else:
            status['block_info'] = f'ok ({block_count} records)'
    except Exception as e:
        status['block_info'] = f'error: {e}'

    return status


async def market_scheduler(live_server, db, fetcher, calendar):
    """交易时段调度器（后台异步任务）"""
    from datetime import datetime, time
    import asyncio

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime('%Y%m%d')
            current_time = now.time()

            # Check if trade day
            is_trade_day = True
            try:
                is_trade_day = calendar.is_trade_day(today_str)
            except Exception:
                pass  # If calendar check fails, assume trade day

            if not is_trade_day:
                await asyncio.sleep(300)  # Sleep 5 min on non-trade days
                continue

            # --- 09:25 盘前准备 ---
            if time(9, 25) <= current_time < time(9, 30):
                logger.info("[Scheduler] 盘前准备...")
                try:
                    stock_pool = live_server.config.get('scan', {}).get('stock_pool', [])
                    if stock_pool:
                        # Fetch minute bars for pre-market analysis
                        fetcher.fetch_and_store_minute_bars(stock_pool, db, period='5')
                    live_server.update_market_prices()
                except Exception as e:
                    logger.error(f"[Scheduler] 盘前准备失败: {e}")
                await asyncio.sleep(60)

            # --- 09:30-15:00 盘中扫描 ---
            elif time(9, 30) <= current_time < time(15, 0):
                try:
                    await asyncio.to_thread(live_server.scan_and_trade)
                except Exception as e:
                    logger.error(f"[Scheduler] 盘中扫描失败: {e}")
                await asyncio.sleep(60)

            # --- 15:00-15:30 收盘处理 ---
            elif time(15, 0) <= current_time < time(15, 30):
                logger.info("[Scheduler] 收盘处理...")
                try:
                    stock_pool = live_server.config.get('scan', {}).get('stock_pool', [])
                    if stock_pool:
                        # 收盘一键同步：日线 + 分钟线 + 财务
                        ts_codes = [f"{c}.SH" if c.startswith('6') else f"{c}.SZ" for c in stock_pool]
                        sync_result = sync_service.daily_close_sync(ts_codes, today_str)
                        logger.info(f"[Scheduler] 数据同步完成: {sync_result}")

                    # Cleanup old minute bars (keep 5 days)
                    if hasattr(db, 'cleanup_old_minute_bars'):
                        db.cleanup_old_minute_bars(keep_days=5)

                    # Performance snapshot
                    try:
                        acc = live_server.broker.get_account()
                        positions = live_server.broker.get_positions()
                        snapshot = {
                            'date': today_str,
                            'total_assets': acc.total_assets,
                            'available_cash': acc.available_cash,
                            'market_value': acc.market_value,
                            'positions': {}
                        }
                        for code, pos in positions.items():
                            snapshot['positions'][code] = {
                                'name': getattr(pos, 'name', code),
                                'qty': getattr(pos, 'quantity', 0),
                                'cost': getattr(pos, 'cost_price', 0),
                            }
                        if hasattr(db, 'save_account_snapshot'):
                            db.save_account_snapshot(
                                acc.total_assets, acc.available_cash,
                                acc.market_value, snapshot['positions']
                            )
                    except Exception as e:
                        logger.warning(f"[Scheduler] 绩效快照失败: {e}")

                except Exception as e:
                    logger.error(f"[Scheduler] 收盘处理失败: {e}")
                await asyncio.sleep(300)

            # --- 非交易时段 ---
            else:
                await asyncio.sleep(300)

        except Exception as e:
            logger.error(f"[Scheduler] 调度异常: {e}")
            await asyncio.sleep(60)


# ============================================================
# 启动时备份运行时状态（防止意外丢失持仓/风控/股票池数据）
# ============================================================

_RUNTIME_STATE_FILES = [
    "sim_account.json",
    "risk_state.json",
    "trade_checklist.json",
    "live_stock_pool.json",
    "signals.json",
]


def restore_runtime_state_if_needed():
    """如果运行时状态文件丢失（git pull --rebase 可能触发删除），从最新备份恢复。"""
    import shutil
    backup_root = Path(DATA_CACHE_DIR) / "backups"
    if not backup_root.exists():
        return

    missing = [f for f in _RUNTIME_STATE_FILES if not (Path(DATA_CACHE_DIR) / f).exists()]
    if not missing:
        return

    # 按日期倒序找最新备份
    backups = sorted(
        [d for d in backup_root.iterdir() if d.is_dir()],
        key=lambda d: d.name, reverse=True,
    )
    for backup_dir in backups:
        restored = 0
        for fname in missing:
            src = backup_dir / fname
            dst = Path(DATA_CACHE_DIR) / fname
            if src.exists():
                try:
                    shutil.copy2(src, dst)
                    restored += 1
                except Exception as e:
                    logger.warning(f"恢复失败 {fname}: {e}")
        if restored > 0:
            logger.warning(
                f"检测到 {len(missing)} 个状态文件丢失，已从 {backup_dir.name} 恢复 {restored} 个"
            )
            return


def backup_runtime_state():
    """启动时自动备份运行时状态文件到 data_cache/backups/YYYY-MM-DD/"""
    import shutil
    backup_root = Path(DATA_CACHE_DIR) / "backups"
    today_str = datetime.now().strftime("%Y-%m-%d")
    backup_dir = backup_root / today_str
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed = 0
    for fname in _RUNTIME_STATE_FILES:
        src = Path(DATA_CACHE_DIR) / fname
        if src.exists():
            dst = backup_dir / fname
            try:
                shutil.copy2(src, dst)
                backed += 1
            except Exception as e:
                logger.warning(f"备份失败 {fname}: {e}")

    # 清理 30 天前的旧备份
    cutoff = datetime.now() - timedelta(days=30)
    try:
        for d in backup_root.iterdir():
            if d.is_dir():
                try:
                    d_date = datetime.strptime(d.name, "%Y-%m-%d")
                    if d_date < cutoff:
                        shutil.rmtree(d)
                except (ValueError, OSError):
                    pass
    except Exception:
        pass

    if backed > 0:
        logger.info(f"运行时状态已备份: {backed} 个文件 → {backup_dir}")


restore_runtime_state_if_needed()
backup_runtime_state()

# ============================================================
# v2.0 Module Initialization
# ============================================================

# Database
db = SQLiteManager()
calendar = TradeCalendar(db)
fetcher = DataFetcher()

# 初始化数据同步服务
sync_service = DataSyncService(db, fetcher._primary)

# Run data initialization check
init_status = check_and_init_data(db, calendar, fetcher)
tdx_init_status = init_tdx_data(db, sync_service, fetcher)
logger.info(f"[Server] Data status: {init_status}")
logger.info(f"[Server] TDX data status: {tdx_init_status}")

# SignalBus
signal_bus = SignalBus(SIGNAL_BUS_CONFIG)

# ManualBroker (for Eastmoney semi-auto)
manual_broker = ManualBroker({
    'initial_capital': LIVE_TRADING_CONFIG.get('sim', {}).get('initial_capital', 100_000),
    'data_dir': DATA_CACHE_DIR,
})
manual_broker.connect()


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

# 静态文件配置
web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/static", StaticFiles(directory=web_dir), name="static")


def _auto_record_on_startup():
    """启动时判断是否在交易时段，是则自动开始录制，收盘自动停止。"""
    try:
        from test.replay_recorder import RecordSession
        from datetime import datetime, time as dt_time
        now = datetime.now()
        if now.weekday() >= 5:
            return
        t = now.time()
        if not (dt_time(9, 25) <= t <= dt_time(15, 0)):
            return
        recorder = RecordSession()
        if not recorder.is_recording:
            recorder.start([])  # 使用默认股票池
            logger.info('[AutoRecord] 开盘自动录制已启动')

        # 后台线程等待收盘自动停止
        import threading
        def _wait_and_stop():
            while recorder.is_recording:
                now2 = datetime.now()
                if now2.time() > dt_time(15, 5) or now2.weekday() >= 5:
                    recorder.stop()
                    RecordSession.cleanup_old(max_days=5)
                    logger.info('[AutoRecord] 收盘自动停止录制')
                    break
                time.sleep(60)
        threading.Thread(target=_wait_and_stop, daemon=True, name='auto-record-stop').start()
    except Exception as e:
        logger.warning(f'[AutoRecord] 启动失败: {e}')


def sync_kline_to_latest():
    """启动时自动同步日线+分钟线到最新交易日，含衍生指标计算。"""
    try:
        from datetime import datetime, timedelta
        latest_date = db._conn.execute('SELECT MAX(trade_date) FROM daily_bars').fetchone()[0]
        today = datetime.now().strftime('%Y%m%d')
        if not latest_date or latest_date < today:
            start = (datetime.strptime(latest_date, '%Y%m%d') + timedelta(days=1)).strftime('%Y%m%d') if latest_date else '20240101'
            codes = [r['ts_code'] for r in db._conn.execute('SELECT ts_code FROM stock_info').fetchall()]
            logger.info(f'[Startup] K线同步: {start} → {today}, {len(codes)} 只')
            svc = DataSyncService(db, fetcher._primary)
            result = svc.sync_daily_bars(codes, start, today)
            logger.info(f'[Startup] 日线: {result}')
            db.compute_all_derived_indicators(codes)
        # 分钟线: 只同步今天
        today_count = db._conn.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE trade_time LIKE ?",
            (f'{today[:4]}-{today[4:6]}-{today[6:8]}%',)
        ).fetchone()[0]
        if today_count == 0:
            codes = [r['ts_code'] for r in db._conn.execute('SELECT ts_code FROM stock_info').fetchall()]
            svc = DataSyncService(db, fetcher._primary)
            svc.sync_minute_bars(codes, today, period='1')
            logger.info(f'[Startup] 分钟线已同步: {today}')
    except Exception as e:
        logger.warning(f'[Startup] K线同步失败: {e}')


@app.on_event("startup")
async def startup_event():
    """在 uvicorn 启动后设置事件循环，供 MonitorEngine / MarketWatcherEngine 跨线程 SSE 推送使用"""
    MonitorEngine.get_instance().set_event_loop(asyncio.get_running_loop())
    from broker.market_watcher import MarketWatcherEngine
    MarketWatcherEngine.get_instance().set_event_loop(asyncio.get_running_loop())
    # 启动时自动同步 K 线到最新（后台，不阻塞服务启动）
    asyncio.create_task(asyncio.to_thread(sync_kline_to_latest))
    # 清理旧录制 + 开盘自动录制
    asyncio.create_task(asyncio.to_thread(_auto_record_on_startup))


@app.get("/", response_class=HTMLResponse)
async def root():
    """主页"""
    index_path = os.path.join(web_dir, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/{filename}.html", response_class=HTMLResponse)
async def serve_html(filename: str):
    """提供HTML页面"""
    file_path = os.path.join(web_dir, f"{filename}.html")
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    raise HTTPException(status_code=404, detail="Page not found")


# ============================================================
# API 鉴权中间件
# ============================================================
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """API Key 鉴权中间件 — 保护实盘交易等敏感接口"""

    async def dispatch(self, request, call_next):
        # 如果未配置 API_KEY，跳过鉴权
        if not API_KEY:
            return await call_next(request)

        # 只检查配置的保护路径
        path = request.url.path
        needs_auth = any(path.startswith(p) for p in PROTECTED_PATH_PREFIXES)

        if needs_auth:
            # 同源请求（来自本服务器web页面）直接放行
            referer = request.headers.get('Referer', '')
            host = request.headers.get('host', '')
            if host and host.split(':')[0] in referer:
                return await call_next(request)

            # 外部请求：需要 API Key
            # 支持 Authorization: Bearer <key> 或 ?api_key=<key>
            auth_header = request.headers.get('Authorization', '')
            token = None

            if auth_header.startswith('Bearer '):
                token = auth_header[7:]
            else:
                token = request.query_params.get('api_key')

            if token != API_KEY:
                return JSONResponse(
                    status_code=401,
                    content={
                        'status': 'error',
                        'detail': 'Unauthorized: valid API Key required. Use Authorization: Bearer <key> or ?api_key=<key>',
                    }
                )

        return await call_next(request)


app.add_middleware(ApiKeyMiddleware)

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
    initial_capital: float = 100000
    max_position: int = 5
    stop_loss: float = -0.08
    move_stop: float = -0.10
    rebalance_frequency: str = "weekly"
    strict_mode: bool = False  # 严格模式：基于前一日数据决策，消除前视偏差

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

@app.get("/kline_vis.html", response_class=HTMLResponse)
async def kline_vis_page():
    """返回K线策略可视化页面"""
    html_file = BASE_DIR / "web" / "kline_vis.html"
    if html_file.exists():
        return HTMLResponse(content=html_file.read_text(encoding='utf-8'))
    return HTMLResponse(content="<h1>请先创建 web/kline_vis.html 文件</h1>")

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
        sorted_strategies = sorted(strategy_scores.items(), key=lambda x: (-x[1], x[0]))

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
    logger.debug(f"daily endpoint called: task_id={task_id}, strategy={strategy}, date={date}")
    logger.debug(f"results keys: {list(results.keys())}")

    if task_id not in results:
        raise HTTPException(status_code=404, detail="结果不存在")

    result = results[task_id]
    logger.debug(f"result keys: {list(result.keys())}")

    # 从strategy_results获取数据
    strategy_results = result.get("strategy_results", {})
    logger.debug(f"strategy_results keys: {list(strategy_results.keys())}")
    logger.debug(f"strategy '{strategy}' in strategy_results: {strategy in strategy_results}")

    # 选择策略
    if strategy not in strategy_results:
        # 默认使用第一个策略
        strategy = list(strategy_results.keys())[0] if strategy_results else None

    if not strategy or strategy not in strategy_results:
        logger.debug("strategy not found, returning empty")
        return {"status": "success", "data": [] if not date else None}

    daily_ops = strategy_results[strategy].get("daily_operations", [])
    logger.debug(f"daily_ops length: {len(daily_ops)}")

    if date:
        # 返回指定日期的操作
        for op in daily_ops:
            if op.get("date") == date:
                return {"status": "success", "data": op}
        raise HTTPException(status_code=404, detail=f"日期 {date} 不存在")

    # 返回所有日期列表
    dates = [op["date"] for op in daily_ops]
    logger.debug(f"returning dates: {dates}")
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
        portfolio = {'cash': 100_000, 'positions': {}}
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

def _build_backtest_data_from_db(stock_pool: list, start_date: str, end_date: str) -> dict:
    """
    从数据库快速构建回测所需的 market_data_by_date。

    优先使用数据库（SQLite），比从 AKShare 实时拉取快 100 倍以上。
    返回格式: {date_str: {ts_code: {close, open, high, low, volume, ma5, ...}}}
    """
    import pandas as pd
    from datetime import timedelta

    try:
        db = SQLiteManager()
    except Exception:
        return {}

    # 扩展前导期（计算 MA60 等需要历史数据）
    start_dt = datetime.strptime(start_date, '%Y%m%d')
    extended_start = (start_dt - timedelta(days=180)).strftime('%Y%m%d')

    market_data = {}
    loaded = 0

    for code in stock_pool:
        ts_code = f"{code}.SH" if str(code).startswith('6') else f"{code}.SZ"
        try:
            bars = db.get_daily_bars(ts_code, extended_start, end_date)
            if len(bars) < 20:
                continue

            loaded += 1
            df = pd.DataFrame(bars)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            close = df['close']
            volume = df['volume'] if 'volume' in df.columns else close * 100

            # 计算技术指标
            df['ma5'] = close.rolling(5, min_periods=1).mean()
            df['ma10'] = close.rolling(10, min_periods=1).mean()
            df['ma20'] = close.rolling(20, min_periods=1).mean()
            df['ma60'] = close.rolling(60, min_periods=1).mean()
            df['volume_ma20'] = volume.rolling(20, min_periods=1).mean()
            df['return_1d'] = close.pct_change(1)
            df['return_5d'] = close.pct_change(5)
            df['return_20d'] = close.pct_change(20)
            df['return_60d'] = close.pct_change(60)
            ret = close.pct_change()
            df['volatility'] = ret.rolling(20, min_periods=5).std()

            # 股票信息
            info = db.get_stock_info(ts_code)
            pe = info.get('pe', 20) if info else 20
            pb = info.get('pb', 3) if info else 3
            name = info.get('name', code) if info else code
            industry = info.get('industry', '未知') if info else '未知'

            for _, row in df.iterrows():
                date_str = str(row['trade_date']).replace('-', '')[:8]
                if date_str < start_date:
                    continue
                if date_str not in market_data:
                    market_data[date_str] = {}

                market_data[date_str][ts_code] = {
                    'ts_code': ts_code,
                    'close': row.get('close', 0) or 0,
                    'open': row.get('open', 0) or 0,
                    'high': row.get('high', 0) or 0,
                    'low': row.get('low', 0) or 0,
                    'volume': row.get('volume', 0) or 0,
                    'amount': row.get('amount', 0) or 0,
                    'turnover': row.get('turnover', 0) or 0,
                    'pct_chg': row.get('pct_chg', 0) or 0,
                    'ma5': row.get('ma5'),
                    'ma10': row.get('ma10'),
                    'ma20': row.get('ma20'),
                    'ma60': row.get('ma60'),
                    'volume_ma20': row.get('volume_ma20'),
                    'return_1d': row.get('return_1d'),
                    'return_5d': row.get('return_5d'),
                    'return_20d': row.get('return_20d'),
                    'return_60d': row.get('return_60d'),
                    'volatility': row.get('volatility'),
                    'pe': pe, 'pb': pb,
                    'ep': 1 / pe if pe and pe > 0 else 0,
                    'roe': 0,
                    'name': name,
                    'industry': industry,
                    'market_cap': info.get('market_cap', 0) if info else 0,
                    'profit_growth': 0,
                    'revenue_growth': 0,
                    'accrual_ratio': 0,
                    'price_percentile_1y': 0,
                }
        except Exception:
            continue

    db.close()

    if loaded > 0:
        logger.info('回测数据加载完成', extra={'data': {
            'source': 'database',
            'loaded_stocks': loaded,
            'trading_days': len(market_data),
        }})

    return market_data


def execute_backtest(task_id: str, request: BacktestRequest):
    """执行回测（后台任务）"""
    import time as _time
    _t_start = _time.time()

    backtest_logger.info('回测任务开始', extra={'data': {
        'task_id': task_id,
        'stock_pool': request.stock_pool,
        'start_date': request.start_date,
        'end_date': request.end_date,
        'strategy_type': request.strategy_type,
        'initial_capital': request.initial_capital,
    }})

    try:
        # 更新状态
        tasks[task_id]['status'] = 'running'
        tasks[task_id]['message'] = '正在获取数据...'
        tasks[task_id]['progress'] = 0.1

        # 获取数据（优先使用数据库，快速且离线可用）
        _t_data = _time.time()
        market_data = _build_backtest_data_from_db(request.stock_pool, request.start_date, request.end_date)

        if not market_data or len(market_data) == 0:
            # 数据库没有数据，尝试 JSON 缓存
            cache_filename = f'market_data_{request.start_date}_{request.end_date}_{len(request.stock_pool)}stocks'
            market_data = data_cache.load_market_data(cache_filename)
            if market_data and len(market_data) > 0:
                backtest_logger.info('数据加载完成', extra={'data': {
                    'task_id': task_id, 'source': 'json_cache', 'trading_days': len(market_data),
                }})

        if not market_data or not isinstance(market_data, dict) or len(market_data) < 10:
            # 最后才从 AKShare 获取（慢）
            tasks[task_id]['message'] = '正在从网络获取数据（首次较慢）...'
            market_data = data_fetcher.build_market_data_by_date(
                request.stock_pool,
                request.start_date,
                request.end_date
            )
            if market_data:
                cache_filename = f'market_data_{request.start_date}_{request.end_date}_{len(request.stock_pool)}stocks'
                data_cache.save_market_data(market_data, cache_filename)
                backtest_logger.info('数据加载完成', extra={'data': {
                    'task_id': task_id, 'source': 'akshare', 'trading_days': len(market_data),
                    'elapsed_s': round(_time.time() - _t_data, 1),
                }})

        if not market_data or len(market_data) == 0:
            tasks[task_id]['status'] = 'failed'
            tasks[task_id]['message'] = '获取数据失败'
            backtest_logger.error('回测失败: 数据获取失败', extra={'data': {'task_id': task_id}})
            return

        _elapsed_data = round(_time.time() - _t_data, 1)
        backtest_logger.info('数据加载完成', extra={'data': {
            'task_id': task_id, 'source': 'database', 'trading_days': len(market_data),
            'elapsed_s': _elapsed_data,
        }})

        tasks[task_id]["progress"] = 0.4
        tasks[task_id]["message"] = "正在运行策略回测..."

        # 配置
        config = BacktestConfig(
            initial_capital=request.initial_capital,
            max_position_num=request.max_position,
            stop_loss_rate=request.stop_loss,
            move_stop_rate=request.move_stop,
            rebalance_frequency=request.rebalance_frequency,
            strict_mode=request.strict_mode
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
        tasks[task_id]['status'] = 'completed'
        tasks[task_id]['progress'] = 1.0
        tasks[task_id]['message'] = '回测完成'
        tasks[task_id]['completed_at'] = datetime.now().isoformat()

        # 汇总指标
        _summary = {}
        for _sn, _sr in strategy_results.items():
            _m = _sr.get('metrics', {})
            _summary[_sn] = {
                'total_return': _m.get('total_return'),
                'annual_return': _m.get('annual_return'),
                'sharpe_ratio': _m.get('sharpe_ratio'),
                'max_drawdown': _m.get('max_drawdown'),
                'win_rate': _m.get('win_rate'),
                'trade_count': _m.get('trade_count'),
            }
        _elapsed_total = round(_time.time() - _t_start, 1)
        backtest_logger.info('回测完成', extra={'data': {
            'task_id': task_id,
            'strategy_results': _summary,
            'elapsed_s': _elapsed_total,
        }})

    except Exception as e:
        tasks[task_id]['status'] = 'failed'
        tasks[task_id]['message'] = f'回测失败: {str(e)}'
        backtest_logger.error(f'回测失败: {e}', exc_info=True, extra={'data': {'task_id': task_id}})


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
        import copy
        config = copy.deepcopy(LIVE_TRADING_CONFIG)
        # 从持久化文件加载股票池（而不是用 settings.py 的默认值）
        persisted_pool = _load_stock_pool()
        if persisted_pool:
            config['scan']['stock_pool'] = [s['code'] for s in persisted_pool]
        _live_server = LiveTradingServer(config)
        # Attach v2.0 global modules so web/api.py can access them
        _live_server.signal_bus = signal_bus
        _live_server.manual_broker = manual_broker
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
                     config_update: LiveConfigUpdate = LiveConfigUpdate(),
                     # 兼容旧前端（query params）
                     broker: Optional[str] = None,
                     mode: Optional[str] = None,
                     strategy: Optional[str] = None):
    """启动实盘交易服务（可指定券商、模式、策略等）"""
    global _live_server

    # 合并 query params（兼容旧调用方式）
    broker = broker or config_update.broker
    mode = mode or config_update.mode
    strategy = strategy or config_update.strategy
    stock_pool = config_update.stock_pool
    interval_seconds = config_update.interval_seconds

    need_rebuild = bool(broker or mode or strategy or stock_pool or interval_seconds)

    if need_rebuild:
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
        if strategy:
            config['scan']['strategy'] = strategy
        if interval_seconds:
            config['scan']['interval_seconds'] = interval_seconds

        if stock_pool:
            config['scan']['stock_pool'] = stock_pool
        else:
            persisted_pool = _load_stock_pool()
            if persisted_pool:
                config['scan']['stock_pool'] = [s['code'] for s in persisted_pool]

        _live_server = LiveTradingServer(config)
        # Attach v2.0 global modules so web/api.py can access them
        _live_server.signal_bus = signal_bus
        _live_server.manual_broker = manual_broker
    else:
        server = get_live_server()
        persisted_pool = _load_stock_pool()
        if persisted_pool:
            server.config['scan']['stock_pool'] = [s['code'] for s in persisted_pool]

    server = get_live_server()
    result = server.start(background=True)

    # 同步更新实时行情（在线程池中执行，避免阻塞事件循环）
    async def update_prices_periodically():
        while server.running:
            await asyncio.sleep(30)
            await asyncio.to_thread(server.update_market_prices)

    if result.get('status') == 'started':
        background_tasks.add_task(update_prices_periodically)

    return {"status": "success", "data": result}


class LiveConfigBody(BaseModel):
    """实盘配置请求体"""
    broker: Optional[str] = None
    mode: Optional[str] = None
    strategy: Optional[str] = None  # 策略选择


@app.post("/api/live/config")
async def live_update_config(req: LiveConfigBody):
    """更新实盘配置（策略/模式/券商），无需重启"""
    server = get_live_server()
    if req.mode:
        server.trade_mode = req.mode
        server.config['mode'] = req.mode
    if req.strategy:
        server.config['scan']['strategy'] = req.strategy
    if req.broker and req.broker != server.broker_name:
        # 切换券商需要重建 broker
        old_running = server.running
        if old_running:
            server.running = False  # 暂停扫描
        server.broker.disconnect()
        server.broker_name = req.broker
        server._init_broker()
        if old_running:
            server.running = True

    strategy_label = server.config.get('scan', {}).get('strategy', 'all')
    if strategy_label in STRATEGY_REGISTRY:
        strategy_label = STRATEGY_REGISTRY[strategy_label]['name']

    return {
        "status": "success",
        "data": {
            "broker": server.broker_name,
            "mode": server.trade_mode,
            "mode_label": "全自动" if server.trade_mode == 'auto' else "半自动",
            "strategy": server.config.get('scan', {}).get('strategy', 'all'),
            "strategy_label": strategy_label,
        }
    }


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
# 大单监控 API
# ============================================================

class MonitorStartRequest(BaseModel):
    """大单监控启动请求"""
    stock_pool: Optional[List[str]] = None


@app.post("/api/monitor/start")
async def monitor_start(request: MonitorStartRequest = MonitorStartRequest()):
    """启动大单监控"""
    engine = MonitorEngine.get_instance()
    if engine.is_running:
        raise HTTPException(status_code=400, detail="监控已在运行中")

    stock_pool = request.stock_pool
    if not stock_pool:
        # 从实盘股票池获取
        persisted_pool = _load_stock_pool()
        if persisted_pool:
            stock_pool = [s['code'] for s in persisted_pool]
        else:
            stock_pool = LIVE_TRADING_CONFIG.get('scan', {}).get('stock_pool', [])

    if not stock_pool:
        raise HTTPException(status_code=400, detail="股票池为空，请提供 stock_pool 或先配置实盘股票池")

    try:
        engine.start(stock_pool)
        logger.info(f"大单监控已启动, stock_count={len(stock_pool)}")
        return {"status": "started", "stock_count": len(stock_pool)}
    except Exception as e:
        logger.error(f"启动大单监控失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/monitor/stop")
async def monitor_stop():
    """停止大单监控"""
    engine = MonitorEngine.get_instance()
    if not engine.is_running:
        raise HTTPException(status_code=400, detail="监控未在运行")

    try:
        total_alerts = len(engine._alerts)
        engine.stop()
        logger.info(f"大单监控已停止, total_alerts={total_alerts}")
        return {"status": "stopped", "total_alerts": total_alerts}
    except Exception as e:
        logger.error(f"停止大单监控失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/monitor/status")
async def monitor_status():
    """获取大单监控状态"""
    engine = MonitorEngine.get_instance()
    stock_count = len(engine._stock_pool) if hasattr(engine, '_stock_pool') and engine._stock_pool else 0
    return {
        "running": engine.is_running,
        "stock_count": stock_count,
        "total_alerts": len(engine._alerts),
    }


@app.get("/api/monitor/history")
async def monitor_history(limit: int = 100):
    """获取大单监控历史告警"""
    engine = MonitorEngine.get_instance()
    alerts = engine.get_history(limit)
    return {
        "alerts": [json.loads(a.to_sse_data()) if hasattr(a, 'to_sse_data') else a
                   for a in alerts],
        "total": len(engine._alerts) if hasattr(engine, '_alerts') else 0,
    }


@app.get("/api/monitor/stream")
async def monitor_stream():
    """大单监控 SSE 推送"""
    engine = MonitorEngine.get_instance()
    queue = engine.sse_manager.subscribe()

    async def event_generator():
        try:
            while True:
                try:
                    alert = await asyncio.wait_for(queue.get(), timeout=30.0)
                    data = alert.to_sse_data() if hasattr(alert, 'to_sse_data') else json.dumps(
                        alert if isinstance(alert, dict) else str(alert), ensure_ascii=False
                    )
                    yield f"event: alert\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {{\"time\":\"{datetime.now().strftime('%H:%M:%S')}\"}}\n\n"
        except asyncio.CancelledError:
            engine.sse_manager.unsubscribe(queue)
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# 盯盘 API
# ============================================================

@app.post("/api/watcher/start")
async def watcher_start(request: MonitorStartRequest = MonitorStartRequest()):
    """启动盯盘"""
    from broker.market_watcher import MarketWatcherEngine
    engine = MarketWatcherEngine.get_instance()
    if not engine._event_loop:
        engine.set_event_loop(asyncio.get_running_loop())
    stock_pool = request.stock_pool
    if not stock_pool:
        stock_pool = LIVE_TRADING_CONFIG.get('scan', {}).get('stock_pool', [])
    if not stock_pool:
        raise HTTPException(status_code=400, detail="股票池为空")
    return engine.start(stock_pool)


@app.post("/api/watcher/stop")
async def watcher_stop():
    """停止盯盘"""
    from broker.market_watcher import MarketWatcherEngine
    return MarketWatcherEngine.get_instance().stop()


@app.get("/api/watcher/status")
async def watcher_status():
    """获取盯盘状态"""
    from broker.market_watcher import MarketWatcherEngine
    engine = MarketWatcherEngine.get_instance()
    return {"running": engine.is_running}


@app.get("/api/watcher/stream")
async def watcher_stream():
    """盯盘 SSE 推送"""
    from broker.market_watcher import MarketWatcherEngine
    engine = MarketWatcherEngine.get_instance()
    queue = engine._sse.subscribe()

    async def event_gen():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield f": heartbeat\n\n"
        except asyncio.CancelledError:
            engine._sse.unsubscribe(queue)
            raise

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ============================================================
# 数据库浏览器 API
# ============================================================

@app.get("/api/db/stats")
async def get_db_stats():
    """获取数据库统计信息"""
    try:
        db = SQLiteManager()
        codes = db.get_all_stock_codes()

        # 获取记录数和日期范围
        total_records = 0
        date_range = {'min': '99999999', 'max': '00000000'}
        industries = set()

        for code in codes:
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            info = db.get_stock_info(ts_code)
            if info and info.get('industry'):
                industries.add(info['industry'])

            # 查询记录数和日期范围
            try:
                bars = db.get_daily_bars(ts_code, '20200101', '20261231')
                total_records += len(bars)
                if bars:
                    first_date = bars[0].get('trade_date', '')
                    last_date = bars[-1].get('trade_date', '')
                    if first_date and first_date < date_range['min']:
                        date_range['min'] = first_date
                    if last_date and last_date > date_range['max']:
                        date_range['max'] = last_date
            except Exception:
                pass

        db.close()

        return {
            'total_stocks': len(codes),
            'total_records': total_records,
            'industries': len(industries),
            'date_range': f"{date_range['min'][:4]}-{date_range['min'][4:6]} ~ {date_range['max'][:4]}-{date_range['max'][4:6]}" if date_range['min'] != '99999999' else '-'
        }
    except Exception as e:
        return {'error': str(e), 'total_stocks': 0, 'total_records': 0, 'industries': 0, 'date_range': '-'}


@app.get("/api/db/stocks")
async def get_db_stocks():
    """获取所有股票列表（含记录数、最新日期、最新收盘价）"""
    try:
        db = SQLiteManager()
        codes = db.get_all_stock_codes()
        stocks = []

        for code in codes:
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            info = db.get_stock_info(ts_code)

            # 获取最新数据
            try:
                bars = db.get_daily_bars(ts_code, '20240101', '20261231')
                records = len(bars)
                latest_date = bars[-1].get('trade_date', '') if bars else ''
                latest_close = bars[-1].get('close', 0) if bars else 0
            except Exception:
                records = 0
                latest_date = ''
                latest_close = 0

            stocks.append({
                'code': code,
                'ts_code': ts_code,
                'name': info.get('name', code) if info else code,
                'industry': info.get('industry', '') if info else '',
                'records': records,
                'latest_date': latest_date,
                'latest_close': latest_close
            })

        db.close()
        return {'stocks': stocks, 'total': len(stocks)}
    except Exception as e:
        return {'error': str(e), 'stocks': [], 'total': 0}


@app.get("/api/db/kline/{code}")
async def get_db_kline(
    code: str,
    start_date: str = Query(default='20240101', description='开始日期 YYYYMMDD'),
    end_date: str = Query(default='20261231', description='结束日期 YYYYMMDD'),
    indicators: str = Query(default='ma', description='技术指标: ma, macd, rsi, boll, kdj, all'),
):
    """获取股票K线数据（含技术指标）"""
    try:
        db = SQLiteManager()
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

        info = db.get_stock_info(ts_code)
        if not info:
            db.close()
            raise HTTPException(status_code=404, detail=f"股票 {code} 不在数据库中")

        bars = db.get_daily_bars(ts_code, start_date, end_date)
        db.close()

        if not bars:
            return {'code': code, 'ts_code': ts_code, 'name': info.get('name', code), 'bars': [], 'count': 0}

        # 计算技术指标
        closes = [b['close'] for b in bars]
        volumes = [b['volume'] for b in bars]

        for i, bar in enumerate(bars):
            # MA5
            if i >= 4:
                bar['ma5'] = sum(closes[i-4:i+1]) / 5
            else:
                bar['ma5'] = None

            # MA10
            if i >= 9:
                bar['ma10'] = sum(closes[i-9:i+1]) / 10
            else:
                bar['ma10'] = None

            # MA20
            if i >= 19:
                bar['ma20'] = sum(closes[i-19:i+1]) / 20
            else:
                bar['ma20'] = None

            # MA60
            if i >= 59:
                bar['ma60'] = sum(closes[i-59:i+1]) / 60
            else:
                bar['ma60'] = None

        # 高级技术指标（MACD/RSI/BOLL/KDJ）
        adv_indicators = set(indicators.replace(' ', '').split(','))
        if 'all' in adv_indicators:
            adv_indicators = {'macd', 'rsi', 'boll', 'kdj'}

        if adv_indicators & {'macd', 'rsi', 'boll', 'kdj'}:
            from utils.indicators import compute_advanced_indicators
            compute_advanced_indicators(bars)

        # 格式化输出
        result_bars = []
        for b in bars:
            item = {
                'date': b['trade_date'],
                'open': round(b['open'], 2),
                'high': round(b['high'], 2),
                'low': round(b['low'], 2),
                'close': round(b['close'], 2),
                'volume': b['volume'],
                'pct_chg': round(b.get('pct_chg', 0) or 0, 2),
                'ma5': round(b['ma5'], 2) if b['ma5'] else None,
                'ma10': round(b['ma10'], 2) if b['ma10'] else None,
                'ma20': round(b['ma20'], 2) if b['ma20'] else None,
                'ma60': round(b['ma60'], 2) if b['ma60'] else None,
            }
            # 附加高级指标
            for key in ('macd_dif', 'macd_dea', 'macd_hist',
                        'rsi6', 'rsi12', 'rsi24',
                        'boll_upper', 'boll_mid', 'boll_lower', 'boll_width',
                        'kdj_k', 'kdj_d', 'kdj_j'):
                if key in b and b[key] is not None:
                    item[key] = b[key]
            result_bars.append(item)

        return {
            'code': code,
            'ts_code': ts_code,
            'name': info.get('name', code),
            'industry': info.get('industry', ''),
            'bars': result_bars,
            'count': len(result_bars)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 日志查看 API
# ============================================================

# 模块 → 日志文件映射
_LOG_MODULES = {
    'server': LOG_FILE,
    'live_trading': LOG_DIR / 'live_trading.log',
    'backtest': LOG_DIR / 'backtest.log',
    'risk': LOG_DIR / 'risk.log',
    'data': LOG_DIR / 'data.log',
    'error': LOG_DIR / 'error.log',
}


@app.get("/api/logs")
async def get_logs(
    lines: int = 200,
    level: Optional[str] = None,
    module: Optional[str] = None,
    keyword: Optional[str] = None,
):
    """查看最近 N 行日志，支持按模块、级别、关键词过滤"""
    try:
        # 选择日志文件
        target_file = LOG_FILE
        if module and module in _LOG_MODULES:
            target_file = _LOG_MODULES[module]

        if not target_file.exists():
            return {
                'status': 'success',
                'data': {
                    'lines': [], 'total': 0, 'file': str(target_file),
                    'available_modules': list(_LOG_MODULES.keys()),
                }
            }

        with open(target_file, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.readlines()

        # 按级别过滤
        if level:
            level_upper = level.upper()
            all_lines = [l for l in all_lines if f'[{level_upper}]' in l or f'"{level_upper}"' in l]

        # 按关键词搜索
        if keyword:
            all_lines = [l for l in all_lines if keyword in l]

        recent = all_lines[-lines:]
        return {
            'status': 'success',
            'data': {
                'lines': [l.rstrip() for l in recent],
                'total': len(all_lines),
                'showing': len(recent),
                'file': str(target_file),
                'available_modules': list(_LOG_MODULES.keys()),
            }
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


@app.get("/api/logs/download")
async def download_log():
    """下载完整日志文件"""
    if not LOG_FILE.exists():
        raise HTTPException(status_code=404, detail="日志文件不存在")
    return FileResponse(LOG_FILE, media_type="text/plain", filename="server.log")


# ============================================================
# v2.0 Web API Router
# ============================================================

from web.api import router as web_api_router
app.include_router(web_api_router)

from web.kline_api import router as kline_api_router
app.include_router(kline_api_router)

from web.db_api import router as db_api_router
app.include_router(db_api_router)

from test.replay_api import router as replay_api_router
app.include_router(replay_api_router)

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

    banner = f"""
============================================================
A股量化交易系统 - 后端服务
============================================================

  本地访问:
    主页导航:   http://localhost:8000
    回测分析:   http://localhost:8000/app.html
    实盘交易:   http://localhost:8000/live.html
    手机看信号: http://localhost:8000/mobile.html
    K线策略可视化: http://localhost:8000/kline_vis.html
    API文档:    http://localhost:8000/docs
"""
    if lan_ip and lan_ip != '127.0.0.1':
        banner += f"""
  手机扫码访问:
    http://{lan_ip}:8000/mobile.html
    (手机和电脑需在同一WiFi)
"""
    banner += f"""
  日志文件: {LOG_FILE}

  按 Ctrl+C 停止服务
"""
    logger.info("服务启动中...")
    print(banner)

    import asyncio

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,
        log_config={
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "fmt": "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stdout",
                },
            },
            "loggers": {
                "uvicorn": {"level": "INFO", "handlers": ["default"]},
                "uvicorn.error": {"level": "INFO", "handlers": ["default"]},
                "uvicorn.access": {"level": "INFO", "handlers": ["default"]},
            },
        },
    )