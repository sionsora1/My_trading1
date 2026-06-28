# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Commands

```bash
# Start Web server
python server.py                          # → http://localhost:8000

# Start live trading (CLI mode)
python live_server.py --broker sim --mode semi --interval 60

# Run tests
python -m pytest tests/unit/test_market_watcher.py -v
python -m pytest tests/ -v

# Lint check (prints → logger migration status)
grep -rn "print(" broker/ data/ strategy/ backtest/ --include="*.py" | grep -v __main__ | grep -v "print_checklist\|print_report\|banner"
```

## Architecture

### Data Flow

```
TDX (pytdx) ──→ DataFetcher (facade) ──→ live_server.py / server.py
AKShare ──────┘        │                        │
                       ▼                        ▼
                  SQLiteManager            BacktestEngine
                  (11 tables)              (daily bars)
                       │                        │
                       └────────┬───────────────┘
                                ▼
                          strategy/*.py
                          (13 strategies)
```

### Key Patterns

**Data Source**: `data/fetcher.py` is a facade. All data access goes through `DataFetcher`, which auto-falls-back from TDX → AKShare. Never import akshare or pytdx directly outside `data/sources/`.

**Logger**: Always `from utils.logger import get_logger; logger = get_logger('module_name', 'module_name.log')`. Sets `propagate=False` so logs don't pollute `server.log`. JSON format for files, human-readable for console. Never use `print()` in production code — only in `__main__` test blocks or user-facing CLI banners.

**Singletons**: MonitorEngine and MarketWatcherEngine use `get_instance()` with double-check locking. `__init__` checks `hasattr(self, "_poller")` to prevent re-initialization.

**SSE Push**: Cross-thread SSE uses `asyncio.run_coroutine_threadsafe(self._sse.push(data), loop)`. The event loop reference is set via `set_event_loop()` during uvicorn startup.

**Exception Handling Rule**: Poll loops must have global `try/except` wrapping the entire body with `logger.error(..., exc_info=True)`. External dependencies (TDX/DB) must have `try/except` + log. Never `except: pass` without at least a `logger.debug`.

### Where Things Live

| Concern | Location |
|---------|----------|
| Data fetching | `data/fetcher.py` (facade), `data/sources/` (implementations) |
| DB schema + CRUD | `data/database.py` (SQLiteManager, 11 tables) |
| Data sync | `data/sync_service.py` (TDX→DB) |
| Strategies | `strategy/*.py` (13 strategies, all daily-frequency except IntradayReversal) |
| Backtest | `backtest/engine.py` (BacktestEngine), `backtest/matcher.py` (MatchEngine) |
| Live trading | `live_server.py` (LiveTradingServer) |
| Risk management | `broker/risk_manager.py` (RiskManager), `sigbus/filters.py` (SignalFilters) |
| Big order monitor | `broker/monitor.py` (MonitorEngine + Detector + TDXQuotesPoller) |
| Market watcher | `broker/market_watcher.py` (MarketWatcherEngine + 5 sub-modules) |
| Signal pipeline | `sigbus/bus.py` (collect→dedup→filter→sort→allocate) |
| Web API | `server.py` (FastAPI), `web/api.py` (REST router) |
| Web frontend | `web/live.html` (live trading + monitor + watcher tabs) |
| Config | `config/settings.py` (DATA_SOURCE_CONFIG, LIVE_TRADING_CONFIG, BACKTEST_CONFIG) |
| Logs | `logs/` (6 log files: server, live_trading, backtest, risk, data, error) |

### market_data Dict Format

All strategies consume this format:

```python
market_data = {
    "YYYYMMDD": {
        "600519": {         # pure code, no .SH/.SZ
            "close": 150.0, "open": 148.0, "high": 152.0, "low": 147.5,
            "volume": 5000000, "amount": 750000000,
            "ma5": 149.0, "ma10": 147.5, "ma20": 145.0, "ma60": 140.0,
            "return_1d": 0.015, "return_5d": 0.03, "return_20d": 0.08,
            "volatility": 0.18, "pe": 25.0, "pb": 5.0, "roe": 0.15,
            "profit_growth": 0.12, "revenue_growth": 0.08, "gross_margin": 0.70,
            "industry": "白酒", "name": "贵州茅台", "market_cap": 1.5e11,
            # ... 30+ fields total
        }
    }
}
```

### Coding Standards (Non-negotiable)

- Single quotes. PEP 8 imports. PascalCase classes, snake_case methods, `_private` prefix.
- Every class and public method has docstring with Args/Returns.
- All public method params and returns have type annotations.
- External deps have try/except + logger. Poll loops have global try/except.
- `from utils.logger import get_logger; logger = get_logger(...)` — never `print()` or `logging.getLogger()`.
- Business events → `logger.info("event", extra={"data": {...}})` with structured data.
- Errors → `logger.error(..., exc_info=True)` with full traceback.
- Absolute order threshold is 2000万/3s + 1亿/60s.

### Development Workflow (Mandatory — scaled to task size)

**核心原则：任何代码改动前，必须先走流程，不能跳过直接写代码。**

流程按任务规模等比缩放：

```
brainstorming  →  writing-plans  →  TDD 实现  →  code-review
```

**大功能（新模块/多文件改动）：**
- brainstorming：读代码 → 问 3-5 个问题 → 出 2-3 个方案 → 写 spec
- writing-plans：拆成 N 个 task，每个含代码示例和测试要求
- TDD：每个 task 先写测试 → 最小实现 → 重构 → commit
- code-review：每 task 审查 + 最后全分支审查

**小 bug（改几行/改一个文件）：**
- brainstorming：读代码 → 确认 1 个问题 → 一句话方案
- writing-plans：直接 1 个 task，2-5 分钟
- TDD：写测试用例 → 改代码 → 跑通
- code-review：快速扫一眼即可

**底线（不能跳过的）：**
- 不能省略 brainstorming 直接写代码
- 不能省略 writing-plans（哪怕只有 1 个 task）
- 不能先写代码再补测试
- 优先复用现有模块/函数/工具，不重复造轮子

**易回滚：删除此整个 "Development Workflow" 段落即恢复之前行为。**
