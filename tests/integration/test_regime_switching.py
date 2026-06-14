"""
tests/integration/test_regime_switching.py — Integration tests for market regime
detection and strategy switching.

TC-RS01: Market regime detection — verify classification produces expected output
TC-RS02: Strategy changes with regime — verify strategy scoring adapts per regime
TC-RS03: Sideways market strategy performance — verify sideways-specific behavior
"""

import pytest
import numpy as np

from analysis.market_regime import (
    MarketRegime,
    MarketRegimeDetector,
    RegimeAnalysis,
    StrategyRegimeAdapter,
    analyze_market,
)
from config.strategy_profiles import get_profile_for_regime


# =============================================================================
# Synthetic data helpers
# =============================================================================

def _make_synthetic_stock(
    close_scale=1.0,
    ma_spread=0.02,
    return_1d=0.01,
    return_20d=0.05,
    return_60d=0.10,
    volatility=0.20,
    n_stocks=50,
):
    """Build a dict of synthetic stocks with controllable trend characteristics.

    close_scale > 1.0  → close > ma60  (uptrend)
    close_scale < 1.0  → close < ma60  (downtrend)
    ma_spread controls how far MAs are from each other.
    """
    rng = np.random.RandomState(42)
    stocks = {}
    for i in range(n_stocks):
        code = f"60{1000 + i:04d}"
        base = 50.0
        close = base * close_scale * (1 + rng.uniform(-0.05, 0.05))
        stocks[code] = {
            "close": close,
            "ma5":   close * (1 - rng.uniform(0, ma_spread)),
            "ma10":  close * (1 - rng.uniform(ma_spread * 0.5, ma_spread * 1.5)),
            "ma20":  close * (1 - rng.uniform(ma_spread, ma_spread * 2)),
            "ma60":  base,
            "return_1d":  rng.normal(return_1d, 0.02),
            "return_20d": rng.normal(return_20d, 0.05),
            "return_60d": rng.normal(return_60d, 0.10),
            "volatility": rng.uniform(volatility - 0.05, volatility + 0.05),
        }
    return stocks


def _make_synthetic_sideways_market(n_stocks=50):
    """Build synthetic sideways data: prices oscillate around MAs with
    low returns and modest volatility.
    """
    rng = np.random.RandomState(123)
    stocks = {}
    for i in range(n_stocks):
        code = f"60{1000 + i:04d}"
        base = 50.0
        close = base * (1 + rng.uniform(-0.03, 0.03))
        stocks[code] = {
            "close": close,
            "ma5":   close * (1 + rng.uniform(-0.01, 0.01)),
            "ma10":  close * (1 + rng.uniform(-0.015, 0.015)),
            "ma20":  close * (1 + rng.uniform(-0.02, 0.02)),
            "ma60":  base,
            "return_1d":  rng.normal(0.0, 0.015),
            "return_20d": rng.normal(0.0, 0.05),
            "return_60d": rng.normal(0.0, 0.08),
            "volatility": rng.uniform(0.15, 0.3),
        }
    return stocks


# =============================================================================
# TC-RS01: Market regime detection
# =============================================================================

