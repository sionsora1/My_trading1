"""
涨跌停加速检测器 — 封板松动 / 撬板信号 / 逼近加速

三个子检测:
    1. seal_loosen — 封板松动 (封板买盘萎缩, 可能开板)
    2. pry_signal  — 撬板信号 (跌停板出现巨大买单, 可能被撬开)
    3. approach    — 逼近加速 (加速冲向涨跌停, 可能反转)

每只股票独立维护 5 分钟时间窗口 (价格/成交量/买一挂量)。
同股票同子类型 30 秒冷却。
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from broker.detector import AnomalyAlert
from broker.monitor import QuoteSnapshot
from config.settings import ANOMALY_DETECTOR_CONFIG
from utils.logger import get_logger

logger = get_logger('live_trading', 'live_trading.log')


class LimitMoveDetector:
    """涨跌停加速检测器。

    启动时缓存每只股票的涨跌停价格，运行时维护 5 分钟窗口数据，
    检测封板松动、撬板信号、逼近加速三种异动。

    Attributes:
        near_limit_pct: 封板/撬板判断的涨跌幅阈值 (默认 9.5%)
        approach_pct: 逼近加速的涨跌幅阈值 (默认 8.0%)
        seal_loosen_vol_ratio: 封单松动挂量比 (默认 0.5)
        pry_signal_vol_ratio: 撬板信号挂量比 (默认 10)
        approach_price_change: 逼近加速价格变化阈值 % (默认 3.0)
        approach_turnover_multiple: 逼近加速成交量倍率 (默认 3)
        cooldown_sec: 同股票同子类型冷却时间 (默认 30 秒)
        window_sec: 时间窗口大小 (默认 300 秒 = 5 分钟)
    """

    def __init__(
        self,
        near_limit_pct: float | None = None,
        approach_pct: float | None = None,
        seal_loosen_vol_ratio: float | None = None,
        pry_signal_vol_ratio: float | None = None,
        approach_price_change: float | None = None,
        approach_turnover_multiple: float | None = None,
        cooldown_sec: float | None = None,
        window_sec: float | None = None,
    ):
        """初始化检测器，参数未提供时从配置读取默认值。

        Args:
            near_limit_pct: 封板/撬板判断的涨跌幅阈值 (%)
            approach_pct: 逼近加速的涨跌幅阈值 (%)
            seal_loosen_vol_ratio: 封单松动挂量比阈值
            pry_signal_vol_ratio: 撬板信号挂量比阈值
            approach_price_change: 逼近加速价格变化阈值 (%)
            approach_turnover_multiple: 逼近加速成交量倍率
            cooldown_sec: 同股票同子类型冷却时间 (秒)
            window_sec: 时间窗口大小 (秒)
        """
        cfg = ANOMALY_DETECTOR_CONFIG.get('limit_move', {})

        self.near_limit_pct = near_limit_pct if near_limit_pct is not None else cfg.get('near_limit_pct', 9.5)
        self.approach_pct = approach_pct if approach_pct is not None else cfg.get('approach_pct', 8.0)
        self.seal_loosen_vol_ratio = (
            seal_loosen_vol_ratio if seal_loosen_vol_ratio is not None
            else cfg.get('seal_loosen_vol_ratio', 0.5)
        )
        self.pry_signal_vol_ratio = (
            pry_signal_vol_ratio if pry_signal_vol_ratio is not None
            else cfg.get('pry_signal_vol_ratio', 10)
        )
        self.approach_price_change = (
            approach_price_change if approach_price_change is not None
            else cfg.get('approach_price_change', 3.0)
        )
        self.approach_turnover_multiple = (
            approach_turnover_multiple if approach_turnover_multiple is not None
            else cfg.get('approach_turnover_multiple', 3)
        )
        self.cooldown_sec = cooldown_sec if cooldown_sec is not None else cfg.get('cooldown_sec', 30)
        self.window_sec = window_sec if window_sec is not None else cfg.get('window_sec', 300)

        # 涨跌停价格缓存 {code: (limit_up, limit_down)}
        self._limit_prices: Dict[str, Tuple[float, float]] = {}
        # 每只股票的时间窗口 {(timestamp, price, volume, bid_vol1), ...}
        self._windows: Dict[str, List[Tuple[float, float, float, float]]] = {}
        # 冷却期 {f"{code}:{subtype}": last_alert_timestamp}
        self._cooldowns: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # 主板 / 科创板 / 创业板 涨跌停幅度
    # ------------------------------------------------------------------

    @staticmethod
    def _get_limit_rate(code: str) -> float:
        """根据股票代码返回涨跌停幅度。

        主板 (60xxxx / 00xxxx): ±10%
        科创板 (688xxx):       ±20%
        创业板 (300xxx):       ±20%

        Args:
            code: 股票代码

        Returns:
            涨跌停幅度 (小数)
        """
        if code.startswith('688'):
            return 0.20
        if code.startswith('300'):
            return 0.20
        return 0.10

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def set_limit_prices(
        self,
        stock_pool: List[str],
        snapshots: Dict[str, QuoteSnapshot],
    ) -> None:
        """启动时一次性计算所有股票的涨跌停价格。

        根据昨收价和板块涨跌停幅度计算 limit_up / limit_down。
        仅对 stock_pool 中且 snapshots 中有有效 last_close 的股票计算。

        Args:
            stock_pool: 监控股票池
            snapshots: 初始快照 {code: QuoteSnapshot}
        """
        count = 0
        for code in stock_pool:
            snap = snapshots.get(code)
            if snap is None:
                continue
            last_close = snap.last_close
            if last_close <= 0:
                logger.warning(f"涨跌停价格初始化: {code} last_close 无效 ({last_close})")
                continue
            rate = self._get_limit_rate(code)
            limit_up = round(last_close * (1 + rate), 3)
            limit_down = round(last_close * (1 - rate), 3)
            self._limit_prices[code] = (limit_up, limit_down)
            count += 1

        logger.info(f"涨跌停价格初始化完成: {count}/{len(stock_pool)} 只股票")

    def check(self, curr: Dict[str, QuoteSnapshot]) -> List[AnomalyAlert]:
        """对当前快照执行三种涨跌停检测，返回告警列表。

        Args:
            curr: 当前轮 {code: QuoteSnapshot}

        Returns:
            AnomalyAlert 列表，type='limit_move'
        """
        alerts: List[AnomalyAlert] = []
        now = time.time()

        for code, snap in curr.items():
            try:
                # 更新时间窗口
                self._update_window(code, snap, now)

                # 1) 封板松动
                alert = self._check_seal_loosen(code, snap, now)
                if alert:
                    alerts.append(alert)

                # 2) 撬板信号
                alert = self._check_pry_signal(code, snap, now)
                if alert:
                    alerts.append(alert)

                # 3) 逼近加速
                alert = self._check_approach(code, snap, now)
                if alert:
                    alerts.append(alert)

            except Exception as e:
                logger.error(
                    f"涨跌停检测异常 {code} {snap.name}: {e}",
                    exc_info=True,
                )

        return alerts

    # ------------------------------------------------------------------
    # 子检测 1: 封板松动
    # ------------------------------------------------------------------

    def _check_seal_loosen(
        self,
        code: str,
        snap: QuoteSnapshot,
        now: float,
    ) -> AnomalyAlert | None:
        """封板松动: 涨停板附近买盘挂量急剧萎缩，可能开板。

        条件:
            1. change_pct > near_limit_pct (默认为 9.5%)
            2. 窗口内至少有 min_window_entries 个 bid1_vol 样本
            3. 当前 bid1_vol < 窗口 bid1_vol 中位数 × seal_loosen_vol_ratio
            4. 不在冷却期

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            AnomalyAlert 或 None
        """
        # 必须接近涨停
        if snap.change_pct <= self.near_limit_pct:
            return None

        window = self._windows.get(code, [])
        min_entries = 6  # 至少 6 个样本 (~30s 数据)
        if len(window) < min_entries:
            return None

        bid1_vals = [entry[3] for entry in window if entry[3] > 0]
        if len(bid1_vals) < min_entries:
            return None

        median_bid1 = statistics.median(bid1_vals)
        if median_bid1 <= 0:
            return None

        threshold = median_bid1 * self.seal_loosen_vol_ratio

        # 当前买一挂量低于中位数 × 阈值比例
        if snap.bid_vol1 >= threshold:
            return None

        # 冷却检查
        if not self._acquire_cooldown(code, 'seal_loosen', now):
            return None

        collapse_pct = round((1 - snap.bid_vol1 / median_bid1) * 100, 1) if median_bid1 > 0 else 0

        logger.info("封板松动", extra={"data": {
            "code": code, "name": snap.name,
            "price": snap.price, "change_pct": round(snap.change_pct, 3),
            "bid_vol1": snap.bid_vol1,
            "median_bid1": round(median_bid1, 2),
            "collapse_pct": collapse_pct,
        }})

        return AnomalyAlert(
            type='limit_move',
            subtype='seal_loosen',
            code=code,
            name=snap.name,
            direction='sell',
            time=snap.time,
            data={
                'price': snap.price,
                'change_pct': round(snap.change_pct, 3),
                'bid_vol1': snap.bid_vol1,
                'median_bid1': round(median_bid1, 2),
                'collapse_pct': collapse_pct,
            },
        )

    # ------------------------------------------------------------------
    # 子检测 2: 撬板信号
    # ------------------------------------------------------------------

    def _check_pry_signal(
        self,
        code: str,
        snap: QuoteSnapshot,
        now: float,
    ) -> AnomalyAlert | None:
        """撬板信号: 跌停板附近买盘挂量远超卖盘，可能被撬开。

        条件:
            1. change_pct < -near_limit_pct (默认为 -9.5%)
            2. bid_vol1 > 0 且 ask_vol1 > 0
            3. bid_vol1 > ask_vol1 × pry_signal_vol_ratio
            4. 不在冷却期

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            AnomalyAlert 或 None
        """
        # 必须接近跌停
        if snap.change_pct >= -self.near_limit_pct:
            return None

        ask_vol1 = snap.ask_vol1
        bid_vol1 = snap.bid_vol1

        if ask_vol1 <= 0 or bid_vol1 <= 0:
            return None

        # 买一挂量 > 卖一挂量 × 倍率
        if bid_vol1 <= ask_vol1 * self.pry_signal_vol_ratio:
            return None

        # 冷却检查
        if not self._acquire_cooldown(code, 'pry_signal', now):
            return None

        ratio = round(bid_vol1 / ask_vol1, 1)

        logger.info("撬板信号", extra={"data": {
            "code": code, "name": snap.name,
            "price": snap.price, "change_pct": round(snap.change_pct, 3),
            "bid_vol1": bid_vol1, "ask_vol1": ask_vol1,
            "ratio": ratio,
        }})

        return AnomalyAlert(
            type='limit_move',
            subtype='pry_signal',
            code=code,
            name=snap.name,
            direction='buy',
            time=snap.time,
            data={
                'price': snap.price,
                'change_pct': round(snap.change_pct, 3),
                'bid_vol1': bid_vol1,
                'ask_vol1': ask_vol1,
                'ratio': ratio,
            },
        )

    # ------------------------------------------------------------------
    # 子检测 3: 逼近加速
    # ------------------------------------------------------------------

    def _check_approach(
        self,
        code: str,
        snap: QuoteSnapshot,
        now: float,
    ) -> AnomalyAlert | None:
        """逼近加速: 股价加速冲向涨跌停，可能反转。

        条件:
            1. |change_pct| > approach_pct (默认为 8.0%)
            2. 窗口内至少有 min_window_entries 条记录
            3. |当前价 - 窗口最早价| / 最早价 × 100 > approach_price_change
            4. 最近一笔成交量增量 > 窗口内成交量增量中位数 × approach_turnover_multiple
            5. 不在冷却期

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            AnomalyAlert 或 None
        """
        # |change_pct| > approach_pct
        if abs(snap.change_pct) <= self.approach_pct:
            return None

        window = self._windows.get(code, [])
        min_entries = 6
        if len(window) < min_entries:
            return None

        # 价格变化 vs 窗口内最早价格
        oldest_price = window[0][1]
        if oldest_price <= 0:
            return None

        price_change_pct = abs((snap.price - oldest_price) / oldest_price * 100)
        if price_change_pct <= self.approach_price_change:
            return None

        # 成交量增量倍率检测
        deltas = []
        for i in range(1, len(window)):
            prev_vol = window[i - 1][2]
            curr_vol = window[i][2]
            if prev_vol >= 0 and curr_vol >= prev_vol:
                deltas.append(curr_vol - prev_vol)

        if len(deltas) < 3:
            return None

        median_delta = statistics.median(deltas)
        if median_delta <= 0:
            return None

        latest_delta = deltas[-1]
        if latest_delta <= median_delta * self.approach_turnover_multiple:
            return None

        # 冷却检查
        if not self._acquire_cooldown(code, 'approach', now):
            return None

        # 方向: 逼近涨停 → sell, 逼近跌停 → buy
        if snap.change_pct > 0:
            direction = 'sell'
            limit_label = '涨停'
        else:
            direction = 'buy'
            limit_label = '跌停'

        surge_multiple = round(latest_delta / median_delta, 1)

        logger.info("逼近加速", extra={"data": {
            "code": code, "name": snap.name,
            "price": snap.price, "change_pct": round(snap.change_pct, 3),
            "price_change_pct": round(price_change_pct, 3),
            "direction": limit_label,
            "volume_surge_multiple": surge_multiple,
        }})

        return AnomalyAlert(
            type='limit_move',
            subtype='approach',
            code=code,
            name=snap.name,
            direction=direction,
            time=snap.time,
            data={
                'price': snap.price,
                'change_pct': round(snap.change_pct, 3),
                'price_change_pct': round(price_change_pct, 3),
                'limit_direction': limit_label,
                'volume_surge_multiple': surge_multiple,
            },
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _update_window(
        self,
        code: str,
        snap: QuoteSnapshot,
        now: float,
    ) -> None:
        """更新股票的时间窗口。

        追加当前快照数据并清理超过 window_sec 的过期条目。

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳
        """
        if code not in self._windows:
            self._windows[code] = []

        window = self._windows[code]
        window.append((now, snap.price, snap.volume, snap.bid_vol1))

        # 移除超过窗口大小的过期条目
        cutoff = now - self.window_sec
        while window and window[0][0] < cutoff:
            window.pop(0)

        # 防止内存膨胀 (5s 轮询 × 300s = 60 条, 上限 300 条)
        max_entries = 300
        if len(window) > max_entries:
            window[:] = window[-max_entries:]

    def _acquire_cooldown(self, code: str, subtype: str, now: float) -> bool:
        """检查并设置冷却期。

        如果当前时间距离上次该股票该子类型的告警在冷却期内，返回 False；
        否则更新时间戳并返回 True。

        Args:
            code: 股票代码
            subtype: 子类型标识
            now: 当前时间戳

        Returns:
            True 如果允许触发, False 如果在冷却期
        """
        key = f"{code}:{subtype}"
        last = self._cooldowns.get(key, 0.0)
        if now - last < self.cooldown_sec:
            return False
        self._cooldowns[key] = now
        return True
