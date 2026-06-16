# 大单监控模块 — 详细设计文档

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                          server.py                                   │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────────┐  │
│  │POST /monitor │  │GET /monitor   │  │GET /monitor/stream (SSE)  │  │
│  │  /start     │  │  /history     │  │                            │  │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬─────────────────┘  │
│         │                │                      │                    │
│         └────────────────┼──────────────────────┘                    │
│                          │                                           │
│                 ┌────────┴────────┐                                  │
│                 │  MonitorEngine   │  (单例，后台线程)                 │
│                 │  broker/monitor  │                                  │
│                 └────────┬────────┘                                  │
│                          │                                           │
│          ┌───────────────┼───────────────┐                           │
│          │               │               │                           │
│   ┌──────┴──────┐ ┌─────┴──────┐ ┌──────┴──────┐                    │
│   │ TDXQuotesPoller│ │Detector  │ │SSEManager  │                    │
│   │ (数据采集)    │ │(大单判定) │ │(消息推送)  │                    │
│   └──────┬──────┘ └───────────┘ └──────┬──────┘                    │
│          │                              │                           │
│   pytdx TCP                              ├── EventSource → live.html │
│   60.191.117.167:7709                   │                           │
└──────────────────────────────────────────┴───────────────────────────┘
```

**职责分离**：

| 模块 | 文件 | 职责 |
|------|------|------|
| `TDXQuotesPoller` | `broker/monitor.py` | 通过 pytdx 逐只获取实时快照，返回结构化数据 |
| `Detector` | `broker/monitor.py` | 对比前后快照，根据阈值判定是否为大单 |
| `SSEManager` | `broker/monitor.py` | 管理 SSE 连接池，广播消息 |
| `MonitorEngine` | `broker/monitor.py` | 编排上述三个模块，管理生命周期（start/stop） |
| API 端点 | `server.py` | 薄层，转发到 MonitorEngine |
| 前端面板 | `web/live.html` | 开关 + SSE 接收 + 消息列表渲染 |

---

## 2. 模块详细设计

### 2.1 TDXQuotesPoller — 数据采集

```python
class TDXQuotesPoller:
    """
    通过 pytdx 逐只获取实时行情快照。

    设计要点：
    - 每次调用 poll(codes) 对列表中的股票逐一查询
    - 返回 Dict[str, QuoteSnapshot]，key 为纯数字代码
    - 单只失败不影响其他股票，失败的记录 warn 日志
    - 不在交易时段直接返回空（节约连接）
    """

    def __init__(self, servers: list[tuple[str, int]], connect_timeout: float = 5.0):
        self._servers = servers
        self._timeout = connect_timeout

    def poll(self, codes: list[str]) -> dict[str, "QuoteSnapshot"]:
        ...

    @staticmethod
    def is_trading_time() -> bool:
        """判断当前是否在 A 股交易时段（9:25-11:30, 13:00-15:00，周一至周五）"""
        ...


@dataclass
class QuoteSnapshot:
    """单只股票的快照"""
    code: str           # '600519'
    name: str           # '贵州茅台'
    price: float        # 现价
    open: float
    high: float
    low: float
    volume: float       # 累计成交量（股）
    amount: float       # 累计成交额（元）
    change_pct: float   # 涨跌幅 %
    bid1: float         # 买一价
    ask1: float         # 卖一价
    active_buy: float   # 外盘（主动买）
    active_sell: float  # 内盘（主动卖）
    time: str           # 数据时间 '14:32:15'
