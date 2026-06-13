"""
Enhanced Trend-Following Strategy.

Multi-factor trend scoring (0-1) with MA alignment, deviation from MA20,
volume confirmation, and momentum.  Selects the top-N stocks by composite
score and sells when price drops 2 % below the 20-day moving average.
"""

from typing import List, Optional

from .base import BaseStrategy


class EnhancedTrendFollowingStrategy(BaseStrategy):
    """Multi-factor trend-following strategy.

    Builds a 0-1 composite score from four sub-factors:
      1. MA alignment
      2. Deviation from MA20
      3. Volume confirmation
      4. Momentum (20-day return)

    Config keys:
        top_n (int):             Number of stocks to hold (default 5).
        max_single_weight (float): Max weight per BUY signal (default 0.15).
    """

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config)
        self.top_n: int = int(self.config.get('top_n', 5))
        self.max_single_weight: float = float(
            self.config.get('max_single_weight', 0.15))

    # ------------------------------------------------------------------
    # Sub-factor scorers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_ma_alignment(stock: dict) -> float:
        """Score the MA alignment pattern (0→0.4).

        - Perfect bull alignment (close > ma5 > ma10 > ma20 > ma60): 0.40
        - Partial alignment  (close > ma20 > ma60):                  0.25
        - Simple alignment   (close > ma60):                         0.10
        """
        close = stock.get('close', 0) or 0
        ma5 = stock.get('ma5', close) or close
        ma10 = stock.get('ma10', close) or close
        ma20 = stock.get('ma20', close) or close
        ma60 = stock.get('ma60', close) or close

        if close > ma5 > ma10 > ma20 > ma60:
            return 0.40
        if close > ma20 > ma60:
            return 0.25
        if close > ma60:
            return 0.10
        return 0.0

    @staticmethod
    def _score_deviation(stock: dict) -> float:
        """Score deviation from MA20 (0→0.2).

        - 2-15 % above MA20: 0.20
        - 0-2 % above MA20:  0.10
        """
        close = stock.get('close', 0) or 0
        ma20 = stock.get('ma20', close) or close
        if ma20 <= 0 or close <= 0:
            return 0.0
        dev = (close - ma20) / ma20
        if 0.02 <= dev <= 0.15:
            return 0.20
        if 0.0 <= dev < 0.02:
            return 0.10
        return 0.0

    @staticmethod
    def _score_volume(stock: dict) -> float:
        """Score volume confirmation (0→0.2).

        - Volume > 1.2× MA volume AND 20-day return > 0: 0.20
        - Volume > 0.8× MA volume:                       0.10
        """
        volume = stock.get('volume', 0) or 0
        ma_vol = stock.get('volume_ma20', volume) or volume
        return_20d = stock.get('return_20d', 0) or 0.0

        if ma_vol <= 0:
            return 0.0
        ratio = volume / ma_vol
        if ratio > 1.2 and return_20d > 0:
            return 0.20
        if ratio > 0.8:
            return 0.10
        return 0.0

    @staticmethod
    def _score_momentum(stock: dict) -> float:
        """Score momentum (0→0.2).

        - 20-day return > 15 %: 0.20
        - 20-day return > 5 %:  0.10
        """
        r20 = stock.get('return_20d', 0) or 0.0
        if r20 > 0.15:
            return 0.20
        if r20 > 0.05:
            return 0.10
        return 0.0

    # ------------------------------------------------------------------
    # Composite scoring
    # ------------------------------------------------------------------

    def _composite_score(self, stock: dict) -> float:
        """Return the total trend score (0-1) for a single stock."""
        return (
            self._score_ma_alignment(stock) +
            self._score_deviation(stock) +
            self._score_volume(stock) +
            self._score_momentum(stock)
        )

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def generate_signals(self, date: str, market_data: dict,
                         portfolio: dict) -> List[dict]:
        signals: List[dict] = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # --- Score every stock ---
        scored = []
        for code, stock in market_data.items():
            score = self._composite_score(stock)
            if score > 0:
                scored.append((code, score))

        # Sort descending by score, with stable tie-break on code
        scored.sort(key=lambda x: (-x[1], x[0]))

        # Top-N selection
        selected = {code for code, _ in scored[:self.top_n]}

        # --- BUY signals ---
        for code in selected:
            if code not in current_positions:
                stock = market_data.get(code, {})
                score = next((s for c, s in scored if c == code), 0)
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': (
                        f'趋势跟随（得分{score:.2f}）'
                    ),
                })

        # --- SELL signals: close < ma20 * 0.98 (2 % buffer) ---
        for code in list(current_positions):
            stock = market_data.get(code)
            if stock is None:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': '数据缺失，平仓',
                })
                continue

            close = stock.get('close', 0) or 0
            ma20 = stock.get('ma20', close) or close
            if close > 0 and close < ma20 * 0.98:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': '跌破MA20（2%缓冲）',
                })

            # Also sell stocks that are no longer in the top-N and are
            # underwater — classic trend-following exit.
            elif code not in selected:
                pos = portfolio['positions'].get(code, {})
                profit = pos.get('profit_rate', 0)
                if profit < -0.03:
                    signals.append({
                        'ts_code': code,
                        'signal': 'SELL',
                        'weight': 0,
                        'reason': f'趋势排名下降且亏损{profit:.1%}',
                    })

        return signals
