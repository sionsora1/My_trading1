"""
大单监控模块 — 双通道检测大额成交并推送 SSE 告警

通道1 (TDX): 实时价量 → MAD 动态基线 → 量价异动检测
通道2 (东方财富): 分钟级资金流向 → 超大单/大单/中单/小单分类

组件:
    Alert              — 告警值对象（不可变，含订单规模分类）
    QuoteSnapshot      — 实时行情快照
    TDXQuotesPoller    — 通过 pytdx 逐只获取实时快照
    EastMoneyFundPoller — 东方财富资金流向拉取器
    Detector           — MAD 动态基线大单判定引擎
    SSEManager         — SSE 连接池管理
    MonitorEngine      — 编排层（单例，后台双线程）
"""

from __future__ import annotations

import sys
import os

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import asyncio
import dataclasses
import json
import logging
import math
import random
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from config.settings import DATA_SOURCE_CONFIG

logger = logging.getLogger(__name__)

# ======================================================================
# 1. Alert — 告警值对象
# ======================================================================


@dataclass(frozen=True)
class Alert:
    """大单告警值对象 — 不可变，天然线程安全"""

    code: str              # 股票代码 '600519'
    name: str              # 股票名称 '贵州茅台'
    direction: str         # 'buy' | 'sell' | 'neutral'
    level: str             # 'super_large' | 'large' | 'volume_spike'
    volume: int            # 区间成交量（股）
    hands: int             # 区间成交量（手）= volume // 100
    amount: float          # 区间成交额（元）
    price: float           # 当前价格
    change_pct: float      # 区间价格变动 %
    mad_multiple: float    # 偏离 MAD 倍数
    super_large_flow: float  # 超大单净流入（东方财富，元/分钟）
    large_flow: float        # 大单净流入（东方财富，元/分钟）
    time: str              # 数据时间 '14:32:15'
    timestamp: str         # ISO 格式 '2026-06-17T14:32:15'

    def to_sse_data(self) -> str:
        """序列化为 SSE data 字段（JSON 字符串）"""
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False)


# ======================================================================
# 2. QuoteSnapshot — 实时行情快照
# ======================================================================


@dataclass(frozen=True)
class QuoteSnapshot:
    """单只股票的实时行情快照 — 不可变，线程安全"""

    code: str           # '600519'
    name: str           # '贵州茅台'
    price: float        # 现价
    open: float         # 开盘价
    high: float         # 最高价
    low: float          # 最低价
    volume: float       # 累计成交量（股）
    amount: float       # 累计成交额（元）
    change_pct: float   # 涨跌幅 %（相对昨收）
    bid1: float         # 买一价
    ask1: float         # 卖一价
    active_buy: float   # 外盘（主动买）
    active_sell: float  # 内盘（主动卖）
    time: str           # 数据时间 '14:32:15'


# ======================================================================
# 3. TDXQuotesPoller — 数据采集
# ======================================================================