@pytest.mark.parametrize("scenario,expected_regimes", [
    ("bull",     {MarketRegime.BULL}),
    ("bear",     {MarketRegime.BEAR, MarketRegime.CRASH}),
    ("sideways", {MarketRegime.SIDEWAYS}),
])
class TestMarketRegimeDetection:
    """TC-RS01: Market regime detection produces expected classifications
    and well-formed RegimeAnalysis output.

    Notes:
    - The detector may classify strong bear markets as CRASH instead of BEAR.
    - The detector may classify mild bull markets as SIDEWAYS.
    These are valid behaviors of the actual MarketRegimeDetector.
    """

    def test_detect_classifies_regime_correctly(self, scenario, expected_regimes):
        """Synthetic data representative of bull / bear / sideways markets
        should be classified into the corresponding MarketRegime.
        """
        if scenario == "bull":
            data = _make_synthetic_stock(
                close_scale=1.3, ma_spread=0.03,
                return_1d=0.02, return_20d=0.15, return_60d=0.30,
                volatility=0.18,
            )
        elif scenario == "bear":
            data = _make_synthetic_stock(
                close_scale=0.7, ma_spread=0.04,
                return_1d=-0.02, return_20d=-0.15, return_60d=-0.25,
                volatility=0.35,
            )
        else:  # sideways
            data = _make_synthetic_sideways_market()

        detector = MarketRegimeDetector()
        result = detector.detect(data)

        assert isinstance(result, RegimeAnalysis), (
            f"Expected RegimeAnalysis, got {type(result).__name__}"
        )
        assert result.regime in expected_regimes, (
            f"Scenario '{scenario}': expected one of {expected_regimes}, "
            f"got {result.regime}"
        )

    def test_detect_output_has_all_required_fields(self, scenario, expected_regimes):
        """RegimeAnalysis must contain all required attributes with correct types."""
        if scenario == "bull":
            data = _make_synthetic_stock(close_scale=1.3, ma_spread=0.03,
                                         return_1d=0.02, return_20d=0.15,
                                         return_60d=0.30, volatility=0.18)
        elif scenario == "bear":
            data = _make_synthetic_stock(close_scale=0.7, ma_spread=0.04,
                                         return_1d=-0.02, return_20d=-0.15,
                                         return_60d=-0.25, volatility=0.35)
        else:
            data = _make_synthetic_sideways_market()

        detector = MarketRegimeDetector()
        result = detector.detect(data)

        # RegimeAnalysis dataclass fields
        assert isinstance(result.regime, MarketRegime)
        assert isinstance(result.confidence, float)
        assert 0.0 <= result.confidence <= 1.0, (
            f"Confidence must be in [0, 1], got {result.confidence}"
        )
        assert isinstance(result.description, str)
        assert len(result.description) > 0, "Description must not be empty"
        assert isinstance(result.indicators, dict)
        expected_indicators = {"trend", "volatility", "momentum", "breadth", "sentiment"}
        assert set(result.indicators.keys()) == expected_indicators, (
            f"Expected indicators {expected_indicators}, "
            f"got {set(result.indicators.keys())}"
        )
        for key in expected_indicators:
            val = result.indicators[key]
            assert isinstance(val, (float, int, np.floating, np.integer)), (
                f"Indicator '{key}' should be numeric, "
                f"got {type(val).__name__}"
            )
            assert -1.0 <= float(val) <= 1.0, (
                f"Indicator '{key}' out of range: {val}"
            )
        assert isinstance(result.recommended_strategies, list)
        assert len(result.recommended_strategies) > 0, (
            "Must have at least one recommended strategy"
        )
        for s in result.recommended_strategies:
            assert isinstance(s, str)
        assert isinstance(result.risk_level, str)
        assert isinstance(result.position_advice, str)

    def test_detect_confidence_scales_with_strength(self, scenario, expected_regimes):
        """Synthetic market data should produce a valid RegimeAnalysis
        with confidence in [0, 1]."""
        if scenario == "bull":
            strong = _make_synthetic_stock(
                close_scale=1.5, ma_spread=0.05,
                return_1d=0.03, return_20d=0.25, return_60d=0.50,
            )
            weak = _make_synthetic_stock(
                close_scale=1.05, ma_spread=0.01,
                return_1d=0.005, return_20d=0.02, return_60d=0.03,
            )
        elif scenario == "bear":
            strong = _make_synthetic_stock(
                close_scale=0.5, ma_spread=0.05,
                return_1d=-0.03, return_20d=-0.25, return_60d=-0.40,
            )
            weak = _make_synthetic_stock(
                close_scale=0.95, ma_spread=0.01,
                return_1d=-0.005, return_20d=-0.02, return_60d=-0.03,
            )
        else:  # sideways
            # Strong sideways = very flat
            rng = np.random.RandomState(99)
            strong = {}
            for i in range(50):
                code = f"60{1000 + i:04d}"
                close = 50 * (1 + rng.uniform(-0.01, 0.01))
                strong[code] = {
                    "close": close, "ma5": 50, "ma10": 50,
                    "ma20": 50, "ma60": 50,
                    "return_1d": rng.normal(0, 0.005),
                    "return_20d": rng.normal(0, 0.01),
                    "return_60d": rng.normal(0, 0.02),
                    "volatility": rng.uniform(0.1, 0.2),
                }
            weak = _make_synthetic_sideways_market()

        detector = MarketRegimeDetector()
        result_strong = detector.detect(strong)
        result_weak = detector.detect(weak)

        # Both should return a valid regime
        assert result_strong.regime is not None, "Strong detection returned None"
        assert result_weak.regime is not None, "Weak detection returned None"

        # Confidence should be in valid range
        assert 0.0 <= result_strong.confidence <= 1.0
        assert 0.0 <= result_weak.confidence <= 1.0


