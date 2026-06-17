# A股量化交易系统

## 项目简介

完整的A股量化交易系统，支持**回测**和**实盘**两种模式。整合 13 个策略，提供 Web 可视化界面和 REST API。

### 核心特点

- **双数据源**：TDX(通达信) 为主，AKShare 为备，自动降级
- **5档盘口**：实盘下单前估算真实成交价和流动性
- **分钟线回测**：VWAP 真实滑点，替代固定滑点率
- **结构化日志**：JSON 格式，按模块分文件，自动轮转
- **风控系统**：日亏损/连续亏损/最大回撤三级熔断
- **信号总线**：多策略信号收集→去重→过滤→排序→分配
- **完整回测引擎**：T+1、涨跌停、佣金/印花税/滑点、strict_mode 消除前视偏差
- **Web 界面**：FastAPI + 数据库浏览器 + 实盘面板

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动 Web 服务

```bash
python server.py
```

浏览器访问 http://localhost:8000

### 3. 实盘交易（可选）

```bash
python live_server.py --broker sim --mode semi --interval 60
```

参数：
- `--broker`：券商（sim / qmt / ths）
- `--mode`：auto（全自动）/ semi（半自动）
- `--interval`：扫描间隔（秒）
- `--oneshot`：单次扫描后退出

---

## 项目结构

```
My_trading1/
│
├── server.py                    # FastAPI Web 服务入口
├── live_server.py               # 实盘交易服务
├── requirements.txt
│
├── config/                      # 配置模块
│   ├── settings.py              # 全局配置（数据源/回测/风控/因子权重）
│   └── strategy_profiles.py     # 策略画像（牛/熊/中性市场）
│
├── data/                        # 数据层
│   ├── fetcher.py               # DataFetcher 门面（多数据源自动降级）
│   ├── database.py              # SQLiteManager（11张表，WAL模式）
│   ├── sync_service.py          # DataSyncService 定时同步
│   ├── validator.py             # 数据验证
│   ├── calendar.py              # 交易日历
│   └── sources/                 # 数据源抽象层
│       ├── base.py              # BaseDataSource ABC
│       ├── tdx_source.py        # TDXDataSource (pytdx)
│       ├── akshare_source.py    # AKShareDataSource
│       └── __init__.py          # 注册表 + 工厂函数
│
├── strategy/                    # 策略模块（13个策略）
│   ├── base.py                  # BaseStrategy ABC
│   ├── eight_factor.py          # 8因子选股
│   ├── position_strategy.py     # 位置判断
│   ├── trend_following.py       # 趋势跟随
│   ├── mean_reversion.py        # 均值回归
│   ├── low_volatility.py        # 低波动
│   ├── momentum_strategy.py     # 动量/反转/价值/质量
│   ├── sector_rotation.py       # 行业轮动
│   ├── intraday_reversal.py     # 日内反转
│   └── ai_strategy.py           # AI策略 (LightGBM)
│
├── backtest/                    # 回测模块
│   ├── engine.py                # BacktestEngine（日频 + strict_mode）
│   ├── matcher.py               # MatchEngine（撮合成交）
│   ├── performance.py           # 绩效评估
│   └── minute_executor.py       # MinuteBarExecutor（VWAP执行）
│
├── broker/                      # 交易执行
│   ├── base.py                  # BaseBroker ABC + 数据类型
│   ├── sim_broker.py            # 模拟券商
│   ├── qmt_broker.py            # QMT 实盘
│   ├── ths_broker.py            # 同花顺
│   ├── manual_broker.py         # 手动券商（东方财富）
│   ├── risk_manager.py          # RiskManager 风控
│   ├── executor.py              # TradeChecklist 交易清单
│   ├── monitor.py               # 大单监控
│   ├── order_book.py            # OrderBookEstimator 盘口估算
│   ├── notify.py                # 信号通知
│   └── __init__.py              # 注册表 + 工厂
│
├── sigbus/                      # 信号总线
│   ├── bus.py                   # SignalBus（收集/去重/过滤/排序）
│   └── filters.py               # SignalFilters（7项检查）
│
├── factors/                     # 因子计算
│   └── engine.py                # 8因子计算引擎
│
├── optimizer/                   # 参数优化
│   ├── search.py                # 网格/随机/贝叶斯搜索
│   └── validator.py             # 过拟合验证
│
├── analysis/                    # 市场分析
│   ├── market_regime.py         # 市场环境检测
│   └── ai_analyzer.py           # AI分析
│
├── utils/                       # 工具
│   ├── logger.py                # 统一日志 (JsonFormatter + get_logger)
│   └── indicators.py            # 技术指标
│
├── web/                         # Web 前端
│   ├── api.py                   # REST API
│   ├── db_api.py                # 数据库 API
│   ├── kline_api.py             # K线 API
│   ├── db_explorer.html         # 数据库浏览器
│   └── live.html                # 实盘面板
│
├── scripts/                     # 脚本
│   ├── daily_sync.py            # 每日数据同步
│   └── daily_report.py          # 每日报告
│
├── docs/                        # 文档
│   ├── logging_design.md        # 日志系统设计
│   ├── tdx_integration_design.md # TDX集成设计
│   ├── tdx_execution_plan.md    # TDX执行方案
│   ├── large_order_monitor_design.md # 大单监控设计
│   └── system_audit_report.md   # 系统审计报告
│
├── tests/                       # 测试
└── data_cache/                  # 数据缓存（运行时生成）
```

