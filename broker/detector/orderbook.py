"""
盘口异动检测器 — 五档订单簿异常监控

四维子检测:
    1. 挂单突变 (bid_ask_surge)  — 任一档挂量突然暴增
    2. 盘口失衡 (imbalance)       — 买卖盘口严重倾斜
    3. 大单撤单 (cancel)          — 大挂单突然消失
    4. 价差突变 (spread_surge)    — 买卖价差突然扩大

内部状态:
    每只股票维护 60 秒滑窗，记录五档挂量和价差百分比。
    保留上一拍 QuoteSnapshot 用于撤单检测。
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from typing import Any, Dict, List

from broker.detector import AnomalyAlert, CooldownMixin
from broker.monitor import QuoteSnapshot
from config.settings import ANOMALY_DETECTOR_CONFIG
from utils.logger import get_logger

logger = get_logger('live_trading', 'live_trading.log')

# ── 五档盘口字段名（用于 getattr 动态访问） ──
_BID_VOL_FIELDS = ('bid_vol1', 'bid_vol2', 'bid_vol3', 'bid_vol4', 'bid_vol5')
_ASK_VOL_FIELDS = ('ask_vol1', 'ask_vol2', 'ask_vol3', 'ask_vol4', 'ask_vol5')
_BID_PRICE_FIELDS = ('bid1', 'bid2', 'bid3', 'bid4', 'bid5')
_ASK_PRICE_FIELDS = ('ask1', 'ask2', 'ask3', 'ask4', 'ask5')


class OrderbookDetector(CooldownMixin):
    """盘口异动检测器。

    监控五档挂单结构，检测挂单突变、盘口失衡、大单撤单、价差突变。
    每只股票独立维护 60 秒滑动窗口，使用中位数/均值作为动态基线。

    Args:
        **kwargs: 可覆盖 ANOMALY_DETECTOR_CONFIG['orderbook'] 中的参数
    """

    def __init__(self, **kwargs: Any) -> None:
        cfg = ANOMALY_DETECTOR_CONFIG.get('orderbook', {})
        self.cooldown_sec: float = float(
            kwargs.get('cooldown_sec', cfg.get('cooldown_sec', 30))
        )
        self._window_size: int = int(
            kwargs.get('window_size', cfg.get('window_size', 12))
        )
        self._bid_change_multiple: float = float(
            kwargs.get('bid_change_multiple', cfg.get('bid_change_multiple', 10))
        )
        self._bid_change_min_hands: float = float(
            kwargs.get('bid_change_min_hands', cfg.get('bid_change_min_hands', 2000))
        )
        self._imbalance_severe_low: float = float(
            kwargs.get('imbalance_severe_low', cfg.get('imbalance_severe_low', 0.2))
        )
        self._imbalance_severe_high: float = float(
            kwargs.get('imbalance_severe_high', cfg.get('imbalance_severe_high', 5))
        )
        self._imbalance_min_hands: float = float(
            kwargs.get('imbalance_min_hands', cfg.get('imbalance_min_hands', 5000))
        )
        self._cancel_disappear_hands: float = float(
            kwargs.get('cancel_disappear_hands', cfg.get('cancel_disappear_hands', 200))
        )
        self._spread_pct_threshold: float = float(
            kwargs.get('spread_pct_threshold', cfg.get('spread_pct_threshold', 0.5))
        )
        self._spread_mean_multiple: float = float(
            kwargs.get('spread_mean_multiple', cfg.get('spread_mean_multiple', 5))
        )

        # 滑动窗口: code -> deque of (timestamp, bid_vols, ask_vols, spread_pct)
        self._windows: Dict[str, deque] = {}
        # 上一拍快照: code -> QuoteSnapshot
        self._prev_snapshots: Dict[str, QuoteSnapshot] = {}
        # 冷却期: "code:subtype" -> timestamp
        self._cooldowns: Dict[str, float] = {}
        # 流通股本缓存 (按股本分档调整阈值)
        self._liutong_cache: Dict[str, float] = {}

        logger.info(
            'OrderbookDetector 初始化完成',
            extra={
                'data': {
                    'cooldown_sec': self.cooldown_sec,
                    'window_size': self._window_size,
                    'bid_change_multiple': self._bid_change_multiple,
                    'bid_change_min_hands': self._bid_change_min_hands,
                    'imbalance_severe_low': self._imbalance_severe_low,
                    'imbalance_severe_high': self._imbalance_severe_high,
                    'imbalance_min_hands': self._imbalance_min_hands,
                }
            },
        )

    # ──────────────────────────────────────────────────────────────────
    # 公开接口
    # ──────────────────────────────────────────────────────────────────

    def set_liutong_cache(self, liutong: Dict[str, float]) -> None:
        """设置流通股本缓存，用于按股本分档调整阈值。

        启动时由 MonitorEngine 调用，与 TurnoverDetector 共享同一份数据。

        Args:
            liutong: {code: float_shares}，float_shares 单位为股
        """
        self._liutong_cache = liutong
        logger.info(
            '盘口检测器: 流通股本缓存已加载',
            extra={'data': {'count': len(liutong)}},
        )

    def _get_min_hands_for_stock(self, code: str, base_min: float) -> float:
        """按流通股本分档调整最低手数阈值。

        小盘 (<5亿):  base_min * 0.25  (更容易触发)
        中盘 (5-30亿): base_min         (保持默认)
        大盘 (>30亿):   base_min * 2.5  (提高门槛避免盲区)

        Args:
            code: 股票代码
            base_min: 基准最低手数 (配置值)

        Returns:
            调整后的最低手数
        """
        liutong = self._liutong_cache.get(code, 0)
        if liutong <= 0:
            return base_min
        if liutong < 5_0000_0000:       # <5亿: 小盘
            return base_min * 0.25
        elif liutong < 30_0000_0000:     # 5-30亿: 中盘
            return base_min
        else:                             # >30亿: 大盘
            return base_min * 2.5

    def check(self, curr: Dict[str, QuoteSnapshot]) -> List[AnomalyAlert]:
        """检测当前快照全集中的盘口异动。

        Args:
            curr: 当前快照字典 {code: QuoteSnapshot}

        Returns:
            检测到的 AnomalyAlert 列表
        """
        alerts: List[AnomalyAlert] = []
        now = time.time()

        for code, snap in curr.items():
            if not self._valid_snapshot(snap):
                continue

            # 更新滑动窗口
            self._update_window(code, snap)

            # 依次检查四种异动
            try:
                bid_ask_alerts = self._check_bid_ask_surge(code, snap, now)
                alerts.extend(bid_ask_alerts)
            except Exception:
                logger.error(f'挂单突变检测异常 {code}', exc_info=True)

            try:
                imbalance_alerts = self._check_imbalance(code, snap, now)
                alerts.extend(imbalance_alerts)
            except Exception:
                logger.error(f'盘口失衡检测异常 {code}', exc_info=True)

            try:
                cancel_alerts = self._check_cancel(code, snap, now)
                alerts.extend(cancel_alerts)
            except Exception:
                logger.error(f'大单撤单检测异常 {code}', exc_info=True)

            try:
                spread_alerts = self._check_spread_surge(code, snap, now)
                alerts.extend(spread_alerts)
            except Exception:
                logger.error(f'价差突变检测异常 {code}', exc_info=True)

        # 保留当前快照作为下一轮的"上一拍"
        self._prev_snapshots = dict(curr)

        return alerts

    # ──────────────────────────────────────────────────────────────────
    # 窗口管理
    # ──────────────────────────────────────────────────────────────────

    def _update_window(self, code: str, snap: QuoteSnapshot) -> None:
        """向滑动窗口追加一拍数据，并清理过期条目。

        Args:
            code: 股票代码
            snap: 当前快照
        """
        if code not in self._windows:
            self._windows[code] = deque()

        window = self._windows[code]
        now = time.time()

        bid_vols = tuple(getattr(snap, f) for f in _BID_VOL_FIELDS)
        ask_vols = tuple(getattr(snap, f) for f in _ASK_VOL_FIELDS)
        spread_pct = self._calc_spread_pct(snap)

        window.append((now, bid_vols, ask_vols, spread_pct))

        # 清理 60 秒以前的条目
        while window and now - window[0][0] > 60.0:
            window.popleft()

    # ──────────────────────────────────────────────────────────────────
    # 子检测 1: 挂单突变
    # ──────────────────────────────────────────────────────────────────

    def _check_bid_ask_surge(
        self, code: str, snap: QuoteSnapshot, now: float
    ) -> List[AnomalyAlert]:
        """检测挂单突变：任一档挂量相对于 60 秒中位数暴增。

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            触发的告警列表
        """
        window = self._windows.get(code)
        if window is None or len(window) < self._window_size:
            return []

        if not self._acquire_cooldown(code, 'bid_ask_surge', now):
            return []

        # 计算各档位的历史中位数
        bid_medians = self._level_medians(window, 0)  # bid vols 在 tuple idx 1
        ask_medians = self._level_medians(window, 1)  # ask vols 在 tuple idx 2

        curr_bid_vols = tuple(getattr(snap, f) for f in _BID_VOL_FIELDS)
        curr_ask_vols = tuple(getattr(snap, f) for f in _ASK_VOL_FIELDS)

        alerts: List[AnomalyAlert] = []

        # 检查买方挂单突变
        for i in range(5):
            median = bid_medians[i]
            cur_vol = curr_bid_vols[i]
            threshold = median * self._bid_change_multiple
            if cur_vol > threshold and cur_vol > self._get_min_hands_for_stock(code, self._bid_change_min_hands):
                alerts.append(
                    AnomalyAlert(
                        type='orderbook',
                        subtype='bid_ask_surge',
                        code=code,
                        name=snap.name,
                        direction='buy',
                        time=snap.time,
                        data={
                            'side': 'bid',
                            'level': i + 1,
                            'current_hands': round(cur_vol, 0),
                            'median_hands': round(median, 0),
                            'multiple': round(cur_vol / median, 2) if median > 0 else 999.0,
                            'price': snap.bid1 if i == 0 else getattr(snap, _BID_PRICE_FIELDS[i]),
                        },
                    )
                )

        # 检查卖方挂单突变
        for i in range(5):
            median = ask_medians[i]
            cur_vol = curr_ask_vols[i]
            threshold = median * self._bid_change_multiple
            if cur_vol > threshold and cur_vol > self._get_min_hands_for_stock(code, self._bid_change_min_hands):
                alerts.append(
                    AnomalyAlert(
                        type='orderbook',
                        subtype='bid_ask_surge',
                        code=code,
                        name=snap.name,
                        direction='sell',
                        time=snap.time,
                        data={
                            'side': 'ask',
                            'level': i + 1,
                            'current_hands': round(cur_vol, 0),
                            'median_hands': round(median, 0),
                            'multiple': round(cur_vol / median, 2) if median > 0 else 999.0,
                            'price': snap.ask1 if i == 0 else getattr(snap, _ASK_PRICE_FIELDS[i]),
                        },
                    )
                )

        if alerts:
            logger.info(
                '挂单突变',
                extra={
                    'data': {
                        'code': code,
                        'count': len(alerts),
                    }
                },
            )

        return alerts

    # ──────────────────────────────────────────────────────────────────
    # 子检测 2: 盘口失衡
    # ──────────────────────────────────────────────────────────────────

    def _check_imbalance(
        self, code: str, snap: QuoteSnapshot, now: float
    ) -> List[AnomalyAlert]:
        """检测盘口失衡：买卖总挂量比例极端倾斜。

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            触发的告警列表
        """
        if not self._acquire_cooldown(code, 'imbalance', now):
            return []

        total_bid = sum(getattr(snap, f) for f in _BID_VOL_FIELDS)
        total_ask = sum(getattr(snap, f) for f in _ASK_VOL_FIELDS)

        if total_ask <= 0:
            return []

        ratio = total_bid / total_ask

        # 判断是否严重失衡
        if self._imbalance_severe_low < ratio < self._imbalance_severe_high:
            return []

        # 检查大的一侧是否超过最小挂量阈值
        larger_side = max(total_bid, total_ask)
        if larger_side <= self._imbalance_min_hands:
            return []

        if ratio <= self._imbalance_severe_low:
            direction = 'sell'
            description = f'卖盘压单严重: 买/卖={ratio:.2f}'
        else:
            direction = 'buy'
            description = f'买盘托单严重: 买/卖={ratio:.2f}'

        alert = AnomalyAlert(
            type='orderbook',
            subtype='imbalance',
            code=code,
            name=snap.name,
            direction=direction,
            time=snap.time,
            data={
                'ratio': round(ratio, 2),
                'total_bid_hands': round(total_bid, 0),
                'total_ask_hands': round(total_ask, 0),
                'larger_side_hands': round(larger_side, 0),
                'description': description,
            },
        )

        logger.info(
            '盘口失衡',
            extra={
                'data': {
                    'code': code,
                    'ratio': round(ratio, 2),
                    'direction': direction,
                }
            },
        )

        return [alert]

    # ──────────────────────────────────────────────────────────────────
    # 子检测 3: 大单撤单
    # ──────────────────────────────────────────────────────────────────

    def _check_cancel(
        self, code: str, snap: QuoteSnapshot, now: float
    ) -> List[AnomalyAlert]:
        """检测大单撤单：上一拍有大挂单，当前拍消失。

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            触发的告警列表
        """
        prev = self._prev_snapshots.get(code)
        if prev is None:
            return []

        if not self._acquire_cooldown(code, 'cancel', now):
            return []

        window = self._windows.get(code)
        if window is None or len(window) < self._window_size:
            return []

        # 计算上一拍时的历史中位数（使用当前窗口内除最新以外的数据来近似）
        # 更精确的做法是用上一拍之前的数据，这里用窗口内数据近似
        bid_medians = self._level_medians(window, 0)
        ask_medians = self._level_medians(window, 1)

        prev_bid_vols = tuple(getattr(prev, f) for f in _BID_VOL_FIELDS)
        prev_ask_vols = tuple(getattr(prev, f) for f in _ASK_VOL_FIELDS)
        curr_bid_vols = tuple(getattr(snap, f) for f in _BID_VOL_FIELDS)
        curr_ask_vols = tuple(getattr(snap, f) for f in _ASK_VOL_FIELDS)

        alerts: List[AnomalyAlert] = []

        # 检查买方撤单
        for i in range(5):
            median = bid_medians[i]
            prev_vol = prev_bid_vols[i]
            cur_vol = curr_bid_vols[i]
            surge_threshold = median * self._bid_change_multiple
            # 上一拍是巨量挂单
            if prev_vol > surge_threshold and prev_vol > self._get_min_hands_for_stock(code, self._bid_change_min_hands):
                # 当前拍消失
                if cur_vol < self._cancel_disappear_hands:
                    alerts.append(
                        AnomalyAlert(
                            type='orderbook',
                            subtype='cancel',
                            code=code,
                            name=snap.name,
                            direction='buy',
                            time=snap.time,
                            data={
                                'side': 'bid',
                                'level': i + 1,
                                'prev_hands': round(prev_vol, 0),
                                'current_hands': round(cur_vol, 0),
                                'withdrawn_hands': round(prev_vol - cur_vol, 0),
                                'price': snap.bid1 if i == 0 else getattr(snap, _BID_PRICE_FIELDS[i]),
                            },
                        )
                    )

        # 检查卖方撤单
        for i in range(5):
            median = ask_medians[i]
            prev_vol = prev_ask_vols[i]
            cur_vol = curr_ask_vols[i]
            surge_threshold = median * self._bid_change_multiple
            # 上一拍是巨量挂单
            if prev_vol > surge_threshold and prev_vol > self._get_min_hands_for_stock(code, self._bid_change_min_hands):
                # 当前拍消失
                if cur_vol < self._cancel_disappear_hands:
                    alerts.append(
                        AnomalyAlert(
                            type='orderbook',
                            subtype='cancel',
                            code=code,
                            name=snap.name,
                            direction='sell',
                            time=snap.time,
                            data={
                                'side': 'ask',
                                'level': i + 1,
                                'prev_hands': round(prev_vol, 0),
                                'current_hands': round(cur_vol, 0),
                                'withdrawn_hands': round(prev_vol - cur_vol, 0),
                                'price': snap.ask1 if i == 0 else getattr(snap, _ASK_PRICE_FIELDS[i]),
                            },
                        )
                    )

        if alerts:
            logger.info(
                '大单撤单',
                extra={
                    'data': {
                        'code': code,
                        'count': len(alerts),
                    }
                },
            )

        return alerts

    # ──────────────────────────────────────────────────────────────────
    # 子检测 4: 价差突变
    # ──────────────────────────────────────────────────────────────────

    def _check_spread_surge(
        self, code: str, snap: QuoteSnapshot, now: float
    ) -> List[AnomalyAlert]:
        """检测价差突变：买卖价差百分比突然扩大。

        Args:
            code: 股票代码
            snap: 当前快照
            now: 当前时间戳

        Returns:
            触发的告警列表
        """
        if not self._acquire_cooldown(code, 'spread_surge', now):
            return []

        window = self._windows.get(code)
        if window is None or len(window) < self._window_size:
            return []

        spread_pct = self._calc_spread_pct(snap)

        # 提取窗口内的 spread_pct 序列
        spread_pcts = [entry[3] for entry in window]
        mean_spread = statistics.mean(spread_pcts) if spread_pcts else 0.0

        # 双重条件: spread_pct > 阈值 AND spread_pct > 均值 × 倍数
        if spread_pct > self._spread_pct_threshold and spread_pct > mean_spread * self._spread_mean_multiple:
            spread = snap.ask1 - snap.bid1
            alert = AnomalyAlert(
                type='orderbook',
                subtype='spread_surge',
                code=code,
                name=snap.name,
                direction='neutral',
                time=snap.time,
                data={
                    'spread': round(spread, 2),
                    'spread_pct': round(spread_pct, 2),
                    'mean_spread_pct': round(mean_spread, 2),
                    'multiple': round(spread_pct / mean_spread, 2) if mean_spread > 0 else 999.0,
                    'bid1': snap.bid1,
                    'ask1': snap.ask1,
                },
            )

            logger.info(
                '价差突变',
                extra={
                    'data': {
                        'code': code,
                        'spread_pct': round(spread_pct, 2),
                        'mean_spread_pct': round(mean_spread, 2),
                    }
                },
            )

            return [alert]

        return []

    # ──────────────────────────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _valid_snapshot(snap: QuoteSnapshot) -> bool:
        """验证快照盘口数据完整性。

        Args:
            snap: 待验证快照

        Returns:
            盘口数据是否完整有效
        """
        if snap.price <= 0:
            return False
        if snap.bid1 <= 0 or snap.ask1 <= 0:
            return False
        # 至少买卖一档有挂量
        if snap.bid_vol1 <= 0 and snap.ask_vol1 <= 0:
            return False
        return True

    @staticmethod
    def _calc_spread_pct(snap: QuoteSnapshot) -> float:
        """计算价差百分比。

        Args:
            snap: 行情快照

        Returns:
            spread_pct = (ask1 - bid1) / bid1 * 100
        """
        if snap.bid1 <= 0:
            return 0.0
        return (snap.ask1 - snap.bid1) / snap.bid1 * 100.0

    @staticmethod
    def _level_medians(window: deque, vol_idx: int) -> List[float]:
        """计算窗口内各档位挂量的中位数。

        Args:
            window: 滑动窗口 deque
            vol_idx: 0 表示 bid_vols, 1 表示 ask_vols

        Returns:
            五档中位数列表 [median_lvl1, ..., median_lvl5]
        """
        medians = []
        for level in range(5):
            values = [entry[vol_idx + 1][level] for entry in window]
            if values:
                medians.append(statistics.median(values))
            else:
                medians.append(0.0)
        return medians

