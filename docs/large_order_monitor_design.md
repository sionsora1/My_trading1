# 大单监控模块 — 详细设计文档

## 1. 架构概览

```
┌──────────────────────────────────────────────────────────────────────────┐
│                            server.py                                      │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────────┐       │
│  │POST /monitor │  │GET /monitor   │  │GET /monitor/stream (SSE)  │       │
│  │  /start     │  │  /history     │  │                            │       │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬─────────────────┘       │
│         │                │                      │                         │
│         └────────────────┼──────────────────────┘                         │
│                          │                                                │
│                 ┌────────┴────────┐                                       │
│                 │  MonitorEngine   │  (单例，双后台线程)                    │
│                 │  broker/monitor  │                                       │
│                 └────────┬────────┘                                       │
│                          │                                                │
│     ┌────────────────────┼────────────────────┐                           │
│     │                    │                    │                           │
│  ┌──┴──────────┐  ┌──────┴──────┐  ┌─────────┴──────┐                    │
│  │TDXQuotesPoller│  │EastMoneyFund│  │  Detector     │                    │
│  │ 实时价量(5s)  │  │Poller(60s) │  │  MAD动态基线  │                    │
│  └──────┬──────┘  └──────┬──────┘  └──────┬────────┘                    │
│         │                │                │                               │
│   pytdx TCP        东方财富HTTP      60s滑窗 × 每只股票                    │
│         │                │                │                               │
│         └────────────────┼────────────────┘                               │
│                          │                                                │
│                    ┌─────┴──────┐                                         │
│                    │SSEManager  │                                         │
│                    │ (消息推送) │                                         │
│                    └─────┬──────┘                                         │
│                          │                                                │
│                    EventSource → live.html                                │
└──────────────────────────────────────────────────────────────────────────┘
```

**双通道检测**：

| 通道 | 数据源 | 频率 | 用途 |
|------|--------|------|------|
| 通道1 — TDX | pytdx 实时行情 | 每 5s | 价格/成交量/内外盘 → MAD 滑窗 → 量价异动 |
| 通道2 — 东方财富 | push2.eastmoney.com | 每 60s | 分钟级超大单/大单/中单/小单净流入 → 交叉验证 |

---

## 2. 核心算法：MAD 动态基线

### 2.1 设计动机

固定阈值（如"5秒成交额500万"）对不同流动性股票完全不适用：
- 中际旭创日成交300亿，5秒500万是噪音
- 小盘股日成交5000万，5秒10万就是大单

MAD（中位数绝对偏差）让每只股票自己跟自己比，无需人工设定。

### 2.2 算法流程

```
初始化（每只股票独立维护）:
  window = deque(maxlen=12)        # 60 秒滑窗（12 × 5s）
  baseline_rate = 近20日日均成交量 / 14400   # 冷启动基准

每轮轮询:
  for code in stock_pool:
      delta_amt = curr.amount - prev.amount    # 本轮成交额增量
      delta_vol = curr.volume - prev.volume     # 本轮成交量增量
      delta_buy = curr.active_buy - prev.active_buy
      delta_sell = curr.active_sell - prev.active_sell

      window[code].append(delta_amt)            # 喂入滑窗

      if len(window[code]) >= 12:               # 数据足够
          median = window[code] 的中位数
          mad = median(|delta - median|)         # 中位数绝对偏差
          median_ratio = delta_amt / median
          mad_multiple = (delta_amt - median) / mad

          # 双重阈值判定
          if median_ratio >= 5 AND mad_multiple >= 10 → super_large
          if median_ratio >= 3 AND mad_multiple >= 6  → large
          if median_ratio >= 2 AND mad_multiple >= 4  → volume_spike

      if len(window[code]) < 12:                # 冷启动
          用历史 baseline 兜底
```

### 2.3 阈值参数

| 级别 | median 倍数 | MAD 倍数 | 含义 |
|------|------------|----------|------|
| 🔴 超大单 | ≥ 5× | ≥ 10× | 极端异常，几乎可以确定是大资金 |
| 🟡 大单 | ≥ 3× | ≥ 6× | 显著异常放量 |
| ⚪ 放量 | ≥ 2× | ≥ 4× | 轻度异动，值得关注 |

### 2.4 方向判定

不依赖微小的价格变化，改用主动买卖盘占比：

```
buy_ratio = delta_active_buy / (delta_active_buy + delta_active_sell)
buy_ratio > 0.7 → buy
buy_ratio < 0.3 → sell
否则 → neutral
```

### 2.5 东方财富交叉验证

MAD 检测到异动后，查询该股最新一分钟资金流分类数据：
- 超大单净流入 + 大单净流入 同向 → 确认告警
- 中单/小单主导 → 降级为放量

### 2.6 防刷屏机制

- 同只股票触发后 **60 秒冷却期**
- 每轮最多推送 **10 条**，按 MAD 倍数降序
- 非交易时段自动休眠（30s 慢速检查）

---

## 3. 模块详细设计

### 3.1 Alert — 告警值对象

