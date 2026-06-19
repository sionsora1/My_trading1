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

from broker.detector import AnomalyAlert
from broker.monitor import QuoteSnapshot


class TurnoverDetector:
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
        self._cooldown_sec: float = kwargs.get('cooldown_sec', 60.0)

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

            # ── 冷却期检查 ──
            last_alert = self._cooldowns.get(code, 0)
            if now - last_alert < self._cooldown_sec:
                continue

            try:
                alerted = False

                # ── 日内换手率 (%) ──
                # volume 为累计成交量（股），liutong 为流通股本（股）
                daily_turnover = (snap.volume / liutong) * 100

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

                # ============================================================
                # 检测 1: 5分钟换手率增量 spike
                # ============================================================
                if five_min_median > 0 and len(window) >= 2:
                    oldest_vol = window[0][1]
                    delta_5m_vol = snap.volume - oldest_vol
                    if delta_5m_vol > 0:
                        delta_5m_turnover = (delta_5m_vol / liutong) * 100
                        multiple_5m = (
                            delta_5m_turnover / five_min_median
                            if five_min_median > 0
                            else 0.0
                        )
                        if multiple_5m >= self._five_min_multiple:
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
                            alerted = True

                # ============================================================
                # 检测 2 & 3: 全天累计换手率 hot / extreme
                # ============================================================
                if daily_median > 0:
                    daily_multiple = daily_turnover / daily_median
                    if daily_turnover >= daily_median * self._daily_extreme_multiple:
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
                        alerted = True
                    elif daily_turnover >= daily_median * self._daily_hot_multiple:
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
                        alerted = True

                # ── 记录冷却时间 ──
                if alerted:
                    self._cooldowns[code] = now

            except Exception:
                logger.error(
                    f'换手率检测异常: code={code}',
                    exc_info=True,
                )
                continue

        return alerts