---

## 数据源

| 数据源 | 速度 | 数据 | 用途 |
|--------|------|------|------|
| **TDX (通达信)** | 毫秒级 | 日线/分钟线/5档盘口/分笔/除权 | 主数据源 |
| **AKShare** | 秒级 | 资金流向/北向资金/龙虎榜/财务 | 备用 + 补充 |

配置：`config/settings.py` → `DATA_SOURCE_CONFIG`

---

## 日志系统

```
logs/
├── server.log          ← Web 服务（文本格式）
├── live_trading.log    ← 实盘交易（JSON 格式）
├── backtest.log        ← 回测执行（JSON 格式）
├── risk.log            ← 风控判断（JSON 格式）
├── data.log            ← 数据获取（JSON 格式）
└── error.log           ← 全局错误汇总
```

API 查看：`GET /api/logs?module=backtest&level=ERROR&limit=100`

---

## 数据库（11 张表）

| 表 | 说明 |
|----|------|
| `daily_bars` | 日K线 |
| `minute_bars` | 分钟K线 |
| `fundamentals` | 基本面（ROE/毛利率/增速等） |
| `finance_detail` | 扩展财务（37字段，TDX来源） |
| `stock_info` | 股票基本信息 |
| `trade_calendar` | 交易日历 |
| `xdxr` | 除权除息 |
| `block_info` | 板块成分股 |
| `data_log` | 数据同步日志 |
| `account_snapshot` | 账户快照 |

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/backtest` | 提交回测任务 |
| GET | `/api/tasks` | 查看任务列表 |
| GET | `/api/tasks/{id}` | 查看任务状态 |
| GET | `/api/results/{id}` | 查看回测结果 |
| DELETE | `/api/tasks/{id}` | 删除任务 |
| GET | `/api/logs` | 查看日志（支持 module/level/keyword） |
| POST | `/api/data/sync` | 数据同步（source=cache/tdx） |
| GET | `/api/kline/{ts_code}` | K线数据 |
| GET | `/api/db/tables` | 数据库表列表 |
| GET | `/api/db/table/{name}` | 表数据查询 |

---

## 策略说明

### 日频策略（12个）

| 策略 | 类型 | 核心字段 |
|------|------|----------|
| EightFactor | 多因子 | EP/增速/反转/换手/波动/ROE/应计 |
| PositionStrategy | 位置判断 | 均线/分位/基本面/消息面 |
| TrendFollowing | 趋势 | ma20/ma60 |
| MeanReversion | 反转 | return_20d/ma20 |
| LowVolatility | 低波动 | volatility/ROE/EP |
| Momentum | 动量 | return_20d |
| Value | 价值 | EP/ROE |
| Quality | 质量 | ROE/gross_margin |
| SectorRotation | 行业轮动 | industry/return_20d/turnover |
| AIStrategy | AI | LightGBM ~50特征 |
| IntradayReversal | 日内 | 5分钟K线形态 |

---

## 风控体系

| 层级 | 检查项 |
|------|--------|
| 信号过滤 | 日亏损 / 黑名单 / 涨跌停 / 持仓数 / 权重 / 金额 |
| 订单风控 | 交易暂停 / 标的风控 / 仓位 / 资金 / 亏损 / 回撤 |
| 熔断 | 日亏损触发 → 交易暂停 / 连续亏损 → 暂停 / 最大回撤 → 暂停 |

---

## 免责声明

本项目仅供学习和研究使用，不构成任何投资建议。股市有风险，投资需谨慎。
