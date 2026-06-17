# 盯盘模块 — 详细设计文档

> 版本: v1.0 | 日期: 2026-06-17 | 基于需求规格 v0.1

---

## 1. 架构概览

```
server.py
    │
    ├── /api/watcher/start|stop|status  (REST)
    ├── /api/watcher/stream             (SSE)
    │
    ▼
broker/market_watcher.py
    │
    ├── MarketWatcherEngine (单例, 编排生命周期)
    │   ├── _poller: TDXQuotesPoller   ← 与 MonitorEngine 共享
    │   ├── _pool_tracker: PoolStatusTracker
    │   ├── _surge_watcher: SurgeWatcher
    │   ├── _flow_watcher: FlowWatcher
    │   ├── _limit_watcher: LimitUpDownWatcher
    │   ├── _sector_heatmap: SectorHeatmap
    │   └── _sse: SSEManager
    │
    └── 数据流
        poll (3s) → 快照分发 → 5个子模块并行处理 → 告警 → SSE + 日志
```

**设计原则：**
- 每个子模块独立一个类，单一职责
- 共享 TDXQuotesPoller 实例，避免重复连接
- 所有告警统一走 SSE 推送（复用 monitor.py 的 SSEManager）
- 日志遵循项目规范：`get_logger('live_trading', 'live_trading.log')`，结构化 JSON

---

## 2. MarketWatcherEngine — 编排引擎

```python
class MarketWatcherEngine:
    """盯盘引擎（单例）— 编排5个子模块，管理生命周期"""

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "MarketWatcherEngine":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        if hasattr(self, "_poller"):
            return  # 单例防重复初始化
        self._poller = TDXQuotesPoller()
        self._sse = SSEManager()
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._stock_pool: List[str] = []
        self._interval: float = 3.0

        # 5个子模块
        self._pool_tracker = PoolStatusTracker()
        self._surge_watcher = SurgeWatcher()
        self._flow_watcher = FlowWatcher()
        self._limit_watcher = LimitUpDownWatcher()
        self._sector_heatmap = SectorHeatmap()

        # SSE事件循环引用
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    # ---- 生命周期 ----

    def start(self, stock_pool: List[str]) -> dict:
        with self._lock:
            if self._running.is_set():
                return {"status": "already_running"}
            self._stock_pool = list(stock_pool)
            self._running.set()
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="market-watcher"
            )
            self._thread.start()
            logger.info("MarketWatcher 已启动", extra={
                "data": {"stock_count": len(stock_pool)}
            })
            return {"status": "started", "stock_count": len(stock_pool)}

    def stop(self) -> dict:
        with self._lock:
            if not self._running.is_set():
                return {"status": "not_running"}
            self._running.clear()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        logger.info("MarketWatcher 已停止")
        return {"status": "stopped"}

    # ---- 轮询循环 ----

    def _poll_loop(self) -> None:
        """3秒轮询 → 快照分发给5个子模块"""
        logger.info("MarketWatcher 轮询循环启动")

        while self._running.is_set():
            if not TDXQuotesPoller.is_trading_time():
                time.sleep(30)
                continue

            try:
                curr = self._poller.poll(self._stock_pool)
            except Exception as e:
                logger.error(f"轮询失败: {e}", exc_info=True)
                time.sleep(10)
                continue

            if not curr:
                time.sleep(self._interval)
                continue

            # 并行处理：5个子模块各自消费同一份快照
            pool_alerts = self._pool_tracker.process(curr)
            surge_alerts = self._surge_watcher.process(curr)
            flow_alerts = self._flow_watcher.process(curr)
            limit_alerts = self._limit_watcher.process()

            # 板块热度每30秒刷新一次（不需要3秒刷新）
            sector_data = self._sector_heatmap.process()

            # 汇总推送
            all_alerts = pool_alerts + surge_alerts + flow_alerts + limit_alerts
            self._push_alerts(all_alerts)
            self._push_sector(sector_data)

            time.sleep(self._interval)

        logger.info("MarketWatcher 轮询循环退出")

    def _push_alerts(self, alerts: list) -> None:
        """将告警通过 SSE 推送到前端"""
        loop = self._event_loop
        if loop is None or not loop.is_running():
            return
        for alert in alerts:
            asyncio.run_coroutine_threadsafe(self._sse.push(alert), loop)
```

