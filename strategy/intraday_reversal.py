"""
Intraday Reversal Strategy.

Detects V-bottom (bullish reversal) and A-top (bearish reversal) patterns
on 5-minute bar data.  Signals are only generated after the first 30 minutes
of trading to avoid opening noise.

Depends on :class:`data.fetcher.DataFetcher.get_minute_data` for intraday bars.
"""

from typing import List, Optional

from .base import BaseStrategy

# Typical A-share trading session start
_TRADING_START_MINUTES = 9 * 60 + 30  # 09:30


class IntradayReversalStrategy(BaseStrategy):
    """Intraday reversal strategy using 5-minute bars.

    Config keys:
        max_single_weight (float): Max weight per BUY/SELL signal
                                   (default 0.10).
        period (str):              Bar period in minutes ('5' default).
    """

    def __init__(self, config: Optional[dict] = None, fetcher=None):
        super().__init__(config)
        self.max_single_weight: float = float(
            self.config.get('max_single_weight', 0.10))
        self.period: str = str(self.config.get('period', '1'))  # 默认1分钟K线
        self._fetcher = fetcher  # 注入 DataFetcher，避免每次独立连接 TDX

    # ------------------------------------------------------------------
    # Pattern detectors
    # ------------------------------------------------------------------

    @staticmethod
    def _bars_to_lists(bars) -> tuple:
        """Convert bars (DataFrame or list of dicts) to uniform lists."""
        if hasattr(bars, 'to_dict'):
            records = bars.to_dict(orient='records')
        else:
            records = list(bars)

        closes = [float(r.get('close', 0) or 0) for r in records]
        volumes = [float(r.get('volume', 0) or 0) for r in records]
        highs = [float(r.get('high', 0) or 0) for r in records]
        lows = [float(r.get('low', 0) or 0) for r in records]
        opens = [float(r.get('open', 0) or 0) for r in records]
        return closes, volumes, highs, lows, opens

    @classmethod
    def _detect_v_reversal(cls, bars, daily_stock: dict = None) -> bool:
        """Detect V-bottom reversal in the last 20 bars.

        Pattern requirements:
          1. A down-leg at the start (first ~10 bars declining).
          2. A volume spike at the trough (volume > 1.5× average).
          3. A recovery of at least 1.5 % from the trough low to the last bar's
             close.

        Returns True when a valid V-bottom is identified.
        """
        if len(bars) < 20:
            return False

        closes, volumes, highs, lows, _ = cls._bars_to_lists(bars)
        recent = closes[-20:]
        recent_vols = volumes[-20:]
        recent_lows = lows[-20:]

        # --- Trough detection ---
        trough_idx = None
        min_low = None
        for i in range(5, len(recent)):
            low_val = recent_lows[i]
            if min_low is None or low_val < min_low:
                min_low = low_val
                trough_idx = i

        if trough_idx is None or trough_idx >= len(recent) - 3:
            return False  # trough too close to end

        # --- Down leg (before trough) ---
        pre_trough = recent[:trough_idx + 1]
        if len(pre_trough) < 3:
            return False
        first_half_avg = sum(pre_trough[:max(1, len(pre_trough) // 3)]) / \
            max(1, len(pre_trough) // 3)
        second_half_avg = sum(pre_trough[-max(1, len(pre_trough) // 3):]) / \
            max(1, len(pre_trough) // 3)
        if second_half_avg > first_half_avg:
            return False  # not a down leg

        # --- Volume spike at trough ---
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 1
        trough_vol = recent_vols[trough_idx]
        if avg_vol <= 0 or trough_vol < 1.5 * avg_vol:
            return False

        # --- Recovery ---
        last_close = closes[-1]
        if min_low <= 0:
            return False
        recovery = (last_close - min_low) / min_low
        return recovery >= 0.015

    @classmethod
    def _detect_a_reversal(cls, bars) -> bool:
        """Detect A-top reversal in the last 20 bars.

        Pattern requirements:
          1. An up-leg at the start (rising prices).
          2. A peak bar with a long upper shadow (high - max(open,close))
             exceeding 1 % of the close.
          3. A subsequent decline.

        Returns True when a valid A-top is identified.
        """
        if len(bars) < 20:
            return False

        closes, volumes, highs, lows, opens = cls._bars_to_lists(bars)
        recent_closes = closes[-20:]
        recent_highs = highs[-20:]
        recent_lows = lows[-20:]
        recent_opens = opens[-20:]

        # --- Peak detection (highest high in the window) ---
        peak_idx = None
        max_high = None
        for i in range(3, len(recent_highs) - 3):
            h = recent_highs[i]
            if max_high is None or h > max_high:
                max_high = h
                peak_idx = i

        if peak_idx is None:
            return False

        # --- Up leg (before peak) ---
        pre_peak = recent_closes[:peak_idx + 1]
        if len(pre_peak) < 3:
            return False
        first_avg = sum(pre_peak[:max(1, len(pre_peak) // 3)]) / \
            max(1, len(pre_peak) // 3)
        last_avg = sum(pre_peak[-max(1, len(pre_peak) // 3):]) / \
            max(1, len(pre_peak) // 3)
        if last_avg < first_avg:
            return False  # not an up leg

        # --- Long upper shadow at peak ---
        peak_close = recent_closes[peak_idx]
        peak_open = recent_opens[peak_idx]
        peak_high = recent_highs[peak_idx]
        peak_body_top = max(peak_close, peak_open)
        if peak_body_top <= 0:
            return False
        upper_shadow_pct = (peak_high - peak_body_top) / peak_body_top
        if upper_shadow_pct < 0.01:
            return False

        # --- Decline after peak ---
        post_peak = recent_closes[peak_idx:]
        if len(post_peak) < 3:
            return False
        if post_peak[-1] >= post_peak[0]:
            return False  # no decline

        return True

    # ------------------------------------------------------------------
    # Time filter
    # ------------------------------------------------------------------

    @staticmethod
    def _minutes_since_open(trade_time: str) -> int:
        """Parse a time string and return minutes past 09:30.

        Handles: 'HH:MM', 'HH:MM:SS', 'YYYY-MM-DD HH:MM:SS'.
        """
        import re
        s = str(trade_time).strip()
        # 提取末尾的时间部分 (HH:MM 或 HH:MM:SS)
        time_match = re.search(r'(\d{1,2}):(\d{2})', s)
        if not time_match:
            return 0
        try:
            h, m = int(time_match.group(1)), int(time_match.group(2))
            total = h * 60 + m
            return max(0, total - _TRADING_START_MINUTES)
        except (ValueError, IndexError):
            return 0

    def _after_first_30_min(self, bars) -> bool:
        """Return True if the *last* bar is more than 30 minutes into the session."""
        if len(bars) == 0:
            return False
        if hasattr(bars, 'iloc'):
            last_time = str(bars['trade_time'].iloc[-1])
        elif isinstance(bars, list):
            last_bar = bars[-1]
            last_time = str(last_bar.get('trade_time', ''))
        else:
            return True  # cannot determine — allow
        return self._minutes_since_open(last_time) >= 30

    # ------------------------------------------------------------------
    # 独立盯盘检测（批量预取分钟线 + SSE 告警）
    # ------------------------------------------------------------------

    def detect_reversals(self, minute_bars_map: dict) -> List[dict]:
        """用预取的分钟线检测 V底/A顶，返回告警 dict 列表。

        Args:
            minute_bars_map: {code: bars_df_or_list} 批量预取的分钟K线

        Returns:
            [{'code': str, 'type': 'v_reversal'|'a_reversal',
              'name': str, 'price': float, 'time': str}, ...]
        """
        alerts: List[dict] = []
        for code, bars in minute_bars_map.items():
            try:
                if bars is None or len(bars) < 20:
                    continue
                if not self._after_first_30_min(bars):
                    continue

                v_info = self._detect_v_reversal_with_info(bars)
                if v_info:
                    alerts.append({
                        'code': code, 'type': 'v_reversal',
                        'name': '', 'price': v_info['trough_price'],
                        'time': v_info['trough_time'],
                        'confirm_price': v_info['confirm_price'],
                        'confirm_time': v_info['confirm_time'],
                        'end_price': v_info['end_price'],
                        'end_time': v_info['end_time'],
                        'recovery_pct': v_info['recovery_pct'],
                    })
                else:
                    a_info = self._detect_a_reversal_with_info(bars)
                    if a_info:
                        alerts.append({
                            'code': code, 'type': 'a_reversal',
                            'name': '', 'price': a_info['peak_price'],
                            'time': a_info['peak_time'],
                            'end_price': a_info['end_price'],
                            'end_time': a_info['end_time'],
                        })
            except Exception:
                continue
        return alerts

    @classmethod
    def _detect_v_reversal_with_info(cls, bars) -> dict | None:
        """检测V底反转，返回谷底/确认点信息或None。

        Returns:
            {'trough_price': float, 'trough_time': str,
             'confirm_price': float, 'confirm_time': str,
             'end_price': float, 'end_time': str, 'recovery_pct': float}
        """
        if not cls._detect_v_reversal(bars):
            return None
        recent = list(bars[-20:])
        closes = [float(b.get('close', 0)) for b in recent]
        lows = [float(b.get('low', 0)) for b in recent]

        # 找谷底: 最低点
        trough_idx = min(range(5, len(recent) - 3), key=lambda i: lows[i])
        trough_price = lows[trough_idx]
        end_price = closes[-1]

        # 找反弹确认点: 从谷底往后，首次突破 1.5% 的那根K线
        confirm_idx = None
        confirm_price = None
        for i in range(trough_idx + 1, len(recent)):
            if trough_price > 0 and (closes[i] - trough_price) / trough_price >= 0.015:
                confirm_idx = i
                confirm_price = closes[i]
                break

        recovery = (end_price - trough_price) / trough_price * 100

        trough_bar = recent[trough_idx]
        end_bar = recent[-1]
        trough_time = cls._extract_time(trough_bar.get('trade_time', ''))
        end_time = cls._extract_time(end_bar.get('trade_time', ''))
        confirm_time = cls._extract_time(recent[confirm_idx].get('trade_time', '')) if confirm_idx is not None else end_time
        if confirm_price is None:
            confirm_price = end_price

        return {
            'trough_price': round(trough_price, 2),
            'trough_time': trough_time,
            'confirm_price': round(confirm_price, 2),
            'confirm_time': confirm_time,
            'end_price': round(end_price, 2),
            'end_time': end_time,
            'recovery_pct': round(recovery, 2),
        }

    @classmethod
    def _detect_a_reversal_with_info(cls, bars) -> dict | None:
        """检测A顶反转，返回峰顶/终点信息或None。"""
        if not cls._detect_a_reversal(bars):
            return None
        recent = list(bars[-20:])
        highs = [float(b.get('high', 0)) for b in recent]

        # 找峰顶: 最高点（排除首尾各3根）
        peak_idx = max(range(3, len(recent) - 3), key=lambda i: highs[i])
        peak_price = highs[peak_idx]
        end_price = float(recent[-1].get('close', 0))

        peak_bar = recent[peak_idx]
        end_bar = recent[-1]
        peak_time = cls._extract_time(peak_bar.get('trade_time', ''))
        end_time = cls._extract_time(end_bar.get('trade_time', ''))

        return {
            'peak_price': round(peak_price, 2),
            'peak_time': peak_time,
            'end_price': round(end_price, 2),
            'end_time': end_time,
        }

    @staticmethod
    def _extract_time(trade_time) -> str:
        """从 'YYYY-MM-DD HH:MM:SS' 或 'HH:MM' 中提取 HH:MM。"""
        import re
        s = str(trade_time).strip()
        m = re.search(r'(\d{2}:\d{2})', s)
        return m.group(1) if m else s[:5]

    @staticmethod
    def _last_price(bars):
        if hasattr(bars, 'iloc'):
            return float(bars['close'].iloc[-1])
        return float(bars[-1].get('close', 0))

    @staticmethod
    def _last_time(bars):
        if hasattr(bars, 'iloc'):
            return str(bars['trade_time'].iloc[-1])
        return str(bars[-1].get('trade_time', ''))

    # ------------------------------------------------------------------
    # Signal generation (兼容 SignalBus)
    # ------------------------------------------------------------------

    def generate_signals(self, date: str, market_data: dict,
                         portfolio: dict) -> List[dict]:
        signals: List[dict] = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 使用注入的 DataFetcher（由 LiveTradingServer 统一管理）
        fetcher = self._fetcher
        if fetcher is None:
            from data.fetcher import DataFetcher
            fetcher = DataFetcher()

        # Build time window: fetch only today's bars (date format: YYYYMMDD)
        start_time = f"{date[:4]}-{date[4:6]}-{date[6:8]} 09:00:00"
        end_time = f"{date[:4]}-{date[4:6]}-{date[6:8]} 15:30:00"

        for code, stock in market_data.items():
            try:
                bars = fetcher.get_minute_data(
                    ts_code=code,
                    period=self.period,
                    start_time=start_time,
                    end_time=end_time,
                )
                if bars is None or (hasattr(bars, 'empty') and bars.empty):
                    continue
                if len(bars) < 20:
                    continue

                # Time filter: only after first 30 min of trading
                if not self._after_first_30_min(bars):
                    continue

                # Get latest bar price for signal
                if hasattr(bars, 'iloc'):
                    last_close = float(bars['close'].iloc[-1] or 0)
                else:
                    last_close = float(bars[-1].get('close', 0) or 0)

                if last_close <= 0:
                    continue

                # --- V-bottom → BUY ---
                if self._detect_v_reversal(bars, stock):
                    if code not in current_positions:
                        signals.append({
                            'ts_code': code,
                            'signal': 'BUY',
                            'weight': self.max_single_weight,
                            'reason': '盘中V型反转',
                        })

                # --- A-top → SELL ---
                if self._detect_a_reversal(bars):
                    if code in current_positions:
                        signals.append({
                            'ts_code': code,
                            'signal': 'SELL',
                            'weight': 0,
                            'reason': '盘中A型反转',
                        })

            except Exception:
                # Skip stocks where minute data fetch fails
                continue

        return signals
