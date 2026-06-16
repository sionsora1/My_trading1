"""
大单监控模块 — 实时检测大额成交并推送 SSE 告警

组件:
    Alert          — 告警值对象（不可变）
    QuoteSnapshot  — 实时行情快照
    TDXQuotesPoller — 通过 pytdx 逐只获取实时快照
    Detector       — 大单判定引擎
    SSEManager     — SSE 连接池管理
    MonitorEngine  — 编排层（单例，后台线程）
"""

from __future__ import annotations

import sys
import os

# 项目根目录加入 sys.path，确保子模块导入一致
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import asyncio
import dataclasses
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from config.settings import DATA_SOURCE_CONFIG

logger = logging.getLogger(__name__)

# ======================================================================
# 1. Alert — 告警值对象
# ======================================================================


@dataclass(frozen=True)
class Alert:
    """大单告警值对象 — 不可变，天然线程安全"""

    code: str          # 股票代码 '600519'
    name: str          # 股票名称 '贵州茅台'
    direction: str     # 'buy' | 'sell' | 'unknown'
    volume: int        # 区间成交量（股）
    hands: int         # 区间成交量（手）= volume // 100
    amount: float      # 区间成交额（元）
    price: float       # 当前价格
    change_pct: float  # 区间价格变动 %
    time: str          # 数据时间 '14:32:15'
    timestamp: str     # ISO 格式 '2026-06-17T14:32:15'

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
    """通过 pytdx 逐只获取实时行情快照。

    设计要点:
    - 每次 poll() 对列表中的股票逐一查询
    - 返回 Dict[str, QuoteSnapshot]，key 为纯数字代码
    - 单只失败不影响其他股票，失败的记录 warn 日志
    - 不在交易时段直接返回空字典
    """

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

    # ------------------------------------------------------------------
    # 交易时间判断
    # ------------------------------------------------------------------

    @staticmethod
    def is_trading_time() -> bool:
        """判断当前是否在 A 股交易时段。

        集合竞价阶段 9:15-9:25 视为非交易时段（量价不稳定）。
        有效时段: 9:25-11:30, 13:00-15:00，周一至周五。
        """
        now = datetime.now()
        # 周末
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        t = now.time()
        morning_start = t.replace(hour=9, minute=25, second=0, microsecond=0)
        morning_end = t.replace(hour=11, minute=30, second=0, microsecond=0)
        afternoon_start = t.replace(hour=13, minute=0, second=0, microsecond=0)
        afternoon_end = t.replace(hour=15, minute=0, second=0, microsecond=0)

        return (morning_start <= t <= morning_end) or (afternoon_start <= t <= afternoon_end)

    # ------------------------------------------------------------------
    # 行情轮询
    # ------------------------------------------------------------------

    def poll(self, codes: List[str]) -> Dict[str, "QuoteSnapshot"]:
        """轮询获取实时行情快照。

        Args:
            codes: 纯数字股票代码列表，如 ['600519', '000001']

        Returns:
            Dict[str, QuoteSnapshot]，key 为纯数字代码。
            失败或不在交易时段的股票不出现在结果中。
        """
        if not codes:
            return {}

        if not self.is_trading_time():
            logger.debug("当前非交易时段，跳过轮询")
            return {}

        from pytdx.hq import TdxHq_API

        api = TdxHq_API(auto_retry=True, raise_exception=False)
        connected = False

        # 尝试连接服务器
        for ip, port in self._servers:
            try:
                if api.connect(ip, port, time_out=self._timeout):
                    connected = True
                    logger.debug(f"TDX 轮询连接成功: {ip}:{port}")
                    break
            except Exception:
                continue

        if not connected:
            logger.warning("TDX 轮询: 所有服务器连接失败")
            return {}

        results: Dict[str, QuoteSnapshot] = {}
        now_str = datetime.now().strftime("%H:%M:%S")

        for code in codes:
            market = 1 if code.startswith("6") else 0
            try:
                quotes = api.get_security_quotes([(market, code)])
                if not quotes:
                    logger.debug(f"TDX 行情为空: {code}")
                    continue

                q = quotes[0]
                price = float(q.get("price", 0) or 0)
                if price <= 0:
                    continue  # 停牌或无数据

                # 计算涨跌幅（相对昨收）
                pre_close = float(q.get("last_close", 0) or 0)
                if pre_close > 0:
                    change_pct = round((price - pre_close) / pre_close * 100, 2)
                else:
                    change_pct = 0.0

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
                logger.warning(f"TDX 行情获取失败: {code}", exc_info=False)

        try:
            api.disconnect()
        except Exception:
            pass

        return results