---

## 3. 子模块详细设计

### 3.1 PoolStatusTracker — 自选股状态

**职责：** 每 3 秒更新自选股实时状态，供前端面板展示。

**不生产告警，只生产快照数据。** 通过 SSE 推送 `pool_snapshot` 事件，前端整体刷新。

```python
@dataclass(frozen=True)
class PoolSnapshot:
    """自选股状态快照"""
    stocks: List[dict]  # [{code, name, price, change_pct, volume, amount, bid1, ask1, high, low}, ...]
    timestamp: str

class PoolStatusTracker:
    """自选股状态追踪器"""

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        """
        输入: poll 快照 {code: QuoteSnapshot}
        输出: [SSE事件] 仅1条 pool_snapshot
        """
        stocks = []
        for code, snap in curr.items():
            stocks.append({
                "code": code,
                "name": snap.name,
                "price": snap.price,
                "change_pct": snap.change_pct,
                "volume": snap.volume,
                "amount": snap.amount,
                "bid1": snap.bid1,
                "ask1": snap.ask1,
                "high": snap.high,
                "low": snap.low,
            })
        # 按涨跌幅降序排列
        stocks.sort(key=lambda x: x["change_pct"], reverse=True)
        return [{"type": "pool_snapshot", "data": stocks, "timestamp": datetime.now().isoformat()}]
```

**日志：** 不单独记日志（数据量大且高频），前端刷新即可。

---

### 3.2 SurgeWatcher — 异动拉升

**职责：** 检测 1 分钟内涨幅/跌幅超阈值的股票，产出告警。

**机制：** 维护 60 秒滑动窗口的价格历史。每次 poll 后比较最新价与 60 秒前价格。

```python
class SurgeWatcher:
    """异动拉升/跳水检测"""

    def __init__(self, surge_up_pct: float = 1.5, surge_down_pct: float = -1.5):
        self.surge_up_pct = surge_up_pct      # 拉升阈值
        self.surge_down_pct = surge_down_pct  # 跳水阈值
        self._price_history: Dict[str, list] = {}  # code → [(ts, price), ...]

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        alerts = []
        now = time.time()

        for code, snap in curr.items():
            if code not in self._price_history:
                self._price_history[code] = []
            history = self._price_history[code]
            history.append((now, snap.price))
            # 清理60秒外的数据
            history[:] = [(t, p) for t, p in history if now - t <= 60]

            if len(history) < 2:
                continue

            first_price = history[0][1]
            if first_price <= 0:
                continue

            pct = (snap.price - first_price) / first_price * 100

            if pct >= self.surge_up_pct:
                alerts.append({
                    "type": "surge_up",
                    "code": code,
                    "name": snap.name,
                    "price": snap.price,
                    "change_pct": round(pct, 2),
                    "from_price": first_price,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("异动拉升", extra={"data": {
                    "code": code, "name": snap.name,
                    "change_pct": round(pct, 2), "from": first_price, "to": snap.price,
                }})
                # 触发后清空窗口，避免重复告警
                self._price_history[code] = []

            elif pct <= self.surge_down_pct:
                alerts.append({
                    "type": "surge_down",
                    "code": code,
                    "name": snap.name,
                    "price": snap.price,
                    "change_pct": round(pct, 2),
                    "from_price": first_price,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("异动跳水", extra={"data": {
                    "code": code, "name": snap.name,
                    "change_pct": round(pct, 2), "from": first_price, "to": snap.price,
                }})
                self._price_history[code] = []

        return alerts
```

---

### 3.3 FlowWatcher — 主力资金流

**职责：** 利用 TDX 内外盘数据，追踪主动买/卖净流向。

**机制：** 维护 60 秒窗口的内外盘增量累计。每次 poll 对比前后快照的 `active_buy` / `active_sell` 字段。

