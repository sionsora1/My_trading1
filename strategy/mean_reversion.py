"""
Enhanced Mean-Reversion Strategy.

Identifies oversold stocks using a 0-1 composite score built from:
  1. Deviation from MA20
  2. Short-term oversold (5-day return)
  3. Medium-term decline (20-day return)
  4. 1-year price percentile

Filters out stocks in strong downtrends (ma20 < ma60 AND return_20d < -15%).
Sells when profit exceeds 5 %, or when close > ma20 with profit > 2 %.
"""

from typing import List, Optional

from .base import BaseStrategy


class EnhancedMeanReversionStrategy(BaseStrategy):
    """Multi-factor mean-reversion strategy.

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
    # Downtrend filter
    # ------------------------------------------------------------------

    @staticmethod
    def _is_strong_downtrend(stock: dict) -> bool:
        """Return True if the stock is in a strong downtrend.

        Criteria: ma20 < ma60 AND 20-day return < -15 %.
        """
        ma20 = stock.get('ma20', 0) or 0
        ma60 = stock.get('ma60', 0) or 0
        return_20d = stock.get('return_20d', 0) or 0.0

        return (ma20 > 0 and ma60 > 0
                and ma20 < ma60
                and return_20d < -0.15)

    # ------------------------------------------------------------------
    # Sub-factor scorers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_ma_deviation(stock: dict) -> float:
        """Score deviation below MA20 (0→0.35).

        - -15 % to -3 % from MA20: 0.35
        - -3 % to 0 % from MA20:   0.15
        """
        close = stock.get('close', 0) or 0
        ma20 = stock.get('ma20', close) or close
        if ma20 <= 0 or close <= 0:
            return 0.0
        dev = (close - ma20) / ma20
        if -0.15 <= dev <= -0.03:
            return 0.35
        if -0.03 <= dev < 0.00:
            return 0.15
        return 0.0

    @staticmethod
    def _score_short_term_oversold(stock: dict) -> float:
        """Score short-term oversold via 5-day return (0→0.25).

        - -10 % to -2 %: 0.25
        """
        r5 = stock.get('return_5d', 0) or 0.0
        if -0.10 <= r5 <= -0.02:
            return 0.25
        return 0.0

    @staticmethod
    def _score_medium_term_decline(stock: dict) -> float:
        """Score medium-term decline via 20-day return (0→0.25).

        - -30 % to -5 %: 0.25
        """
        r20 = stock.get('return_20d', 0) or 0.0
        if -0.30 <= r20 <= -0.05:
            return 0.25
        return 0.0

    @staticmethod
    def _score_price_percentile(stock: dict) -> float:
        """Score 1-year price percentile (0→0.15).

        - Below 20th percentile: 0.15
        - Below 35th percentile: 0.08
        """
        pct = stock.get('price_percentile_1y', 0.5) or 0.5
        if pct < 0.20:
            return 0.15
        if pct < 0.35:
            return 0.08
        return 0.0

    # ------------------------------------------------------------------
    # Composite scoring
    # ------------------------------------------------------------------

    def _composite_score(self, stock: dict) -> float:
        """Return the total oversold score (0-1) for a single stock."""
        # Skip strong downtrend candidates entirely
        if self._is_strong_downtrend(stock):
            return 0.0

        return (
            self._score_ma_deviation(stock) +
            self._score_short_term_oversold(stock) +
            self._score_medium_term_decline(stock) +
            self._score_price_percentile(stock)
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
                score = next((s for c, s in scored if c == code), 0)
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': f'均值回归（超卖得分{score:.2f}）',
                })

        # --- SELL signals ---
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

            pos = portfolio['positions'].get(code, {})
            profit_rate = pos.get('profit_rate', 0) or 0.0

            close = stock.get('close', 0) or 0
            ma20 = stock.get('ma20', close) or close

            # Sell if profit > 5 %
            if profit_rate > 0.05:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': f'获利了结（盈利{profit_rate:.1%}）',
                })
            # Sell if price has recovered above MA20 with >2 % profit
            elif close > ma20 > 0 and profit_rate > 0.02:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': '价格回归MA20上方，获利了结',
                })

        return signals