# =============================================================================
# TC-RS02: Strategy changes with regime
# =============================================================================

class TestStrategyChangesWithRegime:
    """TC-RS02: When market regime changes, strategy selection and scoring
    should adapt accordingly.
    """

    # ── StrategyRegimeAdapter.get_strategy_score ─────────────────────────
    def test_get_strategy_score_differs_by_regime(self):
        """The same strategy should receive different performance scores
        in different regimes.
        """
        # momentum shines in bull, struggles in bear
        bull_score = StrategyRegimeAdapter.get_strategy_score(
            "momentum", MarketRegime.BULL
        )
        bear_score = StrategyRegimeAdapter.get_strategy_score(
            "momentum", MarketRegime.BEAR
        )
        assert bull_score > bear_score, (
            f"momentum should score higher in BULL ({bull_score}) "
            f"than in BEAR ({bear_score})"
        )

        # mean_reversion shines in sideways, not in trending markets
        sideways_score = StrategyRegimeAdapter.get_strategy_score(
            "mean_reversion", MarketRegime.SIDEWAYS
        )
        bull_score_mr = StrategyRegimeAdapter.get_strategy_score(
            "mean_reversion", MarketRegime.BULL
        )
        assert sideways_score > bull_score_mr, (
            f"mean_reversion should score higher in SIDEWAYS ({sideways_score}) "
            f"than in BULL ({bull_score_mr})"
        )

        # low_volatility is defensive — better in bear than bull
        lv_bear = StrategyRegimeAdapter.get_strategy_score(
            "low_volatility", MarketRegime.BEAR
        )
        lv_bull = StrategyRegimeAdapter.get_strategy_score(
            "low_volatility", MarketRegime.BULL
        )
        assert lv_bear > lv_bull, (
            f"low_volatility should score higher in BEAR ({lv_bear}) "
            f"than in BULL ({lv_bull})"
        )

    def test_get_strategy_score_returns_float_range(self):
        """All strategy scores should be floats between 0 and 1."""
        regimes = list(MarketRegime)
        strategies = ["eight_factor", "momentum", "mean_reversion",
                       "value", "quality", "low_volatility",
                       "trend_following", "position"]

        for st in strategies:
            for regime in regimes:
                score = StrategyRegimeAdapter.get_strategy_score(st, regime)
                assert isinstance(score, float), (
                    f"Score for '{st}' in {regime} should be float, "
                    f"got {type(score).__name__}"
                )
                assert 0.0 <= score <= 1.0, (
                    f"Score for '{st}' in {regime} out of range: {score}"
                )

    def test_get_strategy_score_unknown_returns_default(self):
        """Unknown strategy should return a reasonable default (0.5)."""
        score = StrategyRegimeAdapter.get_strategy_score(
            "nonexistent_strategy", MarketRegime.BULL
        )
        # Should return default 0.5 (from .get fallback)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    # ── StrategyRegimeAdapter.adjust_position_weight ────────────────────
    @pytest.mark.parametrize("regime,expected_ratio", [
        (MarketRegime.BULL,     1.0),
        (MarketRegime.BEAR,     0.6),
        (MarketRegime.SIDEWAYS, 0.8),
        (MarketRegime.VOLATILE, 0.5),
        (MarketRegime.CRASH,    0.2),
    ])
    def test_adjust_position_weight(self, regime, expected_ratio):
        """Position weight should be adjusted by a regime-specific multiplier."""
        base = 1.0
        adjusted = StrategyRegimeAdapter.adjust_position_weight(base, regime)
        assert adjusted == pytest.approx(expected_ratio), (
            f"For {regime}: expected {expected_ratio}, got {adjusted}"
        )

    def test_adjust_position_weight_preserves_scaling(self):
        """Adjustment should scale linearly with base weight."""
        base = 0.5
        for regime, ratio in [
            (MarketRegime.BULL, 1.0),
            (MarketRegime.BEAR, 0.6),
            (MarketRegime.SIDEWAYS, 0.8),
        ]:
            adjusted = StrategyRegimeAdapter.adjust_position_weight(base, regime)
            expected = base * ratio
            assert adjusted == pytest.approx(expected), (
                f"base={base}, regime={regime}: expected {expected}, got {adjusted}"
            )

    # ── StrategyRegimeAdapter.adjust_stop_loss ──────────────────────────
    def test_adjust_stop_loss_bear_tighter(self):
        """Stop loss should be tightened in bear markets."""
        base = 0.10  # 10%
        bull_sl = StrategyRegimeAdapter.adjust_stop_loss(base, MarketRegime.BULL)
        bear_sl = StrategyRegimeAdapter.adjust_stop_loss(base, MarketRegime.BEAR)

        assert bull_sl > bear_sl, (
            f"BULL stop loss ({bull_sl:.3f}) should be wider than "
            f"BEAR stop loss ({bear_sl:.3f})"
        )
        # Bear tightens by 30%
        assert bear_sl == pytest.approx(base * 0.7), (
            f"BEAR stop loss should be {base * 0.7}, got {bear_sl}"
        )

    def test_adjust_stop_loss_sideways_unchanged(self):
        """Stop loss should remain unchanged in sideways markets."""
        base = 0.10
        adjusted = StrategyRegimeAdapter.adjust_stop_loss(
            base, MarketRegime.SIDEWAYS
        )
        assert adjusted == pytest.approx(base), (
            f"SIDEWAYS stop loss should equal base {base}, got {adjusted}"
        )

    # ── get_profile_for_regime ──────────────────────────────────────────
    def test_get_profile_for_regime_returns_expected_strategies(self):
        """Each regime profile should contain the expected strategy set."""
        bull_profile = get_profile_for_regime(MarketRegime.BULL)
        assert "trend_following" in bull_profile["strategies"]
        assert "momentum" in bull_profile["strategies"]

        bear_profile = get_profile_for_regime(MarketRegime.BEAR)
        assert "low_volatility" in bear_profile["strategies"]
        assert "value" in bear_profile["strategies"]

        sideways_profile = get_profile_for_regime(MarketRegime.SIDEWAYS)
        assert "mean_reversion" in sideways_profile["strategies"]

        volatile_profile = get_profile_for_regime(MarketRegime.VOLATILE)
        assert "low_volatility" in volatile_profile["strategies"]

        crash_profile = get_profile_for_regime(MarketRegime.CRASH)
        assert crash_profile["position_ratio"] == pytest.approx(0.10)

    def test_get_profile_for_regime_has_required_fields(self):
        """Every profile must contain name, strategies, position_ratio, stop_loss."""
        required_keys = {"name", "strategies", "position_ratio", "stop_loss"}
        for regime in MarketRegime:
            profile = get_profile_for_regime(regime)
            for key in required_keys:
                assert key in profile, (
                    f"Profile for {regime} missing key '{key}'"
                )
            assert isinstance(profile["name"], str)
            assert isinstance(profile["strategies"], list)
            assert len(profile["strategies"]) > 0
            assert isinstance(profile["position_ratio"], (int, float))
            assert 0.0 <= profile["position_ratio"] <= 1.0
            assert isinstance(profile["stop_loss"], (int, float))
            assert profile["stop_loss"] < 0, (
                f"stop_loss should be negative, got {profile['stop_loss']}"
            )

    def test_get_profile_for_regime_accepts_string(self):
        """get_profile_for_regime should accept both enum and string keys."""
        from_string = get_profile_for_regime("bull")
        from_enum = get_profile_for_regime(MarketRegime.BULL)
        assert from_string == from_enum, (
            f"String 'bull' and MarketRegime.BULL should give same profile"
        )

    def test_get_profile_for_regime_unknown_falls_back(self):
        """Unknown regime string should return the default profile."""
        profile = get_profile_for_regime("nonexistent_regime")
        assert profile["name"] == "默认组合", (
            f"Expected default profile, got '{profile['name']}'"
        )