```python
class FlowWatcher:
    """主力资金流监测"""

    def __init__(
        self,
        inflow_1m: float = 50_000_000,     # 1分钟净流入 > 5000万
        outflow_1m: float = 50_000_000,    # 1分钟净流出 > 5000万
        sustained_3m: float = 20_000_000,  # 持续3分钟每分钟 > 2000万
    ):
        self.inflow_1m = inflow_1m
        self.outflow_1m = outflow_1m
        self.sustained_3m = sustained_3m
        self._prev_snapshots: Dict[str, QuoteSnapshot] = {}
        self._flow_windows: Dict[str, list] = {}  # code → [(ts, net_flow), ...]

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        alerts = []
        now = time.time()

        for code, snap in curr.items():
            prev = self._prev_snapshots.get(code)
            if prev is None:
                self._prev_snapshots[code] = snap
                continue

            # 本次区间增量
            delta_buy = snap.active_buy - (prev.active_buy or 0)
            delta_sell = snap.active_sell - (prev.active_sell or 0)
            net_flow = delta_buy - delta_sell

            # 滑动窗口
            if code not in self._flow_windows:
                self._flow_windows[code] = []
            window = self._flow_windows[code]
            window.append((now, net_flow))
            window[:] = [(t, f) for t, f in window if now - t <= 60]

            # 1分钟累计
            total_1m = sum(f for _, f in window)
            total_inflow = sum(f for _, f in window if f > 0)
            total_outflow = sum(f for _, f in window if f < 0)

            if total_1m >= self.inflow_1m:
                alerts.append({
                    "type": "big_inflow",
                    "code": code,
                    "name": snap.name,
                    "net_flow": total_1m,
                    "type_label": "主力流入",
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("主力资金大幅流入", extra={"data": {
                    "code": code, "name": snap.name,
                    "net_flow_1m": total_1m, "inflow": total_inflow, "outflow": abs(total_outflow),
                }})

            elif total_1m <= -self.outflow_1m:
                alerts.append({
                    "type": "big_outflow",
                    "code": code,
                    "name": snap.name,
                    "net_flow": total_1m,
                    "type_label": "主力流出",
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("主力资金大幅流出", extra={"data": {
                    "code": code, "name": snap.name,
                    "net_flow_1m": abs(total_1m), "inflow": total_inflow, "outflow": abs(total_outflow),
                }})

            self._prev_snapshots[code] = snap

        return alerts
```

---

### 3.4 LimitUpDownWatcher — 涨跌停

**职责：** 全市场扫描涨跌停股票，检测封板强度。

**策略：** 不分批轮询 5000 只（太慢），改为利用 TDX 的板块数据获取涨停板板块成分股。

