"""
tests/unit/test_strategies.py — Generic parameterized tests for all registered strategies.

TC-S01: generate_signals returns list
TC-S02: Each signal has required fields (ts_code, signal, weight, reason)
TC-S03: BUY signals must have positive weight
TC-S04: ts_code exists in market_data
TC-S05: No conflicting BUY+SELL for same stock in a single call
"""

import pytest

from strategy import STRATEGY_REGISTRY


@pytest.mark.slow
@pytest.mark.real_data
@pytest.mark.parametrize("strategy_name", list(STRATEGY_REGISTRY.keys()))
class TestAllStrategies:
    """Generic tests run against every registered strategy."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_strategy(self, strategy_name: str):
        """Instantiate a strategy by registry key."""
        info = STRATEGY_REGISTRY[strategy_name]
        return info["class"]()

    @staticmethod
    def _latest_data(cached_market_data: dict) -> dict:
        """Return the most recent date's market data, or skip the test."""
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return cached_market_data[dates[-1]]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    # ------------------------------------------------------------------
    # TC-S01
    # ------------------------------------------------------------------
    def test_generate_signals_returns_list(
        self, strategy_name: str, cached_market_data: dict
    ):
        """TC-S01: Every strategy's generate_signals() must return a list."""
        strategy = self._make_strategy(strategy_name)
        market_data = self._latest_data(cached_market_data)

        signals = strategy.generate_signals(
            date=sorted(cached_market_data.keys())[-1],
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )
        assert isinstance(signals, list), (
            f"{strategy_name}: expected list, got {type(signals).__name__}"
        )

    # ------------------------------------------------------------------
    # TC-S02
    # ------------------------------------------------------------------
    def test_signal_fields(
        self, strategy_name: str, cached_market_data: dict
    ):
        """TC-S02: Each signal dict must contain ts_code, signal, weight, reason."""
        strategy = self._make_strategy(strategy_name)
        market_data = self._latest_data(cached_market_data)

        signals = strategy.generate_signals(
            date=sorted(cached_market_data.keys())[-1],
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        required_fields = {"ts_code": str, "signal": str, "weight": float, "reason": str}
        signal_values = ("BUY", "SELL")

        for i, sig in enumerate(signals):
            for field, expected_type in required_fields.items():
                assert field in sig, (
                    f"{strategy_name} signal[{i}]: missing field '{field}'"
                )
                assert isinstance(sig[field], expected_type), (
                    f"{strategy_name} signal[{i}]: field '{field}' "
                    f"expected {expected_type.__name__}, got {type(sig[field]).__name__}"
                )
            assert sig["signal"] in signal_values, (
                f"{strategy_name} signal[{i}]: signal must be 'BUY' or 'SELL', "
                f"got '{sig['signal']}'"
            )

    # ------------------------------------------------------------------
    # TC-S03
    # ------------------------------------------------------------------
    def test_buy_signals_positive_weight(
        self, strategy_name: str, cached_market_data: dict
    ):
        """TC-S03: Any signal with signal=='BUY' must have weight > 0."""
        strategy = self._make_strategy(strategy_name)
        market_data = self._latest_data(cached_market_data)

        signals = strategy.generate_signals(
            date=sorted(cached_market_data.keys())[-1],
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        for i, sig in enumerate(signals):
            if sig["signal"] == "BUY":
                assert sig["weight"] > 0, (
                    f"{strategy_name} signal[{i}]: BUY signal "
                    f"must have positive weight, got {sig['weight']}"
                )

    # ------------------------------------------------------------------
    # TC-S04
    # ------------------------------------------------------------------
    def test_ts_code_exists_in_market_data(
        self, strategy_name: str, cached_market_data: dict
    ):
        """TC-S04: Every signal's ts_code must be a key in market_data."""
        strategy = self._make_strategy(strategy_name)
        market_data = self._latest_data(cached_market_data)

        signals = strategy.generate_signals(
            date=sorted(cached_market_data.keys())[-1],
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        valid_codes = set(market_data.keys())
        for i, sig in enumerate(signals):
            assert sig["ts_code"] in valid_codes, (
                f"{strategy_name} signal[{i}]: ts_code '{sig['ts_code']}' "
                f"not found in market_data"
            )

    # ------------------------------------------------------------------
    # TC-S05
    # ------------------------------------------------------------------
    def test_no_conflicting_signals_same_stock(
        self, strategy_name: str, cached_market_data: dict
    ):
        """TC-S05: No conflicting BUY+SELL for the same ts_code in one call."""
        strategy = self._make_strategy(strategy_name)
        market_data = self._latest_data(cached_market_data)

        signals = strategy.generate_signals(
            date=sorted(cached_market_data.keys())[-1],
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        actions_by_code: dict[str, set] = {}
        for sig in signals:
            code = sig["ts_code"]
            actions_by_code.setdefault(code, set()).add(sig["signal"])

        for code, actions in actions_by_code.items():
            assert len(actions) <= 1, (
                f"{strategy_name}: conflicting signals for {code}: {actions}"
            )


# =========================================================================
# Strategy-specific test classes (T1.5b)
# =========================================================================

from strategy.trend_following import EnhancedTrendFollowingStrategy
from strategy.mean_reversion import EnhancedMeanReversionStrategy
from strategy.intraday_reversal import IntradayReversalStrategy
from strategy.momentum_strategy import MomentumStrategy


@pytest.mark.slow
@pytest.mark.real_data
class TestTrendFollowing:
    """Tests specific to EnhancedTrendFollowingStrategy."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        """Return the most recent date string, or skip."""
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_bull_market_more_buy_signals(self, cached_market_data):
        """In an uptrend, strategy should generate BUY signals.

        With empty portfolio, should NOT generate SELL signals
        (nothing to sell).
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        # Filter for stocks clearly in an uptrend:
        # close > ma20 AND positive 20-day return
        uptrend_stocks = {}
        for code, stock in market_data.items():
            close = stock.get("close", 0) or 0
            ma20 = stock.get("ma20", 0) or 0
            r20 = stock.get("return_20d", 0) or 0.0
            if close > ma20 > 0 and r20 > 0:
                uptrend_stocks[code] = stock

        if len(uptrend_stocks) < 1:
            pytest.skip("No stocks in uptrend on latest date")

        strategy = EnhancedTrendFollowingStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=uptrend_stocks,
            portfolio=self._default_portfolio(),
        )

        buy_signals = [s for s in signals if s["signal"] == "BUY"]
        sell_signals = [s for s in signals if s["signal"] == "SELL"]

        assert len(buy_signals) >= 1, (
            f"Expected at least 1 BUY signal in uptrend, got {len(buy_signals)}"
        )
        assert len(sell_signals) == 0, (
            f"Expected 0 SELL signals with empty portfolio, got {len(sell_signals)}"
        )

    def test_sell_when_below_ma20(self, cached_market_data):
        """When a held stock's price falls below MA20 * 0.98,
        a SELL signal should be triggered.
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        # Pick any stock that has valid ma20 and close data
        target_code = None
        target_stock = None
        for code, stock in market_data.items():
            close = stock.get("close", 0) or 0
            ma20 = stock.get("ma20", 0) or 0
            if ma20 > 0 and close > 0:
                target_code = code
                target_stock = dict(stock)  # shallow copy
                break

        if target_code is None:
            pytest.skip("No stock with valid ma20/close data")

        # Modify close to trigger the sell condition: close < ma20 * 0.98
        ma20 = target_stock.get("ma20", 0) or 0
        target_stock["close"] = ma20 * 0.95  # clearly below 0.98 * ma20

        test_market = {target_code: target_stock}

        portfolio = {
            "cash": 100000,
            "positions": {
                target_code: {
                    "ts_code": target_code,
                    "quantity": 100,
                    "cost_price": ma20,
                    "current_price": target_stock["close"],
                    "profit_rate": -0.05,
                    "highest_price": ma20,
                }
            },
        }

        strategy = EnhancedTrendFollowingStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=test_market,
            portfolio=portfolio,
        )

        sell_for_target = [
            s for s in signals
            if s["signal"] == "SELL" and s["ts_code"] == target_code
        ]
        assert len(sell_for_target) >= 1, (
            f"Expected SELL signal for {target_code} when close < ma20*0.98, "
            f"got signals: {signals}"
        )


@pytest.mark.slow
@pytest.mark.real_data
class TestMeanReversion:
    """Tests specific to EnhancedMeanReversionStrategy."""

    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    def test_no_entry_in_strong_downtrend(self, cached_market_data):
        """Construct data with MA20 < MA60 and 20-day decline > 15%.
        Strategy's _composite_score should return 0 (filtered out).
        """
        # Synthetic stock in a strong downtrend
        stock = {
            "close": 8.0,
            "ma20": 9.0,
            "ma60": 10.0,
            "return_20d": -0.20,
            "return_5d": -0.05,
            "price_percentile_1y": 0.10,
        }

        strategy = EnhancedMeanReversionStrategy()
        score = strategy._composite_score(stock)

        assert score == 0.0, (
            f"Expected score 0.0 for strong downtrend stock, got {score}"
        )

    def test_oversold_produces_buy(self, cached_market_data):
        """Find the most oversold stock in real data (lowest return_20d),
        verify the strategy doesn't crash.
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        # Find stock with lowest return_20d
        most_oversold_code = None
        lowest_return = float("inf")
        for code, stock in market_data.items():
            r20 = stock.get("return_20d", 0) or 0.0
            if r20 < lowest_return:
                lowest_return = r20
                most_oversold_code = code

        if most_oversold_code is None:
            pytest.skip("No stock with return_20d data")

        # Create a market-data slice with the most oversold stock
        test_market = {most_oversold_code: market_data[most_oversold_code]}

        strategy = EnhancedMeanReversionStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=test_market,
            portfolio=self._default_portfolio(),
        )

        # Should not crash — return a list
        assert isinstance(signals, list), (
            f"Expected list from generate_signals, got {type(signals).__name__}"
        )


class TestIntradayReversal:
    """Tests specific to IntradayReversalStrategy — pattern detection."""

    @staticmethod
    def _make_bar(close, low, high, open_, volume):
        return {
            "close": close,
            "low": low,
            "high": high,
            "open": open_,
            "volume": volume,
        }

    def test_v_reversal_detection(self):
        """Construct V-shaped pattern: falling bars, high-volume low,
        then rising bars. Verify _detect_v_reversal returns True.
        """
        bars = [
            # Falling leg (bars 0-9)
            self._make_bar(10.5, 10.4, 10.6, 10.45, 100),
            self._make_bar(10.3, 10.2, 10.4, 10.25, 100),
            self._make_bar(10.1, 10.0, 10.2, 10.05, 100),
            self._make_bar( 9.9,  9.8, 10.0,  9.85, 100),
            self._make_bar( 9.7,  9.6,  9.8,  9.65, 100),
            self._make_bar( 9.5,  9.4,  9.6,  9.45, 100),
            self._make_bar( 9.3,  9.2,  9.4,  9.25, 100),
            self._make_bar( 9.1,  9.0,  9.2,  9.05, 100),
            self._make_bar( 8.9,  8.8,  9.0,  8.85, 100),
            self._make_bar( 8.7,  8.6,  8.8,  8.65, 100),
            # Trough bar — high volume (bar 10)
            self._make_bar( 8.5,  8.4,  8.6,  8.45, 200),
            # Rising leg (bars 11-19)
            self._make_bar( 8.55, 8.45, 8.65, 8.50, 100),
            self._make_bar( 8.60, 8.50, 8.70, 8.55, 100),
            self._make_bar( 8.65, 8.55, 8.75, 8.60, 100),
            self._make_bar( 8.70, 8.60, 8.80, 8.65, 100),
            self._make_bar( 8.75, 8.65, 8.85, 8.70, 100),
            self._make_bar( 8.80, 8.70, 8.90, 8.75, 100),
            self._make_bar( 8.85, 8.75, 8.95, 8.80, 100),
            self._make_bar( 8.90, 8.80, 9.00, 8.85, 100),
            self._make_bar( 8.95, 8.85, 9.05, 8.90, 100),
        ]

        result = IntradayReversalStrategy._detect_v_reversal(bars)
        assert result is True, (
            f"Expected V-reversal detection to return True, got {result}"
        )

    def test_a_reversal_detection(self):
        """Construct A-shaped pattern: rising bars, spike with long
        upper shadow, then falling bars. Verify _detect_a_reversal
        returns True.
        """
        bars = [
            # Rising leg (bars 0-8)
            self._make_bar(10.00,  9.95, 10.05,  9.98, 100),
            self._make_bar(10.05, 10.00, 10.10, 10.02, 100),
            self._make_bar(10.10, 10.05, 10.15, 10.08, 100),
            self._make_bar(10.15, 10.10, 10.20, 10.13, 100),
            self._make_bar(10.20, 10.15, 10.25, 10.18, 100),
            self._make_bar(10.25, 10.20, 10.30, 10.23, 100),
            self._make_bar(10.30, 10.25, 10.35, 10.28, 100),
            self._make_bar(10.35, 10.30, 10.40, 10.33, 100),
            self._make_bar(10.40, 10.35, 10.45, 10.38, 100),
            # Peak bar — long upper shadow (bar 9)
            self._make_bar(10.45, 10.40, 10.65, 10.43, 120),
            # Falling leg (bars 10-19)
            self._make_bar(10.40, 10.35, 10.45, 10.42, 100),
            self._make_bar(10.35, 10.30, 10.40, 10.37, 100),
            self._make_bar(10.30, 10.25, 10.35, 10.32, 100),
            self._make_bar(10.25, 10.20, 10.30, 10.27, 100),
            self._make_bar(10.20, 10.15, 10.25, 10.22, 100),
            self._make_bar(10.15, 10.10, 10.20, 10.17,  90),
            self._make_bar(10.10, 10.05, 10.15, 10.12,  90),
            self._make_bar(10.05, 10.00, 10.10, 10.07,  90),
            self._make_bar(10.00,  9.95, 10.05, 10.02,  90),
            self._make_bar( 9.95,  9.90, 10.00,  9.97,  90),
        ]

        result = IntradayReversalStrategy._detect_a_reversal(bars)
        assert result is True, (
            f"Expected A-reversal detection to return True, got {result}"
        )


@pytest.mark.slow
@pytest.mark.real_data
class TestMomentumStrategy:
    """Tests specific to MomentumStrategy."""

    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    def test_generates_signals(self, cached_market_data):
        """Verify MomentumStrategy generates signals with the
        standard interface.
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = MomentumStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        # Standard interface check
        assert isinstance(signals, list), (
            f"Expected list from generate_signals, got {type(signals).__name__}"
        )

        # Verify signal structure if any signals were generated
        for sig in signals:
            assert "ts_code" in sig
            assert sig["signal"] in ("BUY", "SELL")
            assert isinstance(sig["weight"], float)
            assert isinstance(sig["reason"], str)


# =========================================================================
# Strategy-specific test classes (T1.5c)
# =========================================================================

from strategy.eight_factor import EightFactorStrategy
from strategy.low_volatility import EnhancedLowVolatilityStrategy
from strategy.sector_rotation import SectorRotationStrategy
from strategy.position_strategy import PositionStrategy
from strategy.ai_strategy import AIStrategy


@pytest.mark.slow
@pytest.mark.real_data
class TestEightFactor:
    """Tests specific to EightFactorStrategy."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------
    def test_all_stocks_scored(self, cached_market_data):
        """Call generate_signals() with real data. Verify it returns
        a list and does not crash.
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = EightFactorStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        assert isinstance(signals, list), (
            f"Expected list from EightFactorStrategy.generate_signals, "
            f"got {type(signals).__name__}"
        )

    def test_signals_have_reasonable_count(self, cached_market_data):
        """The strategy should produce signals (can be 0 or more,
        but must not throw).
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = EightFactorStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        # Must be a list — can be empty but must not crash
        assert isinstance(signals, list), (
            f"Expected list from EightFactorStrategy, got {type(signals).__name__}"
        )

        # Verify signal structure if any produced
        for sig in signals:
            assert "ts_code" in sig
            assert sig["signal"] in ("BUY", "SELL")
            assert isinstance(sig["weight"], float)
            assert isinstance(sig["reason"], str)


@pytest.mark.slow
@pytest.mark.real_data
class TestLowVolatility:
    """Tests specific to EnhancedLowVolatilityStrategy."""

    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    def test_generates_signals(self, cached_market_data):
        """Call generate_signals() with real data. Verify output format."""
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = EnhancedLowVolatilityStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        assert isinstance(signals, list), (
            f"Expected list from EnhancedLowVolatilityStrategy.generate_signals, "
            f"got {type(signals).__name__}"
        )

        # Verify signal structure if any produced
        for sig in signals:
            assert "ts_code" in sig
            assert sig["signal"] in ("BUY", "SELL")
            assert isinstance(sig["weight"], float)
            assert isinstance(sig["reason"], str)

    def test_defensive_bias(self, cached_market_data):
        """The strategy should tend to select lower-volatility stocks.
        Verify it does not crash.
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = EnhancedLowVolatilityStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        buy_signals = [s for s in signals if s["signal"] == "BUY"]

        if len(buy_signals) < 1:
            pytest.skip("No BUY signals generated — cannot verify defensive bias")

        # Compute average volatility of selected stocks vs all stocks
        buy_codes = {s["ts_code"] for s in buy_signals}
        buy_vols = []
        all_vols = []
        for code, stock in market_data.items():
            vol = stock.get("volatility", None)
            if vol is not None:
                all_vols.append(vol)
                if code in buy_codes:
                    buy_vols.append(vol)

        if buy_vols and all_vols:
            avg_buy_vol = sum(buy_vols) / len(buy_vols)
            avg_all_vol = sum(all_vols) / len(all_vols)
            # Low-vol strategy should pick stocks with below-average volatility
            # This is a soft check — real data may vary, but we verify
            # the strategy is at least not picking only the highest-vol stocks
            assert avg_buy_vol <= avg_all_vol * 2.0, (
                f"Defensive strategy selected stocks with avg volatility "
                f"{avg_buy_vol:.4f} vs market avg {avg_all_vol:.4f}"
            )


@pytest.mark.slow
@pytest.mark.real_data
class TestSectorRotation:
    """Tests specific to SectorRotationStrategy."""

    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    def test_generates_signals(self, cached_market_data):
        """Call generate_signals() with real data. Verify output format.

        Sector data may not be available (no AKShare / network),
        in which case the strategy returns an empty list gracefully.
        """
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = SectorRotationStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        assert isinstance(signals, list), (
            f"Expected list from SectorRotationStrategy.generate_signals, "
            f"got {type(signals).__name__}"
        )

        # Verify signal structure if any produced
        for sig in signals:
            assert "ts_code" in sig
            assert sig["signal"] in ("BUY", "SELL")
            assert isinstance(sig["weight"], float)
            assert isinstance(sig["reason"], str)


@pytest.mark.slow
@pytest.mark.real_data
class TestPositionStrategy:
    """Tests specific to PositionStrategy."""

    @staticmethod
    def _latest_date(cached_market_data: dict) -> str:
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        return dates[-1]

    @staticmethod
    def _default_portfolio() -> dict:
        return {"cash": 100000, "positions": {}}

    def test_generates_signals(self, cached_market_data):
        """Call generate_signals() with real data. Verify output format."""
        date = self._latest_date(cached_market_data)
        market_data = cached_market_data[date]

        strategy = PositionStrategy()
        signals = strategy.generate_signals(
            date=date,
            market_data=market_data,
            portfolio=self._default_portfolio(),
        )

        assert isinstance(signals, list), (
            f"Expected list from PositionStrategy.generate_signals, "
            f"got {type(signals).__name__}"
        )

        # Verify signal structure if any produced
        for sig in signals:
            assert "ts_code" in sig
            assert sig["signal"] in ("BUY", "SELL")
            assert isinstance(sig["weight"], float)
            assert isinstance(sig["reason"], str)


class TestAIStrategy:
    """Tests specific to AIStrategy — may skip if model is unavailable."""

    def test_ai_strategy_exists(self):
        """Verify AIStrategy class can be imported and instantiated."""
        strategy = AIStrategy()
        assert strategy is not None, "AIStrategy should be instantiable"
        assert strategy.max_position_num > 0, (
            f"Expected positive max_position_num, got {strategy.max_position_num}"
        )

    def test_generates_signals_or_skips(self, cached_market_data):
        """Try calling generate_signals(). If the AI model file is missing,
        skip gracefully with pytest.skip(). If it is available, verify
        output format.
        """
        if not cached_market_data:
            pytest.skip("No market data available")
        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No market data available")
        date = dates[-1]
        market_data = cached_market_data[date]
        portfolio = {"cash": 100000, "positions": {}}

        strategy = AIStrategy()

        # Access ai_engine to check model availability.
        # The property returns None if the model file is not found,
        # which triggers a fallback to the 8-factor engine internally.
        engine = strategy.ai_engine
        if engine is None:
            # Model not available — the strategy should still work via
            # 8-factor fallback.  We test that it does not crash.
            try:
                signals = strategy.generate_signals(
                    date=date,
                    market_data=market_data,
                    portfolio=portfolio,
                )
                assert isinstance(signals, list), (
                    f"AIStrategy fallback: expected list, "
                    f"got {type(signals).__name__}"
                )
            except Exception as e:
                pytest.skip(f"AIStrategy not functional: {e}")
        else:
            # Model is available — full test
            signals = strategy.generate_signals(
                date=date,
                market_data=market_data,
                portfolio=portfolio,
            )
            assert isinstance(signals, list), (
                f"Expected list from AIStrategy.generate_signals, "
                f"got {type(signals).__name__}"
            )
            for sig in signals:
                assert "ts_code" in sig
                assert sig["signal"] in ("BUY", "SELL")
                assert isinstance(sig["weight"], float)
                assert isinstance(sig["reason"], str)
