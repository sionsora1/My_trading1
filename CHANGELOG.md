# 更新日志

## v2.2.1 (2026-06-17)

### 修复
- 大单监控页面按钮状态不同步：页面加载时不检查后端状态，按钮始终显示"未开启"
- 监控已运行时点"开启"提示"启动失败"而非"已在运行中"
- `startMonitor()` 增加 `already_running` 状态处理，不再误报错误
- `DOMContentLoaded` 新增 `checkMonitorStatus()` 自动同步按钮状态

---

## v2.2.0 (2026-06-17)

### 新增
- TDX(通达信) 数据源集成 (`pytdx`)，毫秒级行情获取
- `data/sources/` 数据源抽象层：`BaseDataSource` + 注册表 + 工厂模式
- `data/sync_service.py`：DataSyncService 定时同步服务（8个同步方法）
- `backtest/minute_executor.py`：MinuteBarExecutor VWAP 分钟线执行器
- `broker/order_book.py`：OrderBookEstimator 5档盘口估算器
- `broker/monitor.py`：大单监控模块
- `utils/indicators.py`：技术指标工具
- `web/db_api.py`：数据库 REST API
- `web/live.html`：实盘面板页面
- `scripts/daily_sync.py`：每日数据同步脚本
- `docs/tdx_integration_design.md`：TDX 集成设计文档
- `docs/tdx_execution_plan.md`：TDX 执行方案
- `docs/large_order_monitor_design.md`：大单监控设计文档

### 改造
- `data/fetcher.py`：门面模式，多数据源自动降级 (TDX→AKShare)
- `data/database.py`：新增 3 张表 (xdxr/block_info/finance_detail)，7 个 CRUD 方法
- `backtest/engine.py`：新增 `use_minute_execution` 开关，集成分钟线执行
- `backtest/matcher.py`：支持 `override_fill_price` 参数
- `live_server.py`：盘口估算集成，下单前查5档深度
- `server.py`：`init_tdx_data` + `market_scheduler` 改用 `DataSyncService`
- `web/api.py`：`/api/sync?source=tdx` 端点
- `config/settings.py`：新增 `DATA_SOURCE_CONFIG`

---

## v2.1.0 (2026-06-16)

### 新增
- 统一结构化日志系统
- `utils/logger.py`：重写，JsonFormatter + get_logger + RotatingFileHandler
- `docs/logging_design.md`：日志系统设计文档

### 改造
- `live_server.py`：全部 `print()` → 结构化日志，新增业务事件日志
- `server.py`：uvicorn handler 重复修复，新增 `error.log` 汇总，回测日志
- `sigbus/filters.py`：7项检查通过/拦截日志
- `broker/risk_manager.py`：CRITICAL 级别交易暂停日志
- `data/fetcher.py`：30+ 处 `print()` → logger
- `sigbus/bus.py`：移除内联 import，改用 module-level logger
- `/api/logs`：支持 module/level/keyword 参数

---

## v2.0.0 (2026-06-10)

### 新增
- FastAPI Web 服务 + REST API
- 实盘交易服务（模拟盘/QMT/同花顺）
- 信号总线系统：收集→去重→过滤→排序→分配
- SignalFilters 7项风控检查
- RiskManager 风控管理器（日亏损/连续亏损/最大回撤熔断）
- 数据库浏览器 Web 页面
- 止损止盈系统
- strict_mode 回测（消除前视偏差）

### 改造
- 策略注册表 + 工厂模式
- 券商注册表 + 工厂模式
- SQLite 持久化层（7张表，WAL模式，时点查询）

---

## v1.2.0 (2024-01-20)
- Streamlit 可视化界面
- 自定义股票池
- 交互式图表

## v1.1.0 (2024-01-15)
- 数据源从 Tushare 切换到 AKShare
- 每日操作报告
- 止损止盈系统
- 行业分散约束

## v1.0.0 (2024-01-01)
- 初始版本
- 8因子选股策略
- 位置判断策略
- 回测引擎
- 参数优化