```

**并发策略**：逐只串行查询，因为 pytdx 的 `get_security_quotes` 一次连接查一只。81 只股票约需 5-8 秒（单只 ~60-100ms），刚好匹配 5 秒轮询间隔的偏移量。

**容错**：单只查询超时 5 秒，失败后 skip 继续下一只。连续 3 轮全部失败则判定连接断开，自动重连。

---

### 2.2 Detector — 大单判定

```python
class Detector:
    """
    大单检测器。

    判定分为两步：
    1. 量比检测 — 区间成交量是否远超历史同期均值
    2. 方向判定 — 根据价格变化和内外盘判断买入/卖出

    所有阈值均可通过构造函数或 set_thresholds() 调整。
    """

    def __init__(
        self,
        vol_ratio_threshold: float = 3.0,    # 量比阈值
        amount_min: float = 5_000_000,       # 最小成交额差（元），默认500万
        price_change_min: float = 0.3,       # 最小价格波动（%），用于方向判定
    ):
        self.vol_ratio = vol_ratio_threshold
        self.amount_min = amount_min
        self.price_change_min = price_change_min
        self._baselines: dict[str, float] = {}   # {code: 基准区间量}

    def set_thresholds(self, **kwargs):
        """运行时动态调整阈值"""
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def set_baselines(self, baselines: dict[str, float]):
        """
        设置每只股票的基准区间成交量（股/秒）。
        baselines 由外部计算（基于近20日同时段均值），传入此方法。
        """
        self._baselines = baselines

    def detect(
        self,
        code: str,
        prev: QuoteSnapshot | None,
        curr: QuoteSnapshot,
    ) -> "Alert | None":
        """
        对比前后两帧快照，判定是否触发大单告警。

        Args:
            code: 股票代码
            prev: 上一帧快照（首次为 None）
            curr: 当前帧快照

        Returns:
            Alert 对象（触发时）或 None（未触发）
        """
        ...

    def detect_batch(
        self,
        prev_snapshots: dict[str, QuoteSnapshot],
        curr_snapshots: dict[str, QuoteSnapshot],
    ) -> list["Alert"]:
        """批量检测，返回所有触发的告警"""
        ...
```

**判定算法详情**：

```
对于每只股票：
  interval_seconds = 本次时间 - 上次时间 (秒)
  delta_volume = curr.volume - prev.volume       # 区间成交量
  delta_amount = curr.amount - prev.amount       # 区间成交额
  price_change = (curr.price - prev.price) / prev.price * 100

  # 基准区间量 = 每秒基准量 × 间隔秒数
  baseline_vol = baselines.get(code, 0) * interval_seconds
  vol_ratio = delta_volume / baseline_vol if baseline_vol > 0 else 999

  if vol_ratio >= vol_ratio_threshold AND delta_amount >= amount_min:
      # 方向判定
      if price_change >= price_change_min:
          direction = BUY
      elif price_change <= -price_change_min:
          direction = SELL
      else:
          direction = UNKNOWN

      生成 Alert(code, direction, delta_volume, delta_amount, curr.price, curr.change_pct)
```

**基准量计算**（由 MonitorEngine 在 start 时调用一次）：

```
baseline[code] = 取该股近20个交易日中，当前时刻前后5分钟的日均成交量均值 / 300秒
```

---

### 2.3 SSEManager — 消息推送

```python
class SSEManager:
    """
    SSE 连接管理器。

    设计要点：
    - 每个 SSE 连接对应一个 asyncio.Queue
    - push() 广播到所有活跃连接
    - 客户端断开时自动清理队列
    - 定期心跳（30s），防止代理/负载均衡断开
    """

    def __init__(self, max_queue_size: int = 500):
        self._queues: list[asyncio.Queue] = []
        self._max_size = max_queue_size

    def subscribe(self) -> asyncio.Queue:
        """新客户端订阅，返回专属队列"""
        q = asyncio.Queue(maxsize=self._max_size)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """客户端断开时取消订阅"""
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    async def push(self, alert: "Alert"):
        """广播一条告警到所有连接"""
        dead = []
        for q in self._queues:
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                # 队列满了丢弃最旧的消息
                try:
                    q.get_nowait()
                    q.put_nowait(alert)
                except Exception:
                    pass
            except Exception:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)

    async def push_heartbeat(self):
        """发送心跳"""
        ...