# ======================================================================
# 4. Detector — 大单判定
# ======================================================================


class Detector:
    """大单检测器。

    判定分为两步:
    1. 量比检测 — 区间成交量是否远超历史同期均值
    2. 方向判定 — 根据价格变化判断买入 / 卖出

    所有阈值均可通过构造参数或 set_thresholds() 调整。
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
        self._baselines: Dict[str, float] = {}  # {code: 基准区间量（股/秒）}

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def set_thresholds(self, **kwargs: Any) -> None:
        """运行时动态调整阈值。

        Example:
            detector.set_thresholds(vol_ratio_threshold=4.0, amount_min=10_000_000)
        """
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def set_baselines(self, baselines: Dict[str, float]) -> None:
        """设置每只股票的基准区间成交量（股 / 秒）。

        baselines 由外部计算（基于近 20 日同时段均值），传入此方法。
        """
        self._baselines = baselines

    # ------------------------------------------------------------------
    # 检测
    # ------------------------------------------------------------------

    def detect(
        self,
        code: str,
        prev: Optional["QuoteSnapshot"],
        curr: "QuoteSnapshot",
    ) -> Optional["Alert"]:
        """对比前后两帧快照，判定是否触发大单告警。

        Args:
            code: 股票代码
            prev: 上一帧快照（首次为 None，不触发）
            curr: 当前帧快照

        Returns:
            Alert 对象（触发时）或 None（未触发）
        """
        if prev is None:
            return None

        # 计算区间量价变化
        delta_volume = curr.volume - prev.volume
        delta_amount = curr.amount - prev.amount

        if delta_volume <= 0:
            return None

        # 量比计算
        baseline_rate = self._baselines.get(code, 0.0)
        time_diff = self._compute_interval_seconds(prev.time, curr.time)
        if time_diff <= 0:
            time_diff = 5  # 默认 5 秒间隔

        baseline_vol = baseline_rate * time_diff
        if baseline_vol > 0:
            vol_ratio = delta_volume / baseline_vol
        else:
            # 无基准量 → 只用成交额判定
            vol_ratio = 999.0 if delta_amount >= self.amount_min else 0.0

        # 阈值判定
        if vol_ratio < self.vol_ratio or delta_amount < self.amount_min:
            return None

        # 方向判定
        if prev.price > 0:
            price_change = (curr.price - prev.price) / prev.price * 100
        else:
            price_change = 0.0

        if price_change >= self.price_change_min:
            direction = "buy"
        elif price_change <= -self.price_change_min:
            direction = "sell"
        else:
            direction = "unknown"

        hands = int(delta_volume // 100)
        timestamp = datetime.now().isoformat()

        return Alert(
            code=code,
            name=curr.name,
            direction=direction,
            volume=int(delta_volume),
            hands=hands,
            amount=round(delta_amount, 2),
            price=curr.price,
            change_pct=round(price_change, 2),
            time=curr.time,
            timestamp=timestamp,
        )

    def detect_batch(
        self,
        prev_snapshots: Dict[str, "QuoteSnapshot"],
        curr_snapshots: Dict[str, "QuoteSnapshot"],
    ) -> List["Alert"]:
        """批量检测，返回所有触发的告警。

        Args:
            prev_snapshots: 上一轮快照字典
            curr_snapshots: 当前轮快照字典

        Returns:
            告警列表（按触发顺序）
        """
        alerts: List[Alert] = []
        for code, curr in curr_snapshots.items():
            prev = prev_snapshots.get(code)
            alert = self.detect(code, prev, curr)
            if alert is not None:
                alerts.append(alert)
        return alerts

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_interval_seconds(prev_time: str, curr_time: str) -> float:
        """计算两个 HH:MM:SS 格式时间字符串之间的秒数差。"""
        try:
            fmt = "%H:%M:%S"
            t0 = datetime.strptime(prev_time, fmt)
            t1 = datetime.strptime(curr_time, fmt)
            return (t1 - t0).total_seconds()
        except Exception:
            return 0.0


# ======================================================================
# 5. SSEManager — SSE 连接池管理
# ======================================================================


class SSEManager:
    """SSE 连接管理器。

    设计要点:
    - 每个 SSE 连接对应一个 asyncio.Queue
    - push() 广播到所有活跃连接
    - 客户端断开时由调用方负责调用 unsubscribe()
    - 定期心跳（30s）防止代理 / 负载均衡断开
    """

    def __init__(self, max_queue_size: int = 500):
        self._queues: List[asyncio.Queue] = []
        self._max_size = max_queue_size

    def subscribe(self) -> asyncio.Queue:
        """新客户端订阅，返回专属队列。

        Returns:
            asyncio.Queue（maxsize=self._max_size）
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_size)
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """客户端断开时取消订阅。

        Args:
            q: subscribe() 返回的队列
        """
        try:
            self._queues.remove(q)
        except ValueError:
            pass

    @property
    def active_count(self) -> int:
        """活跃连接数"""
        return len(self._queues)

    async def push(self, alert: "Alert") -> None:
        """广播一条告警到所有连接。

        队列满时丢弃最旧消息再放入新消息。
        队列已关闭或异常时自动移除。
        """
        dead: List[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                # 队列满 → 丢弃最旧 -> 放入新消息
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
        """发送心跳消息到所有连接。"""
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
# 6. MonitorEngine — 编排层（单例）
# ======================================================================


class MonitorEngine:
    """大单监控引擎（单例）。

    生命周期:
    1. start(stock_pool) → 初始化基准量 → 启动后台轮询线程
    2. _poll_loop() → 每 5s 轮询 → 检测 → 推送
    3. stop() → 设置停止标志 → 等待线程结束 → 清理资源

    线程安全:
    - 轮询在 daemon 线程中执行
    - SSE 推送在 asyncio 事件循环中执行
    - Alert 是不可变值对象，天然线程安全
    - 状态变量使用 threading.Lock 保护
    """

    _instance: Optional["MonitorEngine"] = None
    _instance_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 单例
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "MonitorEngine":
        """获取单例实例（线程安全）。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    def __init__(self):
        # 防止重复初始化（单例模式下 __init__ 可能被多次调用）
        if hasattr(self, "_poller"):
            return

        self._poller = TDXQuotesPoller()
        self._detector = Detector()
        self._sse = SSEManager()

        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._alerts: List[Alert] = []
        self._prev_snapshots: Dict[str, QuoteSnapshot] = {}
        self._stock_pool: List[str] = []
        self._interval: float = 5.0  # 轮询间隔（秒）
        self._max_alerts: int = 500
        self._baseline_days: int = 20

        # asyncio 事件循环引用（由 set_event_loop 设置，用于跨线程 SSE 推送）
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

        # 统计
        self._started_at: Optional[str] = None
        self._last_poll_at: Optional[str] = None
        self._total_alerts: int = 0

        logger.info("MonitorEngine 初始化完成")

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def started_at(self) -> Optional[str]:
        return self._started_at

    @property
    def last_poll_at(self) -> Optional[str]:
        return self._last_poll_at

    @property
    def total_alerts(self) -> int:
        return self._total_alerts

    @property
    def stock_count(self) -> int:
        return len(self._stock_pool) if self._stock_pool else 0

    @property
    def interval_seconds(self) -> float:
        return self._interval

    @property
    def sse_manager(self) -> SSEManager:
        """暴露 SSEManager 供 server.py 的 SSE endpoint 使用"""
        return self._sse

    # ------------------------------------------------------------------
    # 事件循环引用
    # ------------------------------------------------------------------

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """设置 asyncio 事件循环引用，用于跨线程 SSE 推送。

        必须在启动 FastAPI app 的事件循环中调用一次。
        """
        self._event_loop = loop
        logger.info("MonitorEngine 事件循环已设置")

    # ------------------------------------------------------------------
    # 启停控制
    # ------------------------------------------------------------------

    def start(self, stock_pool: List[str]) -> Dict[str, Any]:
        """启动监控。

        Args:
            stock_pool: 股票代码列表 ['600519', '000333', ...]

        Returns:
            {"status": "started", "stock_count": int}
            {"status": "already_running"}
        """
        with self._lock:
            if self._running.is_set():
                return {"status": "already_running", "stock_count": self.stock_count}

            self._stock_pool = list(stock_pool)
            if not self._stock_pool:
                logger.warning("MonitorEngine.start: 股票池为空")
                return {"status": "error", "message": "股票池为空"}

            self._running.set()
            self._started_at = datetime.now().isoformat()
            self._alerts = []
            self._prev_snapshots = {}
            self._total_alerts = 0

            self._thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="monitor-engine",
            )
            self._thread.start()

            logger.info(f"MonitorEngine 已启动: {len(self._stock_pool)} 只股票")
            return {"status": "started", "stock_count": len(self._stock_pool)}

    def stop(self) -> Dict[str, Any]:
        """停止监控。

        Returns:
            {"status": "stopped", "total_alerts": int}
            {"status": "not_running"}
        """
        with self._lock:
            if not self._running.is_set():
                return {"status": "not_running"}

            self._running.clear()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("MonitorEngine 后台线程未能在 5 秒内退出")

        logger.info(f"MonitorEngine 已停止，累计告警: {self._total_alerts}")
        return {"status": "stopped", "total_alerts": self._total_alerts}

    # ------------------------------------------------------------------
    # 历史查询
    # ------------------------------------------------------------------

    def get_history(self, limit: int = 100) -> List[Alert]:
        """获取最近的历史告警。

        Args:
            limit: 返回条数上限，默认 100

        Returns:
            告警列表（按时间升序，最近的在后）
        """
        with self._lock:
            return list(self._alerts[-limit:])

    def get_status(self) -> Dict[str, Any]:
        """获取引擎运行状态快照。"""
        return {
            "running": self.is_running,
            "stock_count": self.stock_count,
            "total_alerts": self._total_alerts,
            "started_at": self._started_at,
            "last_poll_at": self._last_poll_at,
            "interval_seconds": self._interval,
            "sse_connections": self._sse.active_count,
        }

    # ------------------------------------------------------------------
    # 后台轮询循环
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """后台轮询主循环（运行在 daemon 线程中）。"""
        logger.info("MonitorEngine 轮询循环启动")

        # 首次启动时计算基准量
        try:
            self._compute_baselines()
        except Exception as e:
            logger.error(f"基准量计算失败: {e}")

        while self._running.is_set():
            if not TDXQuotesPoller.is_trading_time():
                time.sleep(30)  # 非交易时段慢速检查
                continue

            try:
                curr = self._poller.poll(self._stock_pool)
            except Exception as e:
                logger.error(f"轮询失败: {e}")
                time.sleep(10)
                continue

            self._last_poll_at = datetime.now().isoformat()

            if not curr:
                # 首次轮询或空结果 → 保存快照但不检测
                if self._prev_snapshots:
                    pass  # 有前次快照但本次无数据（可能都在停牌）
                else:
                    self._prev_snapshots = curr
                time.sleep(self._interval)
                continue

            # 检测大单
            prev = self._prev_snapshots
            alerts = self._detector.detect_batch(prev, curr)
            self._prev_snapshots = curr

            # 处理告警
            for alert in alerts:
                with self._lock:
                    self._alerts.append(alert)
                    self._total_alerts += 1
                    if len(self._alerts) > self._max_alerts:
                        self._alerts = self._alerts[-self._max_alerts:]

                # 跨线程推送到 SSE
                loop = self._event_loop
                if loop is not None and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._sse.push(alert), loop
                    )
                else:
                    logger.debug("事件循环未就绪，跳过 SSE 推送")

            if alerts:
                logger.info(
                    f"检测到 {len(alerts)} 条大单告警: "
                    + ", ".join(
                        f"{a.name}({a.code}) {a.direction} {a.hands}手"
                        for a in alerts[:5]
                    )
                    + ("..." if len(alerts) > 5 else "")
                )

            time.sleep(self._interval)

        logger.info("MonitorEngine 轮询循环退出")

    # ------------------------------------------------------------------
    # 基准量计算
    # ------------------------------------------------------------------

    def _compute_baselines(self) -> None:
        """计算每只股票的基准区间成交量（股 / 秒）。

        方法: 取近 20 个交易日的日均成交量，除以 14400 秒（4 小时交易时段）。
        优先使用 minute_bars 按当前时段计算，回退到 daily_bars 按全天均摊。
        """
        logger.info(f"开始计算基准量: {len(self._stock_pool)} 只股票, {self._baseline_days} 天窗口")

        try:
            from data.database import SQLiteManager
        except ImportError:
            logger.warning("无法导入 SQLiteManager，跳过基准量计算")
            return

        baselines: Dict[str, float] = {}

        now = datetime.now()
        current_time_str = now.strftime("%H:%M:%S")
        end_date = now.strftime("%Y%m%d")
        start_date = (now - timedelta(days=60)).strftime("%Y%m%d")  # 预留足够窗口

        db: Optional[SQLiteManager] = None
        try:
            db = SQLiteManager()
        except Exception as e:
            logger.warning(f"数据库连接失败，跳过基准量计算: {e}")
            return

        try:
            for code in self._stock_pool:
                ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                try:
                    vol_per_sec = self._calc_baseline_for_stock(
                        db, ts_code, start_date, end_date, current_time_str
                    )
                    if vol_per_sec is not None and vol_per_sec > 0:
                        baselines[code] = vol_per_sec
                except Exception as e:
                    logger.debug(f"基准量计算失败 {code}: {e}")

            self._detector.set_baselines(baselines)
            logger.info(
                f"基准量计算完成: {len(baselines)}/{len(self._stock_pool)} 只股票"
            )
        finally:
            try:
                db.close()
            except Exception:
                pass

    @staticmethod
    def _calc_baseline_for_stock(
        db: Any,
        ts_code: str,
        start_date: str,
        end_date: str,
        current_time_str: str,
    ) -> Optional[float]:
        """计算单只股票的基准区间成交量（股/秒）。

        优先尝试 minute_bars（精确到当前时段），
        回退到 daily_bars（全天均摊）。
        """
        # 策略 1: 使用 minute_bars 获取近 20 日同时段成交量
        try:
            # 提取当前时分以便匹配
            hour_min = current_time_str[:5]  # '14:32'
            bars = db.get_minute_bars(ts_code, start_date, end_date, 5)

            if bars and len(bars) >= 20:
                # 取最近 20 个不同交易日的对应时段数据
                daily_volumes: Dict[str, List[float]] = {}
                for row in bars:
                    trade_time = row.get("trade_time", "")
                    if not trade_time:
                        continue
                    # trade_time 格式: '2026-06-15 14:32:00'
                    try:
                        date_part = trade_time[:10]
                        time_part = trade_time[11:16]
                    except Exception:
                        continue

                    # 取当前时段前后 5 分钟的数据（放宽匹配范围）
                    if time_part >= hour_min:
                        if date_part not in daily_volumes:
                            daily_volumes[date_part] = []
                        vol = float(row.get("volume", 0) or 0)
                        daily_volumes[date_part].append(vol)

                if daily_volumes:
                    # 按日期排序，取最近 20 个交易日
                    sorted_dates = sorted(daily_volumes.keys(), reverse=True)[:20]
                    total_vol = 0.0
                    count = 0
                    for d in sorted_dates:
                        vols = daily_volumes[d]
                        if vols:
                            total_vol += vols[-1]  # 最接近当前时刻的分钟量
                            count += 1
                    if count > 0:
                        # 每分钟成交量转换为每秒成交量
                        return (total_vol / count) / 60.0
        except Exception:
            pass

        # 策略 2: 回退到 daily_bars —— 日均成交量 / 14400 秒
        try:
            rows = db.get_daily_bars(ts_code, start_date, end_date)
            if rows and len(rows) >= 5:
                # 取最近 20 个交易日的日均成交量
                recent = rows[-20:]
                total_vol = sum(
                    float(r.get("volume", 0) or 0) for r in recent
                )
                avg_daily_vol = total_vol / len(recent)
                # 4 小时交易 = 14400 秒
                return avg_daily_vol / 14400.0
        except Exception:
            pass

        return None