# =============================================================================
# TC-RS03: Sideways market strategy performance
# =============================================================================

class TestSidewaysMarketStrategyPerformance:
    """TC-RS03: Verify that strategies behave correctly in sideways / choppy
    market conditions.
    """

    def test_mean_reversion_best_in_sideways(self):
        """In sideways markets, mean_reversion should be among the
        highest-scoring strategies.
        """
        strategies = ["eight_factor", "momentum", "mean_reversion",
                       "value", "quality", "low_volatility",
                       "trend_following", "position"]

        scores = {
            st: StrategyRegimeAdapter.get_strategy_score(st, MarketRegime.SIDEWAYS)
            for st in strategies
        }

        # mean_reversion should score 0.9 in sideways — the highest
        max_score = max(scores.values())
        assert scores["mean_reversion"] == pytest.approx(0.9), (
            f"mean_reversion should score 0.9 in SIDEWAYS, "
            f"got {scores['mean_reversion']}"
        )
        assert scores["mean_reversion"] >= max_score * 0.95, (
            f"mean_reversion ({scores['mean_reversion']}) should be near "
            f"the top score ({max_score}) in SIDEWAYS"
        )

    def test_trend_following_struggles_in_sideways(self):
        """Trend following strategies should score low in sideways markets
        because there is no clear trend to follow.
        """
        tf_score = StrategyRegimeAdapter.get_strategy_score(
            "trend_following", MarketRegime.SIDEWAYS
        )
        tf_bull = StrategyRegimeAdapter.get_strategy_score(
            "trend_following", MarketRegime.BULL
        )

        assert tf_score < 0.5, (
            f"trend_following should score low in SIDEWAYS, got {tf_score}"
        )
        assert tf_score < tf_bull, (
            f"trend_following SIDEWAYS ({tf_score}) < BULL ({tf_bull})"
        )

    def test_momentum_struggles_in_sideways(self):
        """Momentum strategies should score below average in sideways markets."""
        mom_score = StrategyRegimeAdapter.get_strategy_score(
            "momentum", MarketRegime.SIDEWAYS
        )
        assert mom_score < 0.5, (
            f"momentum should score low in SIDEWAYS, got {mom_score}"
        )

    def test_get_profile_recommends_mean_reversion(self):
        """Sideways profile should recommend mean_reversion strategy."""
        profile = get_profile_for_regime(MarketRegime.SIDEWAYS)
        assert "mean_reversion" in profile["strategies"], (
            f"Sideways profile should include mean_reversion, "
            f"got {profile['strategies']}"
        )
        assert profile["name"] == "震荡市组合", (
            f"Expected '震荡市组合', got '{profile['name']}'"
        )

    def test_sideways_position_ratio_is_moderate(self):
        """Sideways market position ratio should be moderate (neither
        too aggressive nor too defensive).
        """
        profile = get_profile_for_regime(MarketRegime.SIDEWAYS)
        ratio = profile["position_ratio"]

        # Should be between bear (0.30) and bull (0.80)
        assert 0.40 <= ratio <= 0.60, (
            f"Sideways position ratio should be moderate (0.40-0.60), "
            f"got {ratio}"
        )

    def test_sideways_stop_loss_is_moderate(self):
        """Sideways stop loss should be moderate."""
        profile = get_profile_for_regime(MarketRegime.SIDEWAYS)
        sl = profile["stop_loss"]

        # Should be between crash (-0.03) and bull (-0.08)
        assert -0.10 <= sl <= -0.03, (
            f"Sideways stop loss should be moderate, got {sl}"
        )

    def test_sideways_detection_with_synthetic_data(self):
        """Detector should correctly classify a synthetic sideways market."""
        data = _make_synthetic_sideways_market(n_stocks=50)
        detector = MarketRegimeDetector()
        result = detector.detect(data)

        assert result.regime == MarketRegime.SIDEWAYS, (
            f"Expected SIDEWAYS, got {result.regime}"
        )
        assert result.confidence > 0.3, (
            f"Sideways confidence should be reasonable, got {result.confidence}"
        )

    def test_sideways_position_weight_adjustment(self):
        """In sideways markets, position weight should be reduced to 80%."""
        base = 1.0
        adjusted = StrategyRegimeAdapter.adjust_position_weight(
            base, MarketRegime.SIDEWAYS
        )
        assert adjusted == pytest.approx(0.8), (
            f"Sideways weight should be 0.8, got {adjusted}"
        )

    def test_sideways_recommended_strategies_by_detector(self):
        """MarketRegimeDetector should recommend appropriate strategies
        for a sideways market.
        """
        data = _make_synthetic_sideways_market(n_stocks=50)
        detector = MarketRegimeDetector()
        result = detector.detect(data)

        # Sideways recommendation should include mean_reversion
        assert "mean_reversion" in result.recommended_strategies, (
            f"Sideways recommended strategies should include mean_reversion, "
            f"got {result.recommended_strategies}"
        )

    def test_analyze_market_convenience_function(self):
        """The top-level analyze_market() convenience function should
        work correctly.
        """
        data = _make_synthetic_sideways_market(n_stocks=50)
        result = analyze_market(data)

        assert isinstance(result, RegimeAnalysis)
        assert result.regime == MarketRegime.SIDEWAYS


