"""
内外盘背离检测器 — 价量方向背离 + 极端单边失衡

三个子检测:
    1. price_up_sell_more  — 价涨内盘大 (价格涨但主动卖多, 可能诱多)
    2. price_down_buy_more — 价跌外盘大 (价格跌但主动买多, 可能吸筹)
    3. extreme_imbalance    — 极端失衡 (持续 ≥3 拍单边主导, 比 > 3:1)

每只股票独立维护 5 拍价格历史 + 极端失衡持续计数。
同股票同子类型 30 秒冷却。
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List

from broker.detector import AnomalyAlert, CooldownMixin
from broker.monitor import QuoteSnapshot
from config.settings import ANOMALY_DETECTOR_CONFIG
from utils.logger import get_logger

logger = get_logger('live_trading', 'live_trading.log')


class DivergenceDetector(CooldownMixin):
    """内外盘背离检测器。

    输入当前快照和上一轮快照的 diff，同时维护每只股票的短期价格窗口
    和极端失衡持续时间，检测三种背离信号。

    Attributes:
        cooldown_sec: 同股票同子类型冷却时间 (默认 30 秒)
        window_size: 价格历史窗口大小 (默认 5 拍)
        imbalance_ratio: 内外盘不均衡比 (默认 1.5)
        extreme_ratio: 极端背离比 (默认 3.0)
        extreme_duration: 极端持续拍数阈值 (默认 3)
    """

    def __init__(
        self,
        cooldown_sec: float | None = None,
        window_size: int | None = None,
        imbalance_ratio: float | None = None,
        extreme_ratio: float | None = None,
        extreme_duration: int | None = None,
    ):
        """初始化检测器，参数未提供时从配置读取默认值。

        Args:
            cooldown_sec: 同股票同子类型冷却时间 (秒)
            window_size: 价格历史窗口大小 (拍)
            imbalance_ratio: 价涨内盘大 / 价跌外盘大 的倍率阈值
            extreme_ratio: 极端背离比阈值
            extreme_duration: 极端背离持续拍数阈值
        """
        cfg = ANOMALY_DETECTOR_CONFIG.get('divergence', {})

        self.cooldown_sec = cooldown_sec if cooldown_sec is not None else cfg.get('cooldown_sec', 30)
        self.window_size = window_size if window_size is not None else cfg.get('window_size', 5)
        self.imbalance_ratio = imbalance_ratio if imbalance_ratio is not None else cfg.get('imbalance_ratio', 1.5)
        self.extreme_ratio = extreme_ratio if extreme_ratio is not None else cfg.get('extreme_ratio', 3.0)
        self.extreme_duration = extreme_duration if extreme_duration is not None else cfg.get('extreme_duration', 3)

        # 每只股票的价格历史 {code: [(timestamp, price), ...]}
        self._price_history: Dict[str, list] = {}
        # 每只股票的极端失衡持续计数器 {code: count}
        self._imbalance_counters: Dict[str, int] = defaultdict(int)
        # 冷却期 {f"{code}:{subtype}": last_alert_timestamp}
        self._cooldowns: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 公共主入口
    # ------------------------------------------------------------------

    def check(
        self,
        curr: Dict[str, QuoteSnapshot],
        prev: Dict[str, QuoteSnapshot],
    ) -> List[AnomalyAlert]:
        """对当前快照执行三种背离检测，返回告警列表。

        Args:
            curr: 当前轮 {code: QuoteSnapshot}
            prev: 上一轮 {code: QuoteSnapshot} (上一拍的快照)

        Returns:
            AnomalyAlert 列表，type='divergence'
        """
        alerts: List[AnomalyAlert] = []
        now = time.time()

        for code, snap in curr.items():
            try:
                # 更新价格历史
                self._update_price_history(code, snap.price)

                # 子检测 1 & 2: 需要上一拍数据
                prev_snap = prev.get(code) if prev else None

                # 1) 价涨内盘大
                alert = self._check_price_up_sell_more(code, snap, prev_snap, now)
                if alert:
                    alerts.append(alert)

                # 2) 价跌外盘大
                alert = self._check_price_down_buy_more(code, snap, prev_snap, now)
                if alert:
                    alerts.append(alert)

                # 3) 极端背离
                alert = self._check_extreme_imbalance(code, snap, now)
                if alert:
                    alerts.append(alert)

            except Exception as e:
                logger.error(
                    f"背离检测异常 {code} {snap.name}: {e}",
                    exc_info=True,
                )

        return alerts

    # ------------------------------------------------------------------
    # 子检测 1: 价涨内盘大
    # ------------------------------------------------------------------

    def _check_price_up_sell_more(
        self,
        code: str,
        snap: QuoteSnapshot,
        prev_snap: QuoteSnapshot | None,
        now: float,
    ) -> AnomalyAlert | None:
        """价涨内盘大: 价格涨但主动卖单远超主动买单。

        条件:
            1. 当前价 > 5拍前价格 (或上一拍价格作为近似)
            2. active_sell > active_buy × imbalance_ratio
            3. 不在冷却期

        Args:
            code: 股票代码
            snap: 当前快照
            prev_snap: 上一拍快照 (用于价格比较)
            now: 当前时间戳

        Returns:
            AnomalyAlert 或 None
        """
        if prev_snap is None:
            return None

        # 价格是否在涨 (当前 > 上一拍)
        if snap.price <= prev_snap.price:
            return None

        # 有足够的内外盘数据
        buy = snap.active_buy
        sell = snap.active_sell
        if buy <= 0 or sell <= 0:
            return None

        ratio = sell / buy

        # 主动卖 > 主动买 × imbalance_ratio
        if sell <= buy * self.imbalance_ratio:
            return None

        # 冷却检查
        if not self._acquire_cooldown(code, 'price_up_sell_more', now):
            return None

        price_delta_pct = (snap.price - prev_snap.price) / prev_snap.price * 100

        logger.info("价涨内盘大", extra={"data": {
            "code": code, "name": snap.name,
            "price": snap.price, "price_delta_pct": round(price_delta_pct, 3),
            "active_buy": buy, "active_sell": sell,
            "ratio": round(ratio, 2),
        }})

        return AnomalyAlert(
            type='divergence',
            subtype='price_up_sell_more',
            code=code,
            name=snap.name,
            direction='sell',
            time=snap.time,
            data={
                'price': snap.price,
                'price_delta_pct': round(price_delta_pct, 3),
                'active_buy': buy,
                'active_sell': sell,
                'ratio': round(ratio, 2),
            },
        )

    # ------------------------------------------------------------------
    # 子检测 2: 价跌外盘大
    # ------------------------------------------------------------------

    def _check_price_down_buy_more(
        self,
        code: str,
        snap: QuoteSnapshot,
        prev_snap: QuoteSnapshot | None,
        now: float,
    ) -> AnomalyAlert | None:
        """价跌外盘大: 价格跌但主动买单远超主动卖单。

        条件:
            1. 当前价 < 上一拍价格
            2. active_buy > active_sell × imbalance_ratio
            3. 不在冷却期

        Args:
            code: 股票代码
            snap: 当前快照
            prev_snap: 上一拍快照
            now: 当前时间戳

        Returns:
            AnomalyAlert 或 None
        """
        if prev_snap is None:
            return None

        # 价格是否在跌
        if snap.price >= prev_snap.price:
            return None

        buy = snap.active_buy
        sell = snap.active_sell
        if buy <= 0 or sell <= 0:
            return None

        ratio = buy / sell

        # 主动买 > 主动卖 × imbalance_ratio
        if buy <= sell * self.imbalance_ratio:
            return None

        # 冷却检查
        if not self._acquire_cooldown(code, 'price_down_buy_more', now):
            return None

        price_delta_pct = (snap.price - prev_snap.price) / prev_snap.price * 100

        logger.info("价跌外盘大", extra={"data": {
            "code": code, "name": snap.name,
            "price": snap.price, "price_delta_pct": round(price_delta_pct, 3),
            "active_buy": buy, "active_sell": sell,
            "ratio": round(ratio, 2),
        }})

        return AnomalyAlert(
            type='divergence',
            subtype='price_down_buy_more',
            code=code,
            name=snap.name,
            direction='buy',
            time=snap.time,
            data={
                'price': snap.price,
                'price_delta_pct': round(price_delta_pct, 3),
                'active_buy': buy,
                'active_sell': sell,
                'ratio': round(ratio, 2),
            },
        )

    # ------------------------------------------------------------------
    # 子检测 3: 极端背离
    # ------------------------------------------------------------------

    def _check_extreme_imbalance(
        self,
        code: str,
        snap: QuoteSnapshot,
        now: float,
    ) -> AnomalyAlert | None:
        """极端背离: 持续 N 拍单边主导, 买卖比 > extreme_ratio。

        使用 max/min 计算比例以避免分母方向性问题。
        比例 > extreme_ratio 时计数器 +1，不满足时重置。
        连续达到 extreme_duration 拍时触发告警。

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            AnomalyAlert 或 None
        """
        buy = snap.active_buy
        sell = snap.active_sell

        if buy <= 0 or sell <= 0:
            self._imbalance_counters[code] = 0
            return None

        larger = max(buy, sell)
        smaller = min(buy, sell)
        ratio = larger / smaller

        if ratio > self.extreme_ratio:
            self._imbalance_counters[code] += 1
        else:
            self._imbalance_counters[code] = 0
            return None

        duration = self._imbalance_counters[code]

        # 未达到持续拍数阈值
        if duration < self.extreme_duration:
            return None

        # 冷却检查
        if not self._acquire_cooldown(code, 'extreme_imbalance', now):
            return None

        # 判定主导方向
        if buy > sell:
            dominant = 'buy'
        else:
            dominant = 'sell'

        logger.info("极端背离", extra={"data": {
            "code": code, "name": snap.name,
            "price": snap.price,
            "active_buy": buy, "active_sell": sell,
            "ratio": round(ratio, 2),
            "duration": duration,
            "dominant": dominant,
        }})

        # 触发后重置计数器避免连续重复触发
        self._imbalance_counters[code] = 0

        return AnomalyAlert(
            type='divergence',
            subtype='extreme_imbalance',
            code=code,
            name=snap.name,
            direction='neutral',
            time=snap.time,
            data={
                'price': snap.price,
                'price_delta_pct': 0.0,
                'active_buy': buy,
                'active_sell': sell,
                'ratio': round(ratio, 2),
                'duration': duration,
                'dominant': dominant,
            },
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _update_price_history(self, code: str, price: float) -> None:
        """更新股票的价格历史窗口。

        Args:
            code: 股票代码
            price: 当前价格
        """
        if code not in self._price_history:
            self._price_history[code] = []
        history = self._price_history[code]
        now = time.time()
        history.append((now, price))
        # 保留最近 window_size 拍
        if len(history) > self.window_size:
            history[:] = history[-self.window_size:]
