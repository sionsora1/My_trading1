"""
Integration tests for the SignalBus pipeline.

Covers:
  TC-SB01 — collect signals from multiple strategies
  TC-SB02 — SELL signals ordered before BUY signals
  TC-SB03 — deduplicate same-code same-direction signals (keep highest weight)
  TC-SB04 — limit-up stocks (pct_chg >= 9.9) are blocked from BUY

Uses the ``cached_market_data`` session fixture for real AKShare data.
Tests skip gracefully when no market data is available.
"""

import copy
import pytest

from sigbus.bus import SignalBus
from strategy import get_strategy


@pytest.mark.slow
@pytest.mark.real_data
class TestSignalBusPipeline:
    """Integration tests for the full SignalBus processing pipeline."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _latest_date(market_data: dict) -> str:
        """Return the most recent date key from the market-data dict."""
        return sorted(market_data.keys())[-1]

    @staticmethod
    def _make_strategies(names: list):
        """Instantiate strategies by name, skipping any that fail."""
        strategies = []
        for name in names:
            try:
                strategies.append(get_strategy(name))
            except Exception:
                pass
        return strategies

    # ------------------------------------------------------------------
    # TC-SB01
    # ------------------------------------------------------------------

    def test_collect_signals_from_multiple_strategies(self, cached_market_data):
        """Collect signals from multiple strategies and verify a list is returned.

        Uses trend_following, mean_reversion, and eight_factor strategies with
        real cached market data.  Each returned signal must have the mandatory
        ``ts_code`` and ``signal`` fields.
        """
        if not cached_market_data:
            pytest.skip("No market data")

        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]
        if not market_data:
            pytest.skip(f"No market data for {date}")

        strategies = self._make_strategies(
            ["trend_following", "mean_reversion", "eight_factor"]
        )
        if not strategies:
            pytest.skip("No strategies available")

        portfolio = {"cash": 100000, "positions": {}}

        bus = SignalBus()
        signals = bus.process(date, market_data, portfolio, strategies)

        assert isinstance(signals, list), (
            f"Expected list, got {type(signals).__name__}"
        )
        for sig in signals:
            assert "ts_code" in sig, (
                f"Signal missing 'ts_code': {list(sig.keys())}"
            )
            assert "signal" in sig, (
                f"Signal missing 'signal': {list(sig.keys())}"
            )
            assert sig["signal"] in ("BUY", "SELL"), (
                f"Unexpected signal value: {sig['signal']}"
            )

    # ------------------------------------------------------------------
    # TC-SB02
    # ------------------------------------------------------------------

    def test_sell_signals_before_buy_signals(self, cached_market_data):
        """SELL signals must appear before BUY signals in the output list.

        A portfolio is constructed with positions that have a high enough
        profit-rate to trigger SELL decisions in the mean-reversion strategy.
        A small helper strategy guarantees at least one BUY signal so the
        ordering can be verified.
        """
        if not cached_market_data:
            pytest.skip("No market data")

        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]
        if len(market_data) < 3:
            pytest.skip("Need at least 3 stocks in market data")

        strategies = self._make_strategies(["trend_following", "mean_reversion"])
        if not strategies:
            pytest.skip("No strategies available")

        codes = list(market_data.keys())

        # --- Build positions that trigger SELL in mean_reversion ---
        # mean_reversion sells when profit_rate > 5 %.
        positions = {}
        for code in codes[:2]:
            stock = market_data[code]
            close = stock.get("close", 10) or 10
            cost_price = close * 0.90  # ~11 % profit → triggers SELL
            positions[code] = {
                "ts_code": code,
                "quantity": 100,
                "cost_price": cost_price,
                "current_price": close,
                "profit_rate": (close - cost_price) / cost_price,
                "highest_price": close,
            }

        # --- A lightweight mock strategy that always emits one BUY ---
        # so we are certain both SELL and BUY exist in the output.
        buy_code = codes[2]  # a stock NOT in the positions above

        class _BuyProbe:
            name = "_BuyProbe"
            def generate_signals(self, _date, _mkt, _folio):
                return [{
                    "ts_code": buy_code,
                    "signal": "BUY",
                    "weight": 0.10,
                    "reason": "Probe buy for ordering test",
                }]

        strategies.append(_BuyProbe())

        portfolio = {
            "cash": 100000,
            "total_assets": 200000,
            "positions": positions,
        }

        bus = SignalBus()
        signals = bus.process(date, market_data, portfolio, strategies)

        sell_indices = [
            i for i, s in enumerate(signals) if s["signal"] == "SELL"
        ]
        buy_indices = [
            i for i, s in enumerate(signals) if s["signal"] == "BUY"
        ]

        if sell_indices and buy_indices:
            assert max(sell_indices) < min(buy_indices), (
                f"SELL should precede BUY, but got SELL at {sell_indices} "
                f"and BUY at {buy_indices}"
            )

    # ------------------------------------------------------------------
    # TC-SB03
    # ------------------------------------------------------------------

    def test_deduplicate_same_code_same_direction(self, cached_market_data):
        """When two strategies both produce BUY for the same stock,
        only the signal with the highest weight should survive.
        """
        if not cached_market_data:
            pytest.skip("No market data")

        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]
        if not market_data:
            pytest.skip(f"No market data for {date}")

        test_code = list(market_data.keys())[0]

        class _HighWeight:
            name = "_HighWeight"
            def generate_signals(self, _date, _mkt, _folio):
                return [{
                    "ts_code": test_code,
                    "signal": "BUY",
                    "weight": 0.15,
                    "reason": "High-weight signal",
                }]

        class _LowWeight:
            name = "_LowWeight"
            def generate_signals(self, _date, _mkt, _folio):
                return [{
                    "ts_code": test_code,
                    "signal": "BUY",
                    "weight": 0.05,
                    "reason": "Low-weight signal",
                }]

        portfolio = {"cash": 100000, "total_assets": 100000, "positions": {}}
        bus = SignalBus()
        signals = bus.process(
            date, market_data, portfolio,
            [_HighWeight(), _LowWeight()],
        )

        buy_signals = [
            s for s in signals
            if s["ts_code"] == test_code and s["signal"] == "BUY"
        ]
        assert len(buy_signals) == 1, (
            f"Expected 1 deduplicated BUY for {test_code}, "
            f"got {len(buy_signals)}"
        )
        assert buy_signals[0]["weight"] == 0.15, (
            f"Expected highest weight 0.15, got {buy_signals[0]['weight']}"
        )

    # ------------------------------------------------------------------
    # TC-SB04
    # ------------------------------------------------------------------

    def test_limit_up_stocks_blocked(self, cached_market_data):
        """Stocks with pct_chg >= 9.9 must not receive BUY signals.

        A copy of real market data is modified so one stock appears to be
        at the limit-up level.  A mock strategy tries to BUY it, and the
        signal bus should filter it out.
        """
        if not cached_market_data:
            pytest.skip("No market data")

        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]
        if not market_data:
            pytest.skip(f"No market data for {date}")

        test_code = list(market_data.keys())[0]

        # Deep-copy so we don't mutate the session-scoped fixture
        test_market = copy.deepcopy(market_data)
        test_market[test_code]["pct_chg"] = 9.95

        class _LimitUpBuyer:
            name = "_LimitUpBuyer"
            def generate_signals(self, _date, _mkt, _folio):
                return [{
                    "ts_code": test_code,
                    "signal": "BUY",
                    "weight": 0.50,
                    "reason": "Test buy at limit-up",
                }]

        portfolio = {"cash": 100000, "positions": {}}
        bus = SignalBus()
        signals = bus.process(
            date, test_market, portfolio,
            [_LimitUpBuyer()],
        )

        buy_signals = [
            s for s in signals
            if s["ts_code"] == test_code and s["signal"] == "BUY"
        ]
        assert len(buy_signals) == 0, (
            f"Limit-up stock {test_code} should be blocked, "
            f"but {len(buy_signals)} BUY signal(s) were emitted"
        )
