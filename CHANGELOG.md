# 更新日志

## v2.4.0 (2026-06-19)

### 新增
- **六模块异动检测**：6 个独立检测器，共用 SSE 推送管线
  - `DivergenceDetector` 内外盘背离：价涨内盘大/价跌外盘大/极端背离(3拍)
  - `OrderbookDetector` 盘口异动：挂单突变/盘口失衡/大单撤单/价差突变（四维）
  - `LimitMoveDetector` 涨跌停加速：封板松动/撬板信号/逼近加速
  - `TurnoverDetector` 换手率异动：自身MAD基线，5分钟增量/全天累计
  - `TransBigDetector` 逐笔大单：独立3秒线程 + 预筛选 + 2000万下限 + 动态基线
  - `RankChangeDetector` 排名突变：自选池81只，跃升>30位或涨幅突变
- **AnomalyAlert + SimpleQueue**：新告警值对象，与原有 `Alert` 并行不冲突
- **五档盘口全量**：`QuoteSnapshot` 扩展至 33 字段，含 bid/ask 1-5 价+量 + last_close
- **ANOMALY_DETECTOR_CONFIG**：6 模块所有阈值均可配置
- **14条单元测试**：`tests/unit/test_anomaly_detectors.py` 全覆盖
- **设计文档**：`docs/anomaly_detection_design.md` 含 Q1-Q8 完整决策记录

### 修复
- TDX 数据源 (`tdx_source.py`)：新增 `get_stock_info`（原基类返回空 `{}` 导致回退 AKShare）
- TDX 数据源：添加 `_safe_decode` 修复 pytdx GBK 名称编码
- 实时行情：从本地 DB 缓存填充 `name` 字段（pytdx quotes 不返回名称）
- `db_api.py`：分钟数据查询日期格式修复（`date_fmt` 未使用导致 LIKE 不匹配）
- `config/settings.py`：数据源改回 `primary: tdx, fallback: akshare`

### 前端
- 新告警类型渲染：7 种类型标签 + 颜色 + 详情自适应
- 新增筛选按钮：逐笔/盘口/背离/换手/涨跌
- `web/monitor_demo.html`：示范页面

### 数据统计
- 81只股票日线 48,075 条（2024-01 ~ 2026-06）
- 5分钟K线 64,800 条 / 1分钟分时 349,920 条
- 流通股本缓存 81 只 / 换手率历史中位数 81 只
- 交易日历 9,402 天

---

## v2.3.0 (2026-06-17)

### 新增
- **大单监控 MAD 动态基线**：替代固定阈值，每只股票独立维护 60 秒滑窗
  - 中位数绝对偏差 (MAD) 自适应流动性：高流动股自动提高阈值，低流动股自动降低
  - 双重判定：`delta/median` 比 AND `(delta-median)/MAD` 比 同时达标
  - 三级告警：超大单(5×med + 10×MAD) / 大单(3×med + 6×MAD) / 放量(2×med + 4×MAD)
- **东方财富资金流双通道**：`EastMoneyFundPoller` 分钟级超大单/大单/中单/小单分类
  - TDX 量价异动 + 东方财富订单规模交叉验证
- **盯盘板块资金流**：`SectorHeatmap` 增强，计算板块均涨幅/主力净流入/领涨股
- **启动自动备份恢复**：`server.py` 启动时自动备份 5 个运行时状态文件
  - 丢失时自动从最新备份恢复，30 天旧备份自动清理

### 修复
- 涨跌停扫描：TDX 批量查询返回 None 导致整批数据丢失 + 股票名称缺失
  - `_scan_batches` 增加空值检查，`_get_full_stock_list` 同步建立全市场 1863 只代码→名称映射
- 板块热点：板块名称 GBK 编码损坏导致成分股聚合失败
- 大单监控：原始固定阈值 (500万/5s AND 3×量比) 过于严格，累计告警为 0
- 运行时状态文件被 `git pull --rebase` 物理删除
  - `git rm --cached` 移出跟踪，`.gitignore` 简化为 `data_cache/`
- 防刷屏：60s 同股票冷却 + 每轮最多 10 条 + 前端中性方向过滤

### 改造
- `broker/monitor.py`：Detector 全重写为 MAD 滑窗，MonitorEngine 双线程架构
- `broker/market_watcher.py`：SectorHeatmap 增加 `_enrich_with_quotes()`，LimitUpDownWatcher 名称缓存
- `server.py`：新增 `restore_runtime_state_if_needed()` + `backup_runtime_state()`
- `web/live.html`：大单告警渲染改为级别标签 + MAD 倍数 + 资金流验证信息
- `config/settings.py`：API_KEY 支持 `QUANT_API_KEY` 环境变量
- `data/fetcher.py`：新增 `_parse_ts_code()` / `_volume_series()` 工具方法

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
