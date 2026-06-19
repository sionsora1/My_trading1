"""
逐笔大单检测器 — 独立线程轮询逐笔成交，检测大额交易。

工作流程:
    1. 独立 daemon 线程每 3 秒轮询一次
    2. 预筛选: 只检查成交量增量 > MAD×3 的股票，避免全量查询
    3. 通过 pytdx get_transaction_data 获取逐笔成交
    4. 单笔成交额 > max(2000万, 该股近30日逐笔成交额中位数×30) 则触发
    5. 分类: 大单 / 特大单(>3×阈值且>5000万) / 巨单(>1亿)
    6. 集合竞价(buyorsell=8): 对比历史5天同时段成交量，>3×则告警

告警通过 SimpleQueue 非阻塞传递，由 MonitorEngine 消费并推送到 SSE。
"""

from __future__ import annotations

import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from broker.detector import AnomalyAlert, SimpleQueue
from config.settings import ANOMALY_DETECTOR_CONFIG, DATA_SOURCE_CONFIG
from utils.logger import get_logger

logger = get_logger('live_trading', 'live_trading.log')


class TransBigDetector:
    """逐笔大单检测器。

    通过 pytdx get_transaction_data 接口逐只获取逐笔成交，
    检测单笔成交额超过动态阈值的大单，并分类输出告警。

    Attributes:
        _queue: 告警输出队列 (SimpleQueue)
        _stock_pool: 监控股票池 (6位纯数字代码)
        _stop_event: 停止信号
        _hist_medians: 每只股票近30日逐笔成交额中位数
        _auction_history: 每只股票近5天集合竞价成交量记录
        _latest_snapshots: 最新快照 (由 MonitorEngine 注入)
        _prev_snapshots: 上一轮快照 (用于计算 delta)
    """

    def __init__(self, queue: SimpleQueue, stock_pool: List[str], **kwargs: Any):
        """初始化逐笔大单检测器。

        Args:
            queue: 非阻塞告警队列，检测到的告警通过此队列输出
            stock_pool: 股票代码列表 (6位纯数字)
            **kwargs: 额外配置项 (预留)
        """
        cfg = ANOMALY_DETECTOR_CONFIG.get('trans_big', {})

        self._queue = queue
        self._stock_pool = list(stock_pool)

        # 配置参数
        self._interval_sec: float = cfg.get('interval_sec', 3)
        self._abs_threshold: int = cfg.get('abs_threshold', 20_000_000)           # 2000万
        self._dynamic_multiple: int = cfg.get('dynamic_multiple', 30)              # ×30
        self._lookback_days: int = cfg.get('lookback_days', 30)                    # 30日
        self._super_large_multiple: int = cfg.get('super_large_multiple', 3)       # 特大单倍率
        self._super_large_abs: int = cfg.get('super_large_abs', 50_000_000)        # 5000万
        self._giant_abs: int = cfg.get('giant_abs', 100_000_000)                  # 1亿
        self._auction_days: int = cfg.get('auction_days', 5)                       # 竞价对比天数
        self._auction_multiple: int = cfg.get('auction_multiple', 3)               # 竞价异常倍率
        self._pre_filter_mad: int = cfg.get('pre_filter_mad', 3)                   # 预筛选倍率

        # TDX 服务器地址
        tdx_cfg = DATA_SOURCE_CONFIG.get('tdx', {})
        self._servers: List[Tuple[str, int]] = tdx_cfg.get(
            'servers',
            [('60.191.117.167', 7709)],
        )
        self._connect_timeout: float = float(tdx_cfg.get('connect_timeout', 5))

        # 状态
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 历史基线
        self._hist_medians: Dict[str, float] = {}               # code → 日均逐笔成交额中位数
        self._auction_history: Dict[str, List[float]] = defaultdict(list)  # code → [近5天集合竞价量(手)]

        # 快照 (由 MonitorEngine 注入)
        self._latest_snapshots: Dict[str, Any] = {}
        self._prev_snapshots: Dict[str, Any] = {}

        # 预筛选滑窗 — 维护每只股票近几轮的 volume delta {code: [(ts, delta_vol), ...]}
        self._delta_window: Dict[str, List[Tuple[float, float]]] = defaultdict(list)

        logger.info(
            f"TransBigDetector 初始化: {len(self._stock_pool)} 只股票, "
            f"阈值≥{self._abs_threshold / 1e4:.0f}万, "
            f"动态线=中位数×{self._dynamic_multiple}, "
            f"间隔={self._interval_sec}s"
        )

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def set_hist_medians(self, medians: Dict[str, float]) -> None:
        """设置每只股票的历史逐笔成交额中位数。

        由 MonitorEngine 在启动时从数据库加载后调用。

        Args:
            medians: 字典 {code: 日均逐笔成交额中位数(元)}
        """
        self._hist_medians = medians
        logger.info(f"历史逐笔中位数已加载: {len(medians)} 只")

    def set_auction_history(self, auction_data: Dict[str, List[float]]) -> None:
        """设置每只股票的集合竞价历史数据。

        Args:
            auction_data: {code: [近5天集合竞价成交量(手), ...]}
        """
        self._auction_history = defaultdict(list, auction_data)
        logger.info(f"集合竞价历史已加载: {len(auction_data)} 只")

    def update_snapshots(
        self,
        snapshots: Dict[str, Any],
        prev: Dict[str, Any],
    ) -> None:
        """更新最新行情快照 (由 MonitorEngine 每轮调用)。

        Args:
            snapshots: 当前轮快照 {code: QuoteSnapshot}
            prev: 上一轮快照 {code: QuoteSnapshot}
        """
        self._latest_snapshots = snapshots
        self._prev_snapshots = prev

        # 更新预筛选滑窗 (volume delta)
        now_ts = time.time()
        for code, snap in snapshots.items():
            p = prev.get(code) if prev else None
            if p is None:
                continue
            delta_vol = snap.volume - p.volume
            if delta_vol <= 0:
                continue
            self._delta_window[code].append((now_ts, delta_vol))
            # 清理超过 60 秒的数据
            self._delta_window[code] = [
                (t, v) for t, v in self._delta_window[code]
                if now_ts - t <= 60
            ]

    def start(self) -> None:
        """启动逐笔大单检测线程。"""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("TransBigDetector 已在运行中")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name='trans-big-detector',
        )
        self._thread.start()
        logger.info("TransBigDetector 线程已启动")

    def stop(self) -> None:
        """停止逐笔大单检测线程。"""
        self._stop_event.set()
        logger.info("TransBigDetector 停止信号已发送")

    @property
    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """独立线程主循环。

        每 3 秒执行一轮:
            1. 检查是否在交易时间，否则休眠
            2. 预筛选成交量异常的股票
            3. 连接 TDX，对候选股票获取逐笔成交
            4. 逐笔检查阈值，生成 AnomalyAlert
            5. 推送到 SimpleQueue
        """
        logger.info("TransBigDetector 轮询循环启动")

        # 延迟导入 pytdx (只在独立线程中需要)
        from pytdx.hq import TdxHq_API

        api: Optional[TdxHq_API] = None
        reconnect_cooldown = 0

        while not self._stop_event.is_set():
            try:
                # 非交易时间休眠
                if not self.is_trading_time():
                    if api is not None:
                        try:
                            api.disconnect()
                        except Exception:
                            pass
                        api = None
                    self._stop_event.wait(30)
                    continue

                # 连接 TDX (复用连接，失败时重连)
                if api is None and time.time() > reconnect_cooldown:
                    api = self._connect_tdx()

                if api is None:
                    logger.debug("TransBigDetector: TDX 未连接，跳过本轮")
                    self._stop_event.wait(self._interval_sec)
                    continue

                # 1. 预筛选: 找出成交量 delta > MAD × 3 的股票
                candidate_codes = self._pre_filter()
                if candidate_codes:
                    logger.debug(
                        f"TransBigDetector 预筛选: {len(candidate_codes)} 只候选"
                    )

                # 2. 逐只获取逐笔成交并检测
                for code in candidate_codes:
                    if self._stop_event.is_set():
                        break
                    try:
                        self._check_stock(api, code)
                    except Exception as e:
                        logger.error(
                            f"逐笔检测异常 {code}: {e}", exc_info=True
                        )
                        # 单只股票失败不影响其他股票

            except Exception as e:
                logger.error(
                    f"TransBigDetector 主循环异常: {e}", exc_info=True
                )
                # 连接可能已断开，标记重连
                if api is not None:
                    try:
                        api.disconnect()
                    except Exception:
                        pass
                    api = None
                reconnect_cooldown = time.time() + 5
                self._stop_event.wait(self._interval_sec)

            # 等待下一轮
            self._stop_event.wait(self._interval_sec)

        # 清理连接
        if api is not None:
            try:
                api.disconnect()
            except Exception:
                pass
        logger.info("TransBigDetector 轮询循环退出")

    # ------------------------------------------------------------------
    # 预筛选
    # ------------------------------------------------------------------

    def _pre_filter(self) -> List[str]:
        """预筛选成交量增量异常的股票。

        使用滑窗中的 volume delta 数据，计算 MAD，
        筛选 delta > MAD × _pre_filter_mad 的股票。
        如果没有足够数据或没有快照，返回全量股票池。

        Returns:
            候选股票代码列表
        """
        now_ts = time.time()
        candidates: List[str] = []

        for code in self._stock_pool:
            deltas = [
                v for t, v in self._delta_window.get(code, [])
                if now_ts - t <= 60
            ]
            if len(deltas) < 3:
                # 数据不足，保守起见纳入检查
                candidates.append(code)
                continue

            median = statistics.median(deltas)
            abs_dev = [abs(v - median) for v in deltas]
            mad = statistics.median(abs_dev) if abs_dev else 0.0

            if mad <= 0:
                # MAD 为 0，可能有异常，纳入检查
                candidates.append(code)
                continue

            # 最新 delta
            latest_delta = deltas[-1]
            deviation = latest_delta - median

            if deviation > mad * self._pre_filter_mad:
                candidates.append(code)
                logger.debug(
                    f"预筛选命中 {code}: delta={latest_delta:.0f}, "
                    f"median={median:.0f}, mad={mad:.0f}, "
                    f"×MAD={deviation / mad:.1f}"
                )

        return candidates if candidates else list(self._stock_pool)

    # ------------------------------------------------------------------
    # 逐笔检测
    # ------------------------------------------------------------------

    def _check_stock(self, api: Any, code: str) -> None:
        """检查单只股票的逐笔成交。

        获取最近 10 笔逐笔成交，逐笔检查成交额是否超过阈值，
        以及集合竞价是否存在异常放量。

        Args:
            api: pytdx TdxHq_API 连接实例
            code: 股票代码 (6位)
        """
        market = 1 if code.startswith('6') else 0

        try:
            transactions = api.get_transaction_data(
                market, code, start=0, count=10
            )
        except Exception as e:
            logger.debug(f"get_transaction_data 失败 {code}: {e}")
            return

        if not transactions:
            return

        now_time = datetime.now().strftime('%H:%M:%S')

        for txn in transactions:
            try:
                self._check_single_txn(code, txn, now_time)
            except Exception as e:
                logger.error(
                    f"单笔检测异常 {code}: {e}", exc_info=True
                )

    def _check_single_txn(
        self,
        code: str,
        txn: dict,
        now_time: str,
    ) -> None:
        """检查单笔逐笔成交。

        计算成交额 = price × vol × 100，
        与动态阈值比较，生成相应级别的告警。

        Args:
            code: 股票代码
            txn: 逐笔成交记录 dict{'time', 'price', 'vol', 'num', 'buyorsell'}
            now_time: 当前时间字符串
        """
        price = float(txn.get('price', 0))
        vol = int(txn.get('vol', 0))           # 手
        num = int(txn.get('num', 0))           # 笔数
        buyorsell = int(txn.get('buyorsell', 0))
        txn_time = str(txn.get('time', ''))

        # 无成交
        if price <= 0 or vol <= 0:
            return

        amount = price * vol * 100              # 成交额 (元)

        # 集合竞价 (buyorsell=8)
        if buyorsell == 8:
            self._check_auction(code, price, vol, amount, txn_time, now_time)
            return

        # 计算阈值
        threshold = self._compute_threshold(code)

        # 不满足阈值
        if amount < threshold:
            return

        # 分类
        multiple = amount / threshold

        if amount >= self._giant_abs:
            subtype = 'giant'
            level_label = '巨单'
        elif (
            amount >= threshold * self._super_large_multiple
            and amount >= self._super_large_abs
        ):
            subtype = 'super_large'
            level_label = '特大单'
        else:
            subtype = 'large'
            level_label = '大单'

        # 方向判定
        if buyorsell == 1:
            direction = 'buy'
        elif buyorsell == 2:
            direction = 'sell'
        else:
            direction = 'neutral'

        # 股票名称
        snap = self._latest_snapshots.get(code)
        name = snap.name if snap else code

        logger.info(
            f"逐笔{level_label}",
            extra={"data": {
                "code": code,
                "name": name,
                "direction": direction,
                "price": price,
                "amount": amount,
                "hands": vol,
                "num_trades": num,
                "threshold": threshold,
                "multiple": round(multiple, 2),
                "time": txn_time,
            }},
        )

        alert = AnomalyAlert(
            type='trans_big',
            subtype=subtype,
            code=code,
            name=name,
            direction=direction,
            time=txn_time or now_time,
            data={
                'price': price,
                'amount': amount,
                'hands': vol,
                'threshold': threshold,
                'multiple': round(multiple, 2),
                'num_trades': num,
            },
        )
        self._queue.put(alert)

    def _check_auction(
        self,
        code: str,
        price: float,
        vol: int,
        amount: float,
        txn_time: str,
        now_time: str,
    ) -> None:
        """检查集合竞价是否异常放量。

        对比历史 5 天同时段竞价成交量中位数，
        如果当前竞价量 > 中位数 × 3，生成 auction_spike 告警。

        Args:
            code: 股票代码
            price: 竞价价格
            vol: 竞价成交量 (手)
            amount: 竞价成交额
            txn_time: 交易时间
            now_time: 当前时间
        """
        history = self._auction_history.get(code, [])
        if len(history) < self._auction_days:
            # 历史数据不足，但仍然检查绝对额
            if amount < self._abs_threshold:
                return
        else:
            median_vol = statistics.median(history)
            if median_vol <= 0:
                return
            ratio = vol / median_vol
            if ratio < self._auction_multiple:
                # 同时检查绝对额兜底
                if amount < self._abs_threshold:
                    return

        snap = self._latest_snapshots.get(code)
        name = snap.name if snap else code

        logger.info(
            "集合竞价异常放量",
            extra={"data": {
                "code": code,
                "name": name,
                "price": price,
                "vol_hands": vol,
                "amount": amount,
                "time": txn_time,
            }},
        )

        alert = AnomalyAlert(
            type='auction',
            subtype='spike',
            code=code,
            name=name,
            direction='neutral',
            time=txn_time or now_time,
            data={
                'price': price,
                'amount': amount,
                'hands': vol,
                'threshold': self._abs_threshold,
                'multiple': (
                    vol / statistics.median(self._auction_history[code])
                    if self._auction_history.get(code)
                    else 0.0
                ),
                'num_trades': 0,
            },
        )
        self._queue.put(alert)

    # ------------------------------------------------------------------
    # 阈值计算
    # ------------------------------------------------------------------

    def _compute_threshold(self, code: str) -> float:
        """计算单只股票的动态大单阈值。

        阈值 = max(绝对下限, 该股近30日逐笔成交额中位数 × 动态倍率)

        Args:
            code: 股票代码

        Returns:
            阈值 (元)
        """
        hist_median = self._hist_medians.get(code, 0.0)
        if hist_median > 0:
            dynamic_line = hist_median * self._dynamic_multiple
            return max(float(self._abs_threshold), dynamic_line)
        return float(self._abs_threshold)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _connect_tdx(self) -> Any:
        """连接 TDX 服务器，返回 TdxHq_API 实例或 None。

        Returns:
            TdxHq_API 连接实例，连接失败返回 None
        """
        from pytdx.hq import TdxHq_API

        api = TdxHq_API(auto_retry=True, raise_exception=False)
        connected = False
        for ip, port in self._servers:
            try:
                if api.connect(ip, port, time_out=self._connect_timeout):
                    connected = True
                    logger.debug(f"TransBigDetector TDX 已连接 {ip}:{port}")
                    break
            except Exception:
                continue

        if not connected:
            logger.warning("TransBigDetector 无法连接任何 TDX 服务器")
            try:
                api.disconnect()
            except Exception:
                pass
            return None

        return api

    # ------------------------------------------------------------------
    # 交易时间判断
    # ------------------------------------------------------------------

    @staticmethod
    def is_trading_time() -> bool:
        """判断当前是否在 A 股交易时段。

        交易时段:
            周一至周五 9:25-11:30, 13:00-15:00

        集合竞价时段 9:15-9:25 也算在内，用于检测竞价异动。

        Returns:
            True 如果在交易时段内
        """
        now = datetime.now()
        # 周末
        if now.weekday() >= 5:
            return False

        t = now.time()
        # 早盘: 9:25 - 11:30 (含开盘竞价公布)
        morning_start = t.replace(hour=9, minute=25, second=0, microsecond=0)
        morning_end = t.replace(hour=11, minute=30, second=0, microsecond=0)
        # 午盘: 13:00 - 15:00 (含收盘竞价)
        afternoon_start = t.replace(hour=13, minute=0, second=0, microsecond=0)
        afternoon_end = t.replace(hour=15, minute=0, second=0, microsecond=0)

        morning = morning_start <= t <= morning_end
        afternoon = afternoon_start <= t <= afternoon_end

        return morning or afternoon