```python
class LimitUpDownWatcher:
    """涨跌停监控（全市场）"""

    def __init__(self, scan_interval: float = 30.0):
        self.scan_interval = scan_interval
        self._last_scan: float = 0
        self._prev_sealed: Dict[str, dict] = {}  # 上次扫描的封板股票

    def process(self) -> List[dict]:
        """每30秒全市场扫描一次涨跌停"""
        now = time.time()
        if now - self._last_scan < self.scan_interval:
            return []  # 未到扫描间隔

        self._last_scan = now
        alerts = []

        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            # 连接服务器
            servers = DATA_SOURCE_CONFIG.get("tdx", {}).get("servers", [])
            connected = False
            for ip, port in servers:
                try:
                    if api.connect(ip, port):
                        connected = True
                        break
                except Exception:
                    continue
            if not connected:
                return []

            # 获取涨停板板块（TDX内置）
            for block_file, label in [("block_zs.dat", "指数"), ("block_gn.dat", "概念")]:
                try:
                    blocks = api.get_and_parse_block_info(block_file)
                    # 取板块成分股中涨幅>=9.5%的
                    for block in blocks:
                        code = block.get("code", "")
                        if not code or len(str(code)) != 6:
                            continue
                        # 过滤：只保留可能涨跌停的代码段
                        pass  # 详细实现见下
                except Exception:
                    continue

            api.disconnect()
        except Exception as e:
            logger.error(f"涨跌停扫描失败: {e}")

        return alerts

    def _scan_limit_stocks(self, api, codes: List[str]) -> List[dict]:
        """分批扫描疑似涨跌停股票，返回封板信息"""
        alerts = []
        batch_size = 50
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            try:
                # 构建查询列表 [(market, code), ...]
                queries = [(1 if c.startswith("6") else 0, c) for c in batch]
                quotes = api.get_security_quotes(queries)
                for q in quotes:
                    pct = float(q.get("change_pct", 0) or 0)  # TDX 可能返回涨跌幅
                    # 或者自己算
                    price = float(q.get("price", 0) or 0)
                    last_close = float(q.get("last_close", 0) or 0)
                    if last_close > 0:
                        pct = (price - last_close) / last_close * 100

                    if abs(pct) < 9.5:
                        continue

                    bid1_vol = float(q.get("bid_vol1", 0) or 0)
                    ask1_vol = float(q.get("ask_vol1", 0) or 0)
                    seal_amount = (bid1_vol if pct > 0 else ask1_vol) * 100 * price
                    seal_strength = "强" if seal_amount >= 200_000_000 else (
                        "中" if seal_amount >= 50_000_000 else "弱"
                    )

                    code = q.get("code", "")
                    alerts.append({
                        "type": "limit_up" if pct > 0 else "limit_down",
                        "code": code,
                        "name": str(q.get("name", "")),
                        "change_pct": round(pct, 2),
                        "seal_amount": round(seal_amount, 2),
                        "seal_strength": seal_strength,
                        "price": price,
                        "timestamp": datetime.now().isoformat(),
                    })
                    logger.info("涨跌停检测", extra={"data": {
                        "code": code, "name": q.get("name", ""),
                        "change_pct": round(pct, 2), "seal_strength": seal_strength,
                    }})
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"涨跌停批次扫描失败: {e}")
                continue
        return alerts
```

**扫描逻辑：**
1. 从 TDX 获取全市场股票列表（缓存，不每次拉取）
2. 分批 50 只/批，每批间隔 0.3 秒防封
3. 过滤涨跌幅 ≥ 9.5% 的股票
4. 计算封单金额（涨停=买1挂单量×价格，跌停=卖1挂单量×价格）
5. 分级：封单 ≥ 2 亿 = 强，≥ 5000 万 = 中，< 5000 万 = 弱

---

### 3.5 SectorHeatmap — 板块热点

**职责：** 获取 TDX 板块涨幅排名，产出板块热力图数据。

**机制：** 每 30 秒调用 TDX 板块 API，取涨幅 Top10。

```python
class SectorHeatmap:
    """板块热点"""

    def __init__(self, refresh_interval: float = 30.0):
        self.refresh_interval = refresh_interval
        self._last_refresh: float = 0
        self._cached_data: dict = {}  # 缓存最近一次结果

    def process(self) -> dict:
        now = time.time()
        if now - self._last_refresh < self.refresh_interval:
            return self._cached_data  # 返回缓存

        self._last_refresh = now
        result = {"concept": [], "industry": [], "timestamp": datetime.now().isoformat()}

        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            servers = DATA_SOURCE_CONFIG.get("tdx", {}).get("servers", [])
            connected = False
            for ip, port in servers:
                try:
                    if api.connect(ip, port):
                        connected = True
                        break
                except Exception:
                    continue
            if not connected:
                return self._cached_data

            # 获取板块数据
            for block_file, key in [("block_gn.dat", "concept"), ("block.dat", "industry")]:
                blocks = api.get_and_parse_block_info(block_file)
                # 取板块成分股，计算板块涨幅
                sector_list = []
                # TDX 板块数据含成分股代码，需逐板块计算涨幅
                # 简化方案：取板块数量和名称，涨幅后续扩展
                # 详细实现可根据需要逐板块查询
                result[key] = sector_list

            api.disconnect()
        except Exception as e:
            logger.error(f"板块热点获取失败: {e}")

        self._cached_data = result
        logger.info("板块热点刷新", extra={"data": {
            "concept_count": len(result.get("concept", [])),
            "industry_count": len(result.get("industry", [])),
        }})
        return result
```