```

---

### 2.4 MonitorEngine — 编排层

```python
class MonitorEngine:
    """
    大单监控引擎（单例）。

    生命周期：
    1. start(stock_pool) → 初始化基准量 → 启动后台轮询线程
    2. _poll_loop() → 每 5s 轮询 → 检测 → 推送
    3. stop() → 设置停止标志 → 等待线程结束 → 清理资源

    线程安全：
    - 轮询在 daemon 线程中执行
    - SSE 推送在 asyncio 事件循环中执行
    - Alert 对象是不可变值对象，天然线程安全
    - 状态变量使用 threading.Lock 保护
    """

    _instance = None

    @classmethod
    def get_instance(cls) -> "MonitorEngine":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._poller = TDXQuotesPoller(servers=[...])
        self._detector = Detector()
        self._sse = SSEManager()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._alerts: list[Alert] = []       # 历史告警（最多 500 条）
        self._prev_snapshot: dict = {}

    def start(self, stock_pool: list[str]):
        """启动监控"""
        with self._lock:
            if self._running.is_set():
                return
            self._stock_pool = list(stock_pool)
            self._running.set()
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self):
        """停止监控"""
        with self._lock:
            self._running.clear()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def get_history(self, limit: int = 100) -> list[Alert]:
        """获取历史告警"""
        return self._alerts[-limit:]

    def _poll_loop(self):
        """后台轮询主循环"""
        self._compute_baselines()

        while self._running.is_set():
            if not TDXQuotesPoller.is_trading_time():
                time.sleep(30)   # 非交易时段慢速检查
                continue

            try:
                curr = self._poller.poll(self._stock_pool)
            except Exception as e:
                logger.error(f"轮询失败: {e}")
                time.sleep(10)
                continue

            alerts = self._detector.detect_batch(self._prev_snapshot, curr)
            self._prev_snapshot = curr

            for alert in alerts:
                self._alerts.append(alert)
                if len(self._alerts) > 500:
                    self._alerts = self._alerts[-500:]
                # 推送到 SSE（跨线程调度到 asyncio）
                asyncio.run_coroutine_threadsafe(
                    self._sse.push(alert), main_event_loop
                )

            time.sleep(self._interval)
```

---

### 2.5 Alert — 告警值对象

```python
@dataclass(frozen=True)   # 不可变，线程安全
class Alert:
    code: str          # '600519'
    name: str          # '贵州茅台'
    direction: str     # 'buy' | 'sell' | 'unknown'
    volume: int        # 区间成交量（股）
    hands: int         # 区间成交量（手）= volume // 100
    amount: float      # 区间成交额（元）
    price: float       # 当前价格
    change_pct: float  # 价格变动 %
    time: str          # '14:32:15'
    timestamp: str     # ISO 格式 '2026-06-17T14:32:15'

    def to_sse_data(self) -> str:
        """序列化为 SSE 的 data 字段（JSON）"""
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)
```

---

## 3. API 端点设计

所有端点前缀：`/api/monitor`

### 3.1 `POST /api/monitor/start`

```
Request:  { "stock_pool": ["600519", "000333", ...] }
           # 不传则使用当前实盘股票池

Response: { "status": "started", "stock_count": 81 }

Error:    400 - 已在运行中
          500 - 启动失败
```

### 3.2 `POST /api/monitor/stop`

```
Response: { "status": "stopped", "total_alerts": 42 }

Error:    400 - 当前未在运行
```

### 3.3 `GET /api/monitor/status`

```
Response: {
    "running": true,
    "stock_count": 81,
    "total_alerts": 42,
    "started_at": "2026-06-17T09:30:00",
    "last_poll_at": "2026-06-17T14:32:15",
    "interval_seconds": 5
}
```

### 3.4 `GET /api/monitor/history`

```
Query:    ?limit=50  (可选，默认 100)

Response: {
    "alerts": [ Alert, Alert, ... ],
    "total": 42
}
```

### 3.5 `GET /api/monitor/stream`（SSE）

```
Content-Type: text/event-stream

事件格式：
  event: alert
  data: {"code":"600519","name":"贵州茅台","direction":"buy","hands":1250,...}

  event: heartbeat
  data: {"time":"14:32:30"}

  event: status
  data: {"running":true,"stock_count":81}

