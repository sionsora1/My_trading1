"""
TurnoverDetector — 换手率异动检测器

监控换手率异常变化：
1. 5分钟增量超过自身5分钟中位数的 N 倍 → spike
2. 全天累计超过自身30天日均中位数的 N 倍 → hot / extreme

输入: QuoteSnapshot (volume, code, name, price, time)
依赖: 流通股本缓存 + 历史中位数 (启动时注入)
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import Any, Dict, List

_project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import ANOMALY_DETECTOR_CONFIG
from utils.logger import get_logger

logger = get_logger('live_trading', 'live_trading.log')

from broker.detector import AnomalyAlert, CooldownMixin
from broker.monitor import QuoteSnapshot


class TurnoverDetector(CooldownMixin):
    """换手率异动检测器。

    检测三种异常等级：
        spike:   5分钟换手率增量 > 历史5分钟中位数 × five_min_multiple
        hot:     全天换手率 > 历史日均换手率 × daily_hot_multiple
        extreme: 全天换手率 > 历史日均换手率 × daily_extreme_multiple

    每只股票 60 秒冷却期防止刷屏。
    若流通股本缓存中无该股票则跳过。
    """

    def __init__(self, **kwargs):
        # ── 加载配置默认值 ──
        cfg = ANOMALY_DETECTOR_CONFIG.get('turnover', {})
        self._lookback_days: int = kwargs.get('lookback_days', cfg.get('lookback_days', 30))
        self._five_min_multiple: float = kwargs.get(
            'five_min_multiple', cfg.get('five_min_multiple', 5)
        )
        self._daily_hot_multiple: float = kwargs.get(
            'daily_hot_multiple', cfg.get('daily_hot_multiple', 3)
        )
        self._daily_extreme_multiple: float = kwargs.get(
            'daily_extreme_multiple', cfg.get('daily_extreme_multiple', 5)
        )

        # ── 流通股本缓存 {code: float_shares} ──
        self._liutong_cache: Dict[str, float] = {}

        # ── 历史中位数 {code: {'daily': float, '5min': float}} ──
        self._hist_medians: Dict[str, dict] = {}

        # ── 5分钟滑窗 {code: deque([(timestamp, cumulative_volume), ...])} ──
        self._five_min_windows: Dict[str, deque] = {}

        # ── 冷却期 {code: last_alert_timestamp} ──
        self._cooldowns: Dict[str, float] = {}
        self.cooldown_sec: float = kwargs.get('cooldown_sec', 60.0)

    def set_liutong_cache(self, liutong: Dict[str, float]) -> None:
        """设置流通股本缓存。

        启动时由 MonitorEngine 调用，从 DB finance_detail 表加载。

        Args:
            liutong: {code: float_shares}，float_shares 单位为股
        """
        self._liutong_cache = liutong
        logger.info(
            '换手率检测器: 流通股本缓存已加载',
            extra={'data': {'count': len(liutong)}},
        )

    def set_hist_medians(self, medians: Dict[str, dict]) -> None:
        """设置历史中位数。

        启动时由 MonitorEngine 调用，从 DB daily_bars 表计算。

        Args:
            medians: {code: {'daily': float, '5min': float}}
                     daily 为近 N 天日换手率中位数 (%)
                     5min 为近 N 天 5 分钟增量换手率中位数 (%)
        """
        self._hist_medians = medians
        logger.info(
            '换手率检测器: 历史中位数已加载',
            extra={'data': {'count': len(medians)}},
        )

    def _get_fallback_daily_median(self, liutong: float) -> float:
        """DB 无历史数据时，按流通股本估算日均换手率 (%)。

        基于 81 只股票池近 60 日实测数据的分档统计:
            <5亿 (小盘): 5%  /  5-30亿 (中盘): 2%  /  >30亿 (大盘): 1%

        Args:
            liutong: 流通股本 (股)

        Returns:
            估算的日均换手率 (%)
        """
        if liutong < 5_0000_0000:       # <5亿: 小盘
            return 5.0
        elif liutong < 30_0000_0000:     # 5-30亿: 中盘
            return 2.0
        else:                             # >30亿: 大盘
            return 1.0

    @staticmethod
    def _trading_day_elapsed() -> float:
        """返回当前已过交易时间的比例 (0.0 ~ 1.0)。

        9:30→11:30 (120min) + 13:00→15:00 (120min) = 240分钟。
        非交易时段返回 0.0。
        """
        from datetime import datetime, time
        now = datetime.now()
        if now.weekday() >= 5:
            return 0.0  # 周末
        t = now.time()
        morning_start = time(9, 30)
        morning_end = time(11, 30)
        afternoon_start = time(13, 0)
        afternoon_end = time(15, 0)

        elapsed = 0
        if t < morning_start:
            return 0.0
        if morning_start <= t <= morning_end:
            elapsed = (now.hour * 60 + now.minute) - (9 * 60 + 30)
        elif morning_end < t < afternoon_start:
            elapsed = 120  # 午休期间已过完整上午
        elif afternoon_start <= t <= afternoon_end:
            elapsed = 120 + (now.hour * 60 + now.minute) - (13 * 60)
        else:
            return 1.0  # 收盘后
        return max(0.0, min(1.0, elapsed / 240.0))

    def check(self, curr: Dict[str, QuoteSnapshot]) -> List[AnomalyAlert]:
        """检测换手率异动。

        对每只股票：
        1. 计算日内累计换手率 = volume / liutong × 100%
        2. 维护 5 分钟滑窗，计算最新 5 分钟换手率增量
        3. 与历史中位数比对，触发相应告警

        Args:
            curr: 当前行情快照 {code: QuoteSnapshot}

        Returns:
            AnomalyAlert 列表 (type='turnover')
        """
        alerts: List[AnomalyAlert] = []
        now = time.time()

        for code, snap in curr.items():
            # ── 必须要有流通股本 ──
            liutong = self._liutong_cache.get(code)
            if not liutong or liutong <= 0:
                continue

            try:

                # ── 日内换手率 (%) ──
                # snap.volume 为 TDX 成交量（手），需 ×100 转股
                # liutong 为流通股本（股）
                daily_turnover = (snap.volume * 100 / liutong) * 100

                # ── 维护 5 分钟滑窗 ──
                if code not in self._five_min_windows:
                    self._five_min_windows[code] = deque()
                window = self._five_min_windows[code]
                window.append((now, snap.volume))

                # 清理超过 5 分钟 (300 秒) 的旧数据
                while window and now - window[0][0] > 300:
                    window.popleft()

                # ── 获取历史中位数 ──
                hist = self._hist_medians.get(code, {})
                daily_median = hist.get('daily', 0.0)
                five_min_median = hist.get('5min', 0.0)

                # 回退: DB 无历史数据时，按流通股本分档估算
                if daily_median <= 0 and liutong > 0:
                    daily_median = self._get_fallback_daily_median(liutong)
                if five_min_median <= 0 and daily_median > 0:
                    five_min_median = daily_median / 48.0  # 48个5分钟/交易日

                # ============================================================
                # 检测 1: 5分钟换手率增量 spike
                # ============================================================
                if five_min_median > 0 and len(window) >= 2:
                    oldest_vol = window[0][1]
                    delta_5m_vol = snap.volume - oldest_vol
                    if delta_5m_vol > 0:
                        delta_5m_turnover = (delta_5m_vol * 100 / liutong) * 100
                        multiple_5m = (
                            delta_5m_turnover / five_min_median
                            if five_min_median > 0
                            else 0.0
                        )
                        if multiple_5m >= self._five_min_multiple:
                            if self._acquire_cooldown(code, 'spike', now):
                                alerts.append(AnomalyAlert(
                                    type='turnover',
                                    subtype='spike',
                                    code=code,
                                    name=snap.name,
                                    direction='neutral',
                                    time=snap.time,
                                    data={
                                        'daily_turnover_pct': round(daily_turnover, 3),
                                        'delta_5m_pct': round(delta_5m_turnover, 3),
                                        'median_5m': round(five_min_median, 3),
                                        'multiple': round(multiple_5m, 1),
                                        'price': snap.price,
                                    },
                                ))
                                logger.info(
                                    '换手率异动: spike',
                                    extra={'data': {
                                        'code': code,
                                        'name': snap.name,
                                        'daily_turnover_pct': round(daily_turnover, 3),
                                        'delta_5m_pct': round(delta_5m_turnover, 3),
                                        'multiple': round(multiple_5m, 1),
                                    }},
                                )

                # ============================================================
                # 检测 2 & 3: 全天累计换手率 hot / extreme
                # ============================================================
                if daily_median > 0:
                    # 日内时间加权：当前应完成全天成交的 elapsed_ratio
                    # 9:30→11:30 + 13:00→15:00 = 240分钟
                    elapsed_ratio = self._trading_day_elapsed()
                    adjusted_median = daily_median * elapsed_ratio if elapsed_ratio > 0 else daily_median
                    daily_multiple = daily_turnover / adjusted_median if adjusted_median > 0 else 0.0
                    if daily_turnover >= adjusted_median * self._daily_extreme_multiple:
                        if self._acquire_cooldown(code, 'extreme', now):
                            alerts.append(AnomalyAlert(
                                type='turnover',
                                subtype='extreme',
                                code=code,
                                name=snap.name,
                                direction='neutral',
                                time=snap.time,
                                data={
                                    'daily_turnover_pct': round(daily_turnover, 3),
                                    'delta_5m_pct': 0.0,
                                    'median_5m': round(daily_median, 3),
                                    'multiple': round(daily_multiple, 1),
                                    'price': snap.price,
                                },
                            ))
                            logger.info(
                                '换手率异动: extreme',
                                extra={'data': {
                                    'code': code,
                                    'name': snap.name,
                                    'daily_turnover_pct': round(daily_turnover, 3),
                                    'daily_median': round(daily_median, 3),
                                    'multiple': round(daily_multiple, 1),
                                }},
                            )
                    elif daily_turnover >= adjusted_median * self._daily_hot_multiple:
                        if self._acquire_cooldown(code, 'hot', now):
                            alerts.append(AnomalyAlert(
                                type='turnover',
                                subtype='hot',
                                code=code,
                                name=snap.name,
                                direction='neutral',
                                time=snap.time,
                                data={
                                    'daily_turnover_pct': round(daily_turnover, 3),
                                    'delta_5m_pct': 0.0,
                                    'median_5m': round(daily_median, 3),
                                    'multiple': round(daily_multiple, 1),
                                    'price': snap.price,
                                },
                            ))
                            logger.info(
                                '换手率异动: hot',
                                extra={'data': {
                                    'code': code,
                                    'name': snap.name,
                                    'daily_turnover_pct': round(daily_turnover, 3),
                                    'daily_median': round(daily_median, 3),
                                    'multiple': round(daily_multiple, 1),
                                }},
                            )

            except Exception:
                logger.error(
                    f'换手率检测异常: code={code}',
                    exc_info=True,
                )
                continue

        return alerts