> **注意：** TDX `get_and_parse_block_info` 返回的是板块→成分股映射，不含涨幅。板块涨幅需要逐板块计算（取成分股平均涨幅）。如需简化，先展示板块名称和成分股数量排序，涨幅计算后续扩展。

---

## 4. 前端设计

### 4.1 SSE 事件类型

| 事件类型 | 来源 | 前端处理 |
|----------|------|----------|
| `pool_snapshot` | PoolStatusTracker | 刷新自选股列表 |
| `surge_up` / `surge_down` | SurgeWatcher | 异动告警卡片 |
| `big_inflow` / `big_outflow` | FlowWatcher | 资金流告警卡片 |
| `limit_up` / `limit_down` | LimitUpDownWatcher | 涨跌停列表 |
| `sector_heatmap` | SectorHeatmap | 板块热力图 |
| `stats` | MarketWatcherEngine | 今日统计数字 |

### 4.2 UI 布局

```
┌─ 盯盘 ───────────────────────────────────────────┐
│  📊 今日: 涨停 15只 | 跌停 3只 | 异动 8次        │  ← stats 数字面板
│                                                    │
│  [自选股] [涨跌停] [异动] [板块] [资金流]         │  ← 子标签
│                                                    │
│  ┌─ 自选股 (按涨跌幅↓) ───────────────────────┐  │
│  │ 代码      名称     现价    涨跌   成交额    │  │
│  │ 300394  天孚通信  328.50  +5.2%  12.5亿   │  │
│  │ 600519  贵州茅台 1255.00  +0.5%   8.3亿   │  │
│  │ ...                                       │  │
│  └────────────────────────────────────────────┘  │
│                                                    │
│  ┌─ 异动告警 (实时滚动) ──────────────────────┐  │
│  │ 14:35 🔴 天孚通信 急速拉升 +2.1%  主力流入  │  │
│  │ 14:35 🟢 兆易创新 涨停封板 封单2.3亿(强)   │  │
│  └────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

---

## 5. API 端点

```python
# server.py 新增

@app.post("/api/watcher/start")
async def watcher_start(stock_pool: Optional[List[str]] = None):
    engine = MarketWatcherEngine.get_instance()
    engine.set_event_loop(asyncio.get_running_loop())
    pool = stock_pool or _get_stock_pool()
    return engine.start(pool)

@app.post("/api/watcher/stop")
async def watcher_stop():
    return MarketWatcherEngine.get_instance().stop()

@app.get("/api/watcher/status")
async def watcher_status():
    engine = MarketWatcherEngine.get_instance()
    return {"running": engine.is_running}

@app.get("/api/watcher/stream")
async def watcher_stream():
    """SSE 端点，推送盯盘实时数据"""
    engine = MarketWatcherEngine.get_instance()
    queue = engine.sse_manager.subscribe()
    async def event_gen():
        try:
            while True:
                data = await queue.get()
                yield data  # SSE format
        except asyncio.CancelledError:
            engine.sse_manager.unsubscribe(queue)
    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

---

## 6. 日志规范

所有子模块统一使用：

```python
from utils.logger import get_logger
logger = get_logger('live_trading', 'live_trading.log')
```

写入 `logs/live_trading.log`，JSON 结构化格式，与现有日志系统一致。

### 6.1 日志事件表

#### 引擎层 (MarketWatcherEngine)

| 事件 | 级别 | 数据 | 说明 |
|------|------|------|------|
| `MarketWatcher 已启动` | INFO | `stock_count` | 盯盘启动成功 |
| `MarketWatcher 已停止` | INFO | — | 盯盘正常停止 |
| `轮询失败` | ERROR | `exc_info=True` | TDX 连接或网络异常 |
| `轮询循环退出` | INFO | — | 线程正常退出 |
| `MarketWatcher 初始化完成` | INFO | — | 单例构造完成 |
| `股票池为空，跳过` | WARNING | — | 启动时无股票池 |

#### SurgeWatcher

| 事件 | 级别 | 数据 | 说明 |
|------|------|------|------|
| `异动拉升` | INFO | `code, name, change_pct, from_price, to_price` | 1分钟内涨超1.5% |
| `异动跳水` | INFO | `code, name, change_pct, from_price, to_price` | 1分钟内跌超1.5% |