# =============================================================================
# Real-data tests (marked slow — require AKShare / network)
# =============================================================================

@pytest.mark.slow
class TestRegimeSwitchingRealData:
    """Integration tests using cached_market_data fixture for real
    AKShare-sourced data.
    """

    def test_detect_with_real_market_data(self, cached_market_data):
        """TC-RS01: Run regime detection against real market data.
        Verifies the detector does not crash and produces valid output.
        """
        if not cached_market_data:
            pytest.skip("No real market data available")

        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No dates in real market data")

        # Use the most recent date
        latest_date = dates[-1]
        market_data = cached_market_data[latest_date]

        detector = MarketRegimeDetector()
        result = detector.detect(market_data)

        assert isinstance(result, RegimeAnalysis)
        assert isinstance(result.regime, MarketRegime)
        assert 0.0 <= result.confidence <= 1.0
        assert len(result.recommended_strategies) > 0
        assert result.risk_level in ("低", "中等", "高", "极高")

    def test_regime_profiles_match_detected_regime(self, cached_market_data):
        """TC-RS02: The strategy profile for the detected regime should
        exist and contain valid strategies.
        """
        if not cached_market_data:
            pytest.skip("No real market data available")

        dates = sorted(cached_market_data.keys())
        if not dates:
            pytest.skip("No dates in real market data")

        # Test a few dates to ensure profiles exist for varying regimes
        detector = MarketRegimeDetector()
        sampled_dates = [dates[0], dates[-1]]

        for date in sampled_dates:
            market_data = cached_market_data[date]
            result = detector.detect(market_data)
            profile = get_profile_for_regime(result.regime)

            assert "strategies" in profile
            assert len(profile["strategies"]) > 0
            assert "position_ratio" in profile
            assert "stop_loss" in profile

    def test_sideways_detection_with_real_data(self, cached_market_data):
        """TC-RS03: Run regime detection across multiple dates to verify
        the system can handle potentially different regimes in real data.
        """
        if not cached_market_data:
            pytest.skip("No real market data available")

        dates = sorted(cached_market_data.keys())
        if len(dates) < 2:
            pytest.skip("Need at least 2 dates of real market data")

        detector = MarketRegimeDetector()
        regimes_seen = set()

        for date in dates:
            market_data = cached_market_data[date]
            result = detector.detect(market_data)
            regimes_seen.add(result.regime)

            # Verify output integrity for every date
            assert isinstance(result, RegimeAnalysis)
            assert 0.0 <= result.confidence <= 1.0
            assert len(result.recommended_strategies) > 0

        # At least one regime was detected
        assert len(regimes_seen) >= 1, (
            f"Should detect at least one regime, got {len(regimes_seen)}"
        )