客户端重连：EventSource 浏览器原生支持自动重连。
```

---

## 4. 前端集成（live.html）

### 4.1 布局位置

在现有状态栏和控制栏之间，新增一行监控专用栏：

```
┌─ statusBar ─────────────────────────────────────┐
│  ● 未启动  |  券商: --  |  模式: --  | ...       │
└─────────────────────────────────────────────────┘
┌─ monitorBar（新增）──────────────────────────────┐
│  📡 大单监控  [▶ 开启] [⏹ 停止]    共 42 条告警 │
└─────────────────────────────────────────────────┘
┌─ 控制栏（现有）──────────────────────────────────┐
│  [启动实盘] [停止] [重置]  ...                    │
└─────────────────────────────────────────────────┘
```

### 4.2 消息面板

在现有 tabs 区域新增一个 "📡 大单监控" tab，与持仓/信号并列：

```
┌─ tabs ──────────────────────────────────────────┐
│ [信号] [持仓] [订单] [📡 大单监控]               │
└─────────────────────────────────────────────────┘
┌─ tab-content（监控面板）─────────────────────────┐
│  [清空] [仅买入] [仅卖出] [全部]                  │
│                                                   │
│  🟢 14:32:15  贵州茅台  大单买入                  │
│     1,250手  成交1,935万  现价1,548.32 ↑1.2%      │
│  ───────────────────────────────────────          │
│  🔴 14:32:18  宁德时代  大单卖出                  │
│     850手  成交1,276万  现价218.50 ↓0.8%          │
│  ───────────────────────────────────────          │
│  ...（最多200条，滚动）                            │
└─────────────────────────────────────────────────┘
```

### 4.3 交互逻辑

```
页面加载：
  1. 创建 EventSource('/api/monitor/stream')
  2. 监听 'alert' 事件 → 追加到消息列表顶部
  3. 监听 'status' 事件 → 更新开关按钮状态
  4. 调用 GET /api/monitor/status 同步初始状态

开启监控：
  1. 用户点击 [▶ 开启]
  2. POST /api/monitor/start (body: 当前实盘股票池)
  3. 按钮变为 [⏹ 停止]（绿色脉冲动画）
  4. SSE 开始推送告警

关闭监控：
  1. 用户点击 [⏹ 停止]
  2. POST /api/monitor/stop
  3. 按钮恢复 [▶ 开启]（灰色）

消息过滤：
  三个快速筛选按钮：全部 / 仅买入 / 仅卖出
  前端本地过滤，不额外请求
```

### 4.4 样式规范

复用 live.html 现有 CSS 变量和 class：
- 卡片使用 `.card` + `.card-header`
- 买入消息左边框 `var(--green)`，买入标签使用 `.badge-live`
- 卖出消息左边框 `var(--red)`，卖出标签使用 `.badge-danger`
- 按钮使用 `.btn .btn-primary` / `.btn .btn-danger`
- 监控开关激活状态使用脉冲动画（可选）

---

## 5. 文件清单

| 文件 | 操作 | 预计行数 |
|------|------|----------|
| `broker/monitor.py` | **新建** | ~350 行 |
| `server.py` | 修改 — 新增 5 个端点 + 导入 MonitorEngine | +60 行 |
| `web/live.html` | 修改 — 新增监控栏 + 消息面板 tab | +150 行 |

---

## 6. 配置与可调参数

所有可调参数集中在 `MonitorEngine` 和 `Detector` 的构造参数中，后续可通过 API 暴露（二期）：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `poll_interval` | 5 | 轮询间隔（秒） |
| `vol_ratio_threshold` | 3.0 | 量比阈值（实际量 / 基准量） |
| `amount_min` | 5,000,000 | 最小成交额差（元） |
| `price_change_min` | 0.3 | 方向判定最小价格波动（%） |
| `max_alerts` | 500 | 最大历史告警数 |
| `baseline_days` | 20 | 基准量计算用的历史天数 |
| `tdx_servers` | 复用 settings.py | TDX 服务器地址 |

---

## 7. 后续扩展点

- **阈值 API**：`PUT /api/monitor/thresholds` 运行时调整参数
- **声音告警**：大单触发时播放提示音
- **桌面通知**：Web Notification API
- **持久化**：告警写入 SQLite，支持历史回溯
- **统计面板**：当日大单买入/卖出汇总，净买入量