#### FlowWatcher

| 事件 | 级别 | 数据 | 说明 |
|------|------|------|------|
| `主力资金大幅流入` | INFO | `code, name, net_flow_1m, inflow, outflow` | 1分钟净流入>5000万 |
| `主力资金大幅流出` | INFO | `code, name, net_flow_1m, inflow, outflow` | 1分钟净流出>5000万 |

#### LimitUpDownWatcher

| 事件 | 级别 | 数据 | 说明 |
|------|------|------|------|
| `涨跌停扫描开始` | DEBUG | `total_stocks, batch_count` | 全市场扫描启动 |
| `涨跌停检测` | INFO | `code, name, change_pct, seal_strength, seal_amount` | 检测到涨跌停 |
| `涨跌停扫描完成` | DEBUG | `limit_up_count, limit_down_count, elapsed_s` | 一轮扫描结束 |
| `涨跌停扫描失败` | ERROR | `exc_info=True` | TDX 连接失败或异常 |
| `涨跌停连接成功` | DEBUG | `ip, port` | 服务器连接成功 |

#### SectorHeatmap

| 事件 | 级别 | 数据 | 说明 |
|------|------|------|------|
| `板块热点刷新` | INFO | `concept_count, industry_count` | 板块数据更新 |
| `板块热点获取失败` | WARNING | `error` | API 失败，使用缓存 |

#### PoolStatusTracker

| 事件 | 级别 | 数据 | 说明 |
|------|------|------|------|
| `自选股快照` | DEBUG | `stock_count, avg_change_pct` | 每10轮输出一次摘要 |

### 6.2 日志排查指南

**问题：盯盘没数据**

```bash
# 检查引擎是否启动
grep "MarketWatcher 已启动" logs/live_trading.log

# 检查轮询是否正常
grep "轮询失败" logs/live_trading.log

# 如果轮询失败，查看异常堆栈
grep -A 20 "轮询失败" logs/live_trading.log
```

**问题：某子模块不触发**

```bash
# 异动不触发 → 检查阈值是否过高
grep "异动拉升\|异动跳水" logs/live_trading.log | tail -10

# 资金流不触发 → 检查内外盘数据
grep "主力资金" logs/live_trading.log | tail -10

# 涨跌停不触发 → 检查扫描是否执行
grep "涨跌停扫描" logs/live_trading.log | tail -10

# 板块无数据 → 检查TDX连接
grep "板块热点" logs/live_trading.log | tail -10
```

**问题：性能异常**

```bash
# 检查轮询频率（正常每3秒一条）
grep "自选股快照" logs/live_trading.log | tail -20

# 涨跌停扫描耗时
grep "涨跌停扫描完成" logs/live_trading.log | tail -5
```

### 6.3 启动/停止日志完整流程

启动时预期日志序列：

```
1. [INFO] MarketWatcher 初始化完成
2. [INFO] MarketWatcher 已启动 {"data": {"stock_count": 62}}
3. [DEBUG] 涨跌停扫描开始 {"data": {"total_stocks": 5000, "batch_count": 100}}
4. [DEBUG] 涨跌停连接成功 {"data": {"ip": "60.191.117.167", "port": 7709}}
5. [DEBUG] 涨跌停扫描完成 {"data": {"limit_up_count": 3, "limit_down_count": 1, "elapsed_s": 28.5}}
6. [INFO] 板块热点刷新 {"data": {"concept_count": 380, "industry_count": 74}}
7. [DEBUG] 自选股快照 {"data": {"stock_count": 62, "avg_change_pct": 0.35}}
8. [INFO] 异动拉升 {"data": {"code": "300394", ...}}
9. [INFO] 主力资金大幅流入 {"data": {"code": "002475", ...}}
...
```

停止时：

```
1. [INFO] MarketWatcher 已停止
2. [INFO] MarketWatcher 轮询循环退出
```

如果日志序列中断或缺少步骤，对应子模块可能有问题——对照上表定位即可。