class TDXQuotesPoller:
    """通过 pytdx 逐只获取实时行情快照。"""

    def __init__(
        self,
        servers: Optional[List[Tuple[str, int]]] = None,
        connect_timeout: float = 5.0,
    ):
        tdx_cfg = DATA_SOURCE_CONFIG.get("tdx", {})
        self._servers: List[Tuple[str, int]] = servers or tdx_cfg.get(
            "servers",
            [("60.191.117.167", 7709)],
        )
        self._timeout: float = connect_timeout

    @staticmethod
    def is_trading_time() -> bool:
        """判断当前是否在 A 股交易时段。"""
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        morning_start = t.replace(hour=9, minute=25, second=0, microsecond=0)
        morning_end = t.replace(hour=11, minute=30, second=0, microsecond=0)
        afternoon_start = t.replace(hour=13, minute=0, second=0, microsecond=0)
        afternoon_end = t.replace(hour=15, minute=0, second=0, microsecond=0)
        return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)

    def poll(self, codes: List[str]) -> Dict[str, "QuoteSnapshot"]:
        """轮询获取实时行情快照。"""
        if not codes:
            return {}
        if not self.is_trading_time():
            return {}

        from pytdx.hq import TdxHq_API

        api = TdxHq_API(auto_retry=True, raise_exception=False)
        connected = False
        for ip, port in self._servers:
            try:
                if api.connect(ip, port, time_out=self._timeout):
                    connected = True
                    break
            except Exception:
                continue
        if not connected:
            return {}

        results: Dict[str, QuoteSnapshot] = {}
        now_str = datetime.now().strftime("%H:%M:%S")
        for code in codes:
            market = 1 if code.startswith("6") else 0
            try:
                quotes = api.get_security_quotes([(market, code)])
                if not quotes:
                    continue
                q = quotes[0]
                price = float(q.get("price", 0) or 0)
                if price <= 0:
                    continue
                pre_close = float(q.get("last_close", 0) or 0)
                change_pct = round((price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0.0
                snapshot = QuoteSnapshot(
                    code=code,
                    name=str(q.get("name", "")),
                    price=price,
                    open=float(q.get("open", 0) or 0),
                    high=float(q.get("high", 0) or 0),
                    low=float(q.get("low", 0) or 0),
                    volume=float(q.get("vol", 0) or 0),
                    amount=float(q.get("amount", 0) or 0),
                    change_pct=change_pct,
                    bid1=float(q.get("bid1", 0) or 0),
                    ask1=float(q.get("ask1", 0) or 0),
                    active_buy=float(q.get("active1", 0) or 0),
                    active_sell=float(q.get("active2", 0) or 0),
                    time=now_str,
                )
                results[code] = snapshot
            except Exception:
                pass

        try:
            api.disconnect()
        except Exception:
            pass
        return results


# ======================================================================
# 4. EastMoneyFundPoller — 东方财富资金流向
# ======================================================================


class EastMoneyFundPoller:
    """从东方财富拉取分钟级资金流向数据（超大单/大单/中单/小单分类）。

    限流策略: 每次请求间隔 ≥ 2 秒，全量拉取周期约 2-3 分钟。
    """

    # 东方财富市场代码映射
    _MKT_MAP = {"6": "1", "0": "0", "3": "0"}  # 1=上海, 0=深圳

    def __init__(self, min_interval: float = 3.0):
        self._min_interval = min_interval
        self._last_request: float = 0
        self._cache: Dict[str, dict] = {}  # code → fund_flow_data
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "*/*",
        })

    def _rate_limit(self) -> None:
        """确保请求间隔 >= min_interval。"""
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed + random.uniform(0, 1))
        self._last_request = time.time()

    def _make_secid(self, code: str) -> str:
        """纯数字代码 → 东方财富 secid 格式。"""
        prefix = self._MKT_MAP.get(code[0], "0")
        return f"{prefix}.{code}"

    def fetch(self, code: str) -> Optional[dict]:
        """拉取单只股票最新一分钟资金流向。

        Returns:
            {
                "time": "14:58",
                "main_force": -674887744.0,    # 主力净流入
                "small_order": -217811.0,      # 小单净流入
                "medium_order": 675105570.0,   # 中单净流入
                "large_order": -309043724.0,   # 大单净流入
                "super_large_order": -365844020.0,  # 超大单净流入
            }
        """
        self._rate_limit()
        secid = self._make_secid(code)
        try:
            r = self._session.get(
                "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                params={
                    "lmt": "1",
                    "klt": "1",
                    "secid": secid,
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56",
                },
                timeout=10,
            )
            data = r.json()
            if data.get("data") and data["data"].get("klines"):
                parts = data["data"]["klines"][-1].split(",")
                if len(parts) >= 6:
                    return {
                        "time": parts[0].split()[-1] if " " in parts[0] else parts[0],
                        "main_force": float(parts[1]),
                        "small_order": float(parts[2]),
                        "medium_order": float(parts[3]),
                        "large_order": float(parts[4]),
                        "super_large_order": float(parts[5]),
                    }
        except Exception as e:
            logger.debug(f"东方财富资金流获取失败 {code}: {e}")
        return None

    def fetch_all(self, codes: List[str]) -> Dict[str, dict]:
        """批量拉取，更新内部缓存。"""
        results: Dict[str, dict] = {}
        for code in codes:
            data = self.fetch(code)
            if data is not None:
                self._cache[code] = data
                results[code] = data
        return results

    def get_cached(self, code: str) -> Optional[dict]:
        """获取缓存数据（最近 120 秒内有效）。"""
        return self._cache.get(code)


# ======================================================================
# 5. Detector — MAD 动态基线大单判定
# ======================================================================