```python
@dataclass(frozen=True)
class Alert:
    code: str              # '600519'
    name: str              # '贵州茅台'
    direction: str         # 'buy' | 'sell' | 'neutral'
    level: str             # 'super_large' | 'large' | 'volume_spike'
    volume: int            # 区间成交量（股）
    hands: int             # 区间成交量（手）
    amount: float          # 区间成交额（元）
    price: float           # 当前价格
    change_pct: float      # 区间价格变动 %
    mad_multiple: float    # 偏离 MAD 倍数
    super_large_flow: float  # 超大单净流入（东方财富，元/分钟）
    large_flow: float        # 大单净流入（东方财富，元/分钟）
    time: str              # '14:32:15'
    timestamp: str         # ISO 格式
```

### 3.2 EastMoneyFundPoller — 东方财富数据采集

```python
class EastMoneyFundPoller:
    """
    从东方财富拉取分钟级资金流向数据。

    限流策略: 每次请求间隔 ≥ 3 秒，全量 81 只约 4 分钟。
    数据格式（每分钟）:
        time / 主力净流入 / 小单净流入 / 中单净流入 / 大单净流入 / 超大单净流入

    API: push2.eastmoney.com/api/qt/stock/fflow/kline/get
    """

    def fetch(self, code: str) -> dict | None: ...
    def fetch_all(self, codes: list[str]) -> dict[str, dict]: ...
    def get_cached(self, code: str) -> dict | None: ...
```

### 3.3 Detector — MAD 动态基线检测器

```python
class Detector:
    WINDOW_SIZE = 12       # 60秒 / 5秒间隔
    COOLDOWN_SEC = 60      # 同股票冷却时间

    # 双重阈值
    SUPER_LARGE_MEDIAN_RATIO = 5.0    SUPER_LARGE_MAD = 10.0
    LARGE_MEDIAN_RATIO = 3.0          LARGE_MAD = 6.0
    SPIKE_MEDIAN_RATIO = 2.0          SPIKE_MAD = 4.0

    def feed(self, code, delta_amt, delta_vol, delta_buy, delta_sell): ...
    def check(self, code, delta_amt, delta_vol, delta_buy, delta_sell) -> (level, mad_multiple) | None: ...
    def mark_alerted(self, code): ...
    def set_history_baselines(self, baselines): ...
```

### 3.4 MonitorEngine — 编排层

```python
class MonitorEngine:
    """单例。双线程：TDX轮询(5s) + 东方财富轮询(60s)。"""

    def start(self, stock_pool):
        # 1. 加载股票名称
        # 2. 计算历史冷启动基线
        # 3. 启动 TDX 轮询线程 _poll_loop()
        # 4. 启动东方财富轮询线程 _fund_flow_loop()

    def _poll_loop(self):
        # 1. TDXQuotesPoller.poll() → 快照
        # 2. 计算 delta → Detector.feed()
        # 3. 预热 3 轮
        # 4. Detector.check() → 候选告警
        # 5. 查 EastMoneyFundPoller.get_cached() 交叉验证
        # 6. 排序 + 冷却 + 限流 → SSE 推送

    def _fund_flow_loop(self):
        # EastMoneyFundPoller.fetch_all() → 更新缓存
```

---

## 4. API 端点

与 v2.2 保持一致，Alert 字段有所扩展：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/monitor/start` | 启动监控（可选 body: `{stock_pool: [...]}`） |
| POST | `/api/monitor/stop` | 停止监控 |
| GET | `/api/monitor/status` | 运行状态 |
| GET | `/api/monitor/history?limit=100` | 历史告警 |
| GET | `/api/monitor/stream` | SSE 实时推送 |

---

## 5. 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `_interval` | 5 | TDX 轮询间隔（秒） |
| `_fund_interval` | 60 | 东方财富轮询间隔（秒） |
| `WINDOW_SIZE` | 12 | MAD 滑窗数据点数 |
| `COOLDOWN_SEC` | 60 | 同股票冷却时间（秒） |
| `_max_per_cycle` | 10 | 每轮最大推送数 |
| `_max_alerts` | 500 | 内存历史告警上限 |
| `_baseline_days` | 20 | 冷启动历史天数 |
| EastMoney `_min_interval` | 3.0 | 东方财富请求间隔（秒） |

---

## 6. 前端渲染

告警按级别着色：

```
🔴 超大单 | 贵州茅台(600519) 🟢买入 | 1250手 / 1935万 | 36.7×MAD | 超大单:+1.2亿 大单:-0.3亿
🟡 大单   | 宁德时代(300750) 🔴卖出 | 850手 / 1276万  | 7.8×MAD  | 超大单:-0.8亿 大单:+0.1亿
⚪ 放量   | 兆易创新(603986) 🟡中性 | 200手 / 96万    | 4.6×MAD  | 超大单:-- 大单:--
```

过滤按钮：全部 / 仅买入 / 仅卖出 / 中性。

---

## 7. 文件清单

| 文件 | 说明 |
|------|------|
| `broker/monitor.py` | TDXQuotesPoller + EastMoneyFundPoller + Detector + SSEManager + MonitorEngine |
| `server.py` | 5 个 API 端点 + MonitorEngine 单例初始化 |
| `web/live.html` | 监控栏 + 消息面板 + SSE 接收 + 过滤渲染 |
