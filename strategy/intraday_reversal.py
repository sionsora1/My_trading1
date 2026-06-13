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

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.max_single_weight: float = float(
            self.config.get('max_single_weight', 0.10))
        self.period: str = str(self.config.get('period', '5'))

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
        """Parse a HH:MM or HH:MM:SS string and return minutes past 09:30."""
        import re
        parts = re.split(r'[: ]', str(trade_time).strip())
        try:
            h, m = int(parts[0]), int(parts[1])
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
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(self, date: str, market_data: dict,
                         portfolio: dict) -> List[dict]:
        signals: List[dict] = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # Lazy-import DataFetcher to avoid circular dependency at module level
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