class Detector:
    """MAD 动态基线检测器。

    每只股票独立维护 60 秒滑动窗口（12 个 delta_amount）。
    用中位数绝对偏差 (MAD) 替代固定阈值，自适应每只股票的流动性。

    告警级别:
    - super_large:  delta > median + 8×MAD
    - large:        delta > median + 5×MAD
    - volume_spike: delta > median + 3×MAD

    防刷屏:
    - 同只股票 60 秒冷却期
    - 每轮最多 10 条告警
    """

    # ── 双重阈值：(delta/median) 比 AND (delta-median)/MAD 比 ──
    # 两个条件都满足才触发，避免单一指标误判
    SUPER_LARGE_MEDIAN_RATIO = 5.0    # delta 至少是 median 的 5 倍
    SUPER_LARGE_MAD = 10.0            # 偏离至少 10 倍 MAD
    LARGE_MEDIAN_RATIO = 3.0          # delta 至少是 median 的 3 倍
    LARGE_MAD = 6.0                   # 偏离至少 6 倍 MAD
    SPIKE_MEDIAN_RATIO = 2.0          # delta 至少是 median 的 2 倍
    SPIKE_MAD = 4.0                   # 偏离至少 4 倍 MAD

    # ── 滑窗参数 ──
    WINDOW_SIZE = 12     # 60 秒 / 5 秒间隔
    COOLDOWN_SEC = 60    # 同股票冷却时间

    def __init__(self):
        # 每只股票的 delta 滑窗 {code: [(timestamp, delta_amt, delta_vol, delta_buy, delta_sell), ...]}
        self._windows: Dict[str, list] = {}
        # 冷却期 {code: last_alert_timestamp}
        self._cooldowns: Dict[str, float] = {}
        # 历史基线（冷启动用） {code: baseline_rate}
        self._history_baselines: Dict[str, float] = {}

    def set_history_baselines(self, baselines: Dict[str, float]) -> None:
        """设置历史同时段基准量（来自 _compute_baselines）。"""
        self._history_baselines = baselines

    def feed(self, code: str, delta_amt: float, delta_vol: float,
             delta_buy: float, delta_sell: float) -> None:
        """喂入一个新 delta 数据点到滑窗。"""
        if code not in self._windows:
            self._windows[code] = []
        window = self._windows[code]
        now = time.time()
        window.append((now, delta_amt, delta_vol, delta_buy, delta_sell))
        # 清理过期数据（超过 60 秒）
        self._windows[code] = [(t, a, v, b, s) for t, a, v, b, s in window
                               if now - t <= 60]

    @staticmethod
    def _mad(values: List[float]) -> float:
        """中位数绝对偏差。"""
        if len(values) < 3:
            return 0.0
        median = statistics.median(values)
        abs_dev = [abs(v - median) for v in values]
        return statistics.median(abs_dev)

    def check(self, code: str, delta_amt: float, delta_vol: float,
              delta_buy: float, delta_sell: float) -> Optional[Tuple[str, float]]:
        """检测是否触发告警。

        Returns:
            (level, mad_multiple) 或 None
        """
        window = self._windows.get(code, [])
        if len(window) < self.WINDOW_SIZE:
            # 冷启动：数据点不足，用历史基线兜底
            baseline_rate = self._history_baselines.get(code, 0.0)
            if baseline_rate > 0:
                # 5秒基准量 × 窗口期间 = 5 * baseline_rate
                expected = baseline_rate * 5 * self.WINDOW_SIZE
                if expected > 0 and delta_amt > expected * self.MAD_SPIKE:
                    mad_mul = delta_amt / expected if expected > 0 else 0
                    return ("volume_spike", round(mad_mul, 1))
            return None

        # 提取 delta_amt 序列
        deltas = [d[1] for d in window]
        median = statistics.median(deltas)
        mad = self._mad(deltas)
        # MAD=0 时回退用均值的 10% 作为最小偏差（极少发生，仅极低波动股）
        if mad <= 0:
            mean_val = sum(deltas) / len(deltas)
            mad = mean_val * 0.1 if mean_val > 0 else 1.0
            if mad <= 0:
                mad = 1.0

        deviation = delta_amt - median
        mad_multiple = deviation / mad if mad > 0 else deviation
        median_ratio = delta_amt / median if median > 0 else delta_amt

        # 检测冷却
        now = time.time()
        last = self._cooldowns.get(code, 0)
        if now - last < self.COOLDOWN_SEC:
            return None

        # 双重阈值：中位数比 AND MAD倍数 同时达标
        if median_ratio >= self.SUPER_LARGE_MEDIAN_RATIO and mad_multiple >= self.SUPER_LARGE_MAD:
            return ("super_large", round(mad_multiple, 1))
        elif median_ratio >= self.LARGE_MEDIAN_RATIO and mad_multiple >= self.LARGE_MAD:
            return ("large", round(mad_multiple, 1))
        elif median_ratio >= self.SPIKE_MEDIAN_RATIO and mad_multiple >= self.SPIKE_MAD:
            return ("volume_spike", round(mad_multiple, 1))
        return None

    def mark_alerted(self, code: str) -> None:
        """记录冷却时间。"""
        self._cooldowns[code] = time.time()

    def get_window_info(self, code: str) -> dict:
        """获取滑窗统计信息（调试用）。"""
        window = self._windows.get(code, [])
        if len(window) < 3:
            return {"count": len(window)}
        deltas = [d[1] for d in window]
        return {
            "count": len(deltas),
            "median": round(statistics.median(deltas), 0),
            "mad": round(self._mad(deltas), 0),
            "last_delta": round(deltas[-1], 0),
        }


