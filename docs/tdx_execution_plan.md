# TDX 数据源集成 — 执行方案

## 依赖关系

```
Phase 1 (并行)
├── 模块A: 数据源抽象层  ← 基础，其他都依赖它
└── 模块B: 数据库升级    ← 可并行

Phase 2 (并行，依赖 A+B)
├── 模块C: 数据同步服务
├── 模块D: 回测分钟执行器
└── 模块E: 实盘盘口下单

Phase 3 (依赖 C+D+E)
└── 模块F: 调度器 + 集成收尾
```

## 模块拆分

### 模块A: 数据源抽象层（依赖：无）

| 文件 | 操作 |
|------|------|
| `data/sources/__init__.py` | 新建 — 注册表 + `get_data_source()` 工厂 |
| `data/sources/base.py` | 新建 — `BaseDataSource` ABC（6 抽象 + 3 可选） |
| `data/sources/tdx_source.py` | 新建 — `TDXDataSource`（pytdx 实现） |
| `data/sources/akshare_source.py` | 新建 — 从 `fetcher.py` 迁移现有 AKShare 逻辑 |
| `data/fetcher.py` | 改造 — 门面模式 + `_fetch()` 自动降级 |
| `config/settings.py` | 微调 — 新增 `DATA_SOURCE` 配置项 |
| `requirements.txt` | 新增 `pytdx>=1.72` |

**验收标准：**
- `from data.sources import get_data_source; src = get_data_source('tdx')` 可运行
- `src.get_daily_data('000001.SZ', '20260601', '20260616')` 返回正确 DataFrame
- `DataFetcher(primary='tdx')` 可正常工作，TDX 失败自动降级 akshare

---

### 模块B: 数据库升级（依赖：无）

| 文件 | 操作 |
|------|------|
| `data/database.py` | `_create_tables` 新增 3 张表 DDL |
| `data/database.py` | ALTER 语句（`minute_bars` + `amount`，`fundamentals` + 8 列） |
| `data/database.py` | 新增 7 个 CRUD 方法：`upsert_xdxr`、`upsert_block_info`、`get_block_stocks`、`upsert_finance_detail`、`get_finance_detail`、`upsert_minute_bars`（修正）、`get_minute_bars_by_date` |
| `data/database.py` | `_create_indexes` 新增 3 个索引 |

**验收标准：**
- 新表 `xdxr`、`block_info`、`finance_detail` 自动创建
- `db.upsert_xdxr([...])` 正确写入
- `db.get_block_stocks('半导体')` 返回成分股列表

---

### 模块C: 数据同步服务（依赖：A + B）

| 文件 | 操作 |
|------|------|
| `data/sync_service.py` | 新建 — `DataSyncService` 类 |

**核心方法：**

| 方法 | 功能 |
|------|------|
| `sync_stock_list()` | TDX 全量股票列表 → `stock_info` 表 |
| `sync_daily_bars(codes, start, end)` | 批量日线 → `daily_bars` 表（增量） |
| `sync_minute_bars(codes, date, period)` | 分钟线 → `minute_bars` 表 |
| `sync_xdxr(codes)` | 全量除权除息 → `xdxr` 表 |
| `sync_blocks()` | 全量板块成分 → `block_info` 表 |
| `sync_finance_detail(codes)` | 扩展财务 → `finance_detail` 表 |
| `check_and_fill_gaps(codes, days_back)` | 检查 DB 缺失日，自动补全 |
| `daily_close_sync(codes, date)` | 收盘一键同步（日线+分钟线+财务） |

**验收标准：**
- `sync_daily_bars(['000001','600519'], '20260101', '20260616')` 正确入库
- `check_and_fill_gaps(...)` 自动补全缺失数据
- 同步过程有 logger 进度输出

---

### 模块D: 回测分钟执行器（依赖：A + B）

| 文件 | 操作 |
|------|------|
| `backtest/minute_executor.py` | 新建 — `MinuteBarExecutor` 类 |
| `backtest/engine.py` | 改造 — `BacktestConfig` 新增 `use_minute_execution` 开关 |

**MinuteBarExecutor 核心方法：**

| 方法 | 功能 |
|------|------|
| `_calc_vwap()` | 计算当日 VWAP |
| `estimate_fill_price(side, quantity, time_index)` | 从触发时间点逐分钟累加成交量，计算加权均价 |
| `get_liquidity_profile()` | 返回当日每分钟累计成交量曲线 |

**BacktestEngine 集成：**
- 新增 `use_minute_execution: bool = False` 配置项
- 当启用时，`_execute_trade` 调用 `MinuteBarExecutor` 代替固定滑点
- DB 中无分钟线时自动降级为原逻辑

**验收标准：**
- 用历史分钟线回测，成交价基于 VWAP 而非固定滑点
- 分钟线不足时返回部分成交
- `use_minute_execution=False` 时行为完全不变

---

### 模块E: 实盘盘口下单（依赖：A）

| 文件 | 操作 |
|------|------|
| `broker/order_book.py` | 新建 — `OrderBookEstimator` 类 |
| `live_server.py` | 改造 — `scan_and_trade()` 下单前调用盘口估算 |

**OrderBookEstimator 核心方法：**

| 方法 | 功能 |
|------|------|
| `estimate(quotes, side, quantity)` | 基于 5 档盘口估算真实成交价 |
| `get_spread(quotes)` | 计算买卖价差 |
| `get_depth_summary(quotes)` | 盘口深度摘要 |

**live_server.py 集成：**
- 下单前从 `TDXDataSource.get_realtime_quotes` 获取盘口
- 调用 `OrderBookEstimator.estimate` 预估成交价
- 盘口流动性不足时：WARNING 日志 + 降级数量 或 限价单

**验收标准：**
- 盘口数据可用时，下单价格使用估算价
- 流动性不足时日志告警
- TDX 不可用时自动降级为快照价

---

### 模块F: 调度器 + 集成收尾（依赖：C + D + E）

| 文件 | 操作 |
|------|------|
| `server.py` | `market_scheduler` 改用 `DataSyncService` |
| `server.py` | `check_and_init_data` 优先从 TDX 初始化 |
| `live_server.py` | `_fetch_market_data` 中实时行情改用 TDX |
| `web/api.py` | `/api/sync` 端点增加 `source` 参数 |

**验收标准：**
- 服务启动自动连接 TDX，失败降级 AKShare
- 收盘自动同步全部数据到 DB
- `/api/sync?source=tdx` 手动触发 TDX 全量同步

---

## 执行顺序

```
第1轮（并行，无依赖）
  子Agent-1 → 模块A: 数据源抽象层
  子Agent-2 → 模块B: 数据库升级

第2轮（并行，依赖A+B）
  子Agent-3 → 模块C: 数据同步服务
  子Agent-4 → 模块D: 回测分钟执行器
  子Agent-5 → 模块E: 实盘盘口下单

第3轮（依赖C+D+E）
  子Agent-6 → 模块F: 调度器 + 集成收尾
```

每个子 Agent 完成后，主 Agent 验证验收标准，确认通过后进入下一轮。