# ======================================================================
# 6. SSEManager — SSE 连接池管理
# ======================================================================


class SSEManager:
    """SSE 连接管理器。"""

    def __init__(self, max_queue_size: int = 500):
        self._queues: List[asyncio.Queue] = []
        self._max_size = max_queue_size

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_size)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    @property
    def active_count(self) -> int:
        return len(self._queues)

    async def push(self, alert: "Alert") -> None:
        dead: List[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(alert)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    async def push_heartbeat(self) -> None:
        heartbeat = json.dumps({
            "type": "heartbeat",
            "time": datetime.now().strftime("%H:%M:%S"),
        }, ensure_ascii=False)
        dead: List[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(heartbeat)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(heartbeat)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                self._queues.remove(q)
            except ValueError:
                pass


# ======================================================================
# 7. MonitorEngine — 编排层（单例，双通道）
# ======================================================================


class MonitorEngine:
    """大单监控引擎（单例）。

    双通道检测:
    1. TDX 实时价量 → MAD 动态基线 → 量价异动
    2. 东方财富资金流 → 超大单/大单分类 → 交叉验证

    生命周期:
    1. start(stock_pool) → 计算历史基线 → 启动双后台线程
    2. _poll_loop() → 每 5s 轮询 TDX → MAD 检测 → 推送
    3. _fund_flow_loop() → 每 60s 拉取东方财富资金流 → 缓存
    4. stop() → 停止线程 → 清理
    """

    _instance: Optional["MonitorEngine"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "MonitorEngine":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        if hasattr(self, "_poller"):
            return

        self._poller = TDXQuotesPoller()
        self._fund_poller = EastMoneyFundPoller()
        self._detector = Detector()
        self._sse = SSEManager()

        self._thread: Optional[threading.Thread] = None
        self._fund_thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._alerts: List[Alert] = []
        self._prev_snapshots: Dict[str, QuoteSnapshot] = {}
        self._stock_pool: List[str] = []
        self._interval: float = 5.0
        self._fund_interval: float = 60.0
        self._max_alerts: int = 500
        self._max_per_cycle: int = 10
        self._baseline_days: int = 20

        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._started_at: Optional[str] = None
        self._last_poll_at: Optional[str] = None
        self._total_alerts: int = 0
        self._stock_names: Dict[str, str] = {}

        logger.info("MonitorEngine 初始化完成 (MAD动态基线 + 东方财富双通道)")

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def total_alerts(self) -> int:
        return self._total_alerts

    @property
    def stock_count(self) -> int:
        return len(self._stock_pool) if self._stock_pool else 0

    @property
    def sse_manager(self) -> SSEManager:
        return self._sse

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._event_loop = loop
        logger.info("MonitorEngine 事件循环已设置")

    # ------------------------------------------------------------------
    # 股票名称
    # ------------------------------------------------------------------

    def _load_stock_names(self) -> None:
        try:
            from data.database import SQLiteManager
            db = SQLiteManager()
            rows = db._conn.execute("SELECT ts_code, name FROM stock_info").fetchall()
            for row in rows:
                code = row["ts_code"].replace(".SH", "").replace(".SZ", "")
                if row["name"]:
                    self._stock_names[code] = row["name"]
            db.close()
            logger.debug(f"加载了 {len(self._stock_names)} 个股票名称")
        except Exception as e:
            logger.error(f"股票名称加载失败: {e}")

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------

    def start(self, stock_pool: List[str]) -> Dict[str, Any]:
        with self._lock:
            if self._running.is_set():
                return {"status": "already_running", "stock_count": self.stock_count}

            self._stock_pool = list(stock_pool)
            if not self._stock_pool:
                return {"status": "error", "message": "股票池为空"}

            self._running.set()
            self._started_at = datetime.now().isoformat()
            self._alerts = []
            self._prev_snapshots = {}
            self._total_alerts = 0
            self._load_stock_names()

            # 启动 TDX 轮询线程
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="monitor-tdx",
            )
            self._thread.start()

            # 启动东方财富资金流线程
            self._fund_thread = threading.Thread(
                target=self._fund_flow_loop, daemon=True, name="monitor-fund",
            )
            self._fund_thread.start()

            logger.info(f"MonitorEngine 已启动: {len(self._stock_pool)} 只股票 (双通道)")
            return {"status": "started", "stock_count": len(self._stock_pool)}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            if not self._running.is_set():
                return {"status": "not_running"}
            self._running.clear()

        for t in (self._thread, self._fund_thread):
            if t is not None and t.is_alive():
                t.join(timeout=5.0)

        logger.info(f"MonitorEngine 已停止，累计告警: {self._total_alerts}")
        return {"status": "stopped", "total_alerts": self._total_alerts}

    def get_history(self, limit: int = 100) -> List[Alert]:
        with self._lock:
            return list(self._alerts[-limit:])

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running,
            "stock_count": self.stock_count,
            "total_alerts": self._total_alerts,
            "started_at": self._started_at,
            "last_poll_at": self._last_poll_at,
            "interval_seconds": self._interval,
            "sse_connections": self._sse.active_count,
        }

    # ==================================================================
    # 通道1: TDX 实时价量 → MAD 检测
    # ==================================================================

    def _poll_loop(self) -> None:
        """TDX 轮询主循环（每 5 秒）。"""
        logger.info("MonitorEngine TDX 轮询启动")

        # 冷启动：计算历史基线
        try:
            self._compute_baselines()
        except Exception as e:
            logger.error(f"基准量计算失败: {e}")

        # 预热：先累积几轮数据不管测
        warmup_rounds = 3

        while self._running.is_set():
            if not TDXQuotesPoller.is_trading_time():
                time.sleep(30)
                continue

            try:
                curr = self._poller.poll(self._stock_pool)
            except Exception as e:
                logger.error(f"TDX 轮询失败: {e}")
                time.sleep(10)
                continue

            self._last_poll_at = datetime.now().isoformat()

            # 注入名称
            if self._stock_names and curr:
                curr = {
                    code: dataclasses.replace(
                        snap, name=self._stock_names.get(code, snap.name)
                    )
                    for code, snap in curr.items()
                }

            if not curr:
                self._prev_snapshots = curr
                time.sleep(self._interval)
                continue

            # 计算 delta 并喂入滑窗
            prev = self._prev_snapshots
            deltas: Dict[str, Tuple[float, float, float, float]] = {}
            for code, snap in curr.items():
                p = prev.get(code) if prev else None
                if p is None or p.volume <= 0:
                    continue
                da = snap.amount - p.amount
                dv = snap.volume - p.volume
                db = (snap.active_buy or 0) - (p.active_buy or 0)
                ds = (snap.active_sell or 0) - (p.active_sell or 0)
                if dv <= 0 or da <= 0:
                    continue
                deltas[code] = (da, dv, db, ds)
                self._detector.feed(code, da, dv, db, ds)

            self._prev_snapshots = curr

            if warmup_rounds > 0:
                warmup_rounds -= 1
                time.sleep(self._interval)
                continue

            # 检测
            candidates: List[Tuple[str, str, float, float, float, float, float]] = []
            for code, (da, dv, db, ds) in deltas.items():
                result = self._detector.check(code, da, dv, db, ds)
                if result is not None:
                    level, mad_mul = result
                    candidates.append((code, level, mad_mul, da, dv, db, ds))

            # 按 MAD 倍数排序，每轮最多 10 条
            candidates.sort(key=lambda x: x[2], reverse=True)
            alerts_this_cycle: List[Alert] = []
            for code, level, mad_mul, da, dv, db, ds in candidates[:self._max_per_cycle]:
                snap = curr.get(code)
                if snap is None:
                    continue

                # 交叉验证：查东方财富资金流缓存
                fund_data = self._fund_poller.get_cached(code)
                super_large_flow = fund_data.get("super_large_order", 0) if fund_data else 0
                large_flow = fund_data.get("large_order", 0) if fund_data else 0

                # 方向判定：主动买卖盘占比
                total_active = db + ds
                if total_active > 0:
                    buy_ratio = db / total_active
                else:
                    buy_ratio = 0.5
                if buy_ratio > 0.7:
                    direction = "buy"
                elif buy_ratio < 0.3:
                    direction = "sell"
                else:
                    direction = "neutral"

                alert = Alert(
                    code=code,
                    name=snap.name or code,
                    direction=direction,
                    level=level,
                    volume=int(dv),
                    hands=int(dv // 100),
                    amount=round(da, 2),
                    price=snap.price,
                    change_pct=round(
                        (snap.price - prev[code].price) / prev[code].price * 100, 2
                    ) if code in prev and prev[code].price > 0 else 0.0,
                    mad_multiple=mad_mul,
                    super_large_flow=round(super_large_flow, 2),
                    large_flow=round(large_flow, 2),
                    time=snap.time,
                    timestamp=datetime.now().isoformat(),
                )
                alerts_this_cycle.append(alert)
                self._detector.mark_alerted(code)

            # 推送
            for alert in alerts_this_cycle:
                with self._lock:
                    self._alerts.append(alert)
                    self._total_alerts += 1
                    if len(self._alerts) > self._max_alerts:
                        self._alerts = self._alerts[-self._max_alerts:]

                loop = self._event_loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._sse.push(alert), loop)

            if alerts_this_cycle:
                logger.info(
                    f"检测到 {len(alerts_this_cycle)} 条大单告警: "
                    + ", ".join(
                        f"{a.name}({a.code}) {a.direction} {a.hands}手 "
                        f"[{a.level} {a.mad_multiple}×MAD]"
                        for a in alerts_this_cycle[:5]
                    )
                    + ("..." if len(alerts_this_cycle) > 5 else "")
                )

            time.sleep(self._interval)

        logger.info("MonitorEngine TDX 轮询退出")

    # ==================================================================
    # 通道2: 东方财富资金流轮询
    # ==================================================================

    def _fund_flow_loop(self) -> None:
        """东方财富资金流轮询（每 60 秒拉一批）。"""
        logger.info("MonitorEngine 东方财富资金流轮询启动")
        while self._running.is_set():
            if not TDXQuotesPoller.is_trading_time():
                time.sleep(60)
                continue
            try:
                results = self._fund_poller.fetch_all(self._stock_pool)
                if results:
                    logger.debug(f"东方财富资金流: 更新 {len(results)} 只")
            except Exception as e:
                logger.warning(f"东方财富资金流轮询异常: {e}")
            time.sleep(self._fund_interval)
        logger.info("MonitorEngine 东方财富资金流轮询退出")

    # ==================================================================
    # 历史基准量计算（冷启动预热 MAD 基线）
    # ==================================================================

    def _compute_baselines(self) -> None:
        """计算每只股票的基准区间成交量（股/秒），用于 MAD 冷启动。

        近 20 个交易日的日均成交量 / 14400 秒。
        """
        logger.info(f"开始计算历史基线: {len(self._stock_pool)} 只股票")
        try:
            from data.database import SQLiteManager
        except ImportError:
            return

        baselines: Dict[str, float] = {}
        now = datetime.now()
        end_date = now.strftime("%Y%m%d")
        start_date = (now - timedelta(days=60)).strftime("%Y%m%d")

        db: Optional[Any] = None
        try:
            db = SQLiteManager()
        except Exception as e:
            logger.warning(f"数据库连接失败: {e}")
            return

        try:
            for code in self._stock_pool:
                ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                try:
                    vol_per_sec = self._calc_baseline_for_stock(db, ts_code, start_date, end_date)
                    if vol_per_sec is not None and vol_per_sec > 0:
                        baselines[code] = vol_per_sec
                except Exception:
                    pass
            self._detector.set_history_baselines(baselines)
            logger.info(f"历史基线计算完成: {len(baselines)}/{len(self._stock_pool)} 只")
        finally:
            try:
                db.close()
            except Exception:
                pass

    @staticmethod
    def _calc_baseline_for_stock(db: Any, ts_code: str, start_date: str,
                                  end_date: str) -> Optional[float]:
        """单只股票的每秒基准成交量（日均成交量 / 14400 秒）。"""
        try:
            rows = db.get_daily_bars(ts_code, start_date, end_date)
            if rows and len(rows) >= 5:
                recent = rows[-20:]
                total_vol = sum(float(r.get("volume", 0) or 0) for r in recent)
                avg_daily_vol = total_vol / len(recent)
                return avg_daily_vol / 14400.0
        except Exception:
            pass
        return None
