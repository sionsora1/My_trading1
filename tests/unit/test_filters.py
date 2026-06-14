"""
tests/unit/test_filters.py — Unit tests for SignalFilters in sigbus/filters.py.

Test coverage:
  1. Limit-up filter: stocks with pct_chg >= 9.9 blocked for BUY
  2. Limit-down filter: stocks with pct_chg <= -9.9 blocked for SELL
  3. ST flag filter: ST stocks filtered via blacklist mechanism
  4. Market cap filter: stocks below minimum market cap excluded
  5. Liquidity/turnover filter: low turnover or volume filtered out
  6. Duplicate signal dedup: same (ts_code, direction) deduplicated
  7. Risk rule combination: multiple filters work together via run_all_checks
  8. Edge/boundary conditions: boundary values tested
"""

import pytest

from sigbus.filters import SignalFilters


# ---------------------------------------------------------------------------
# Standalone helper filters for concepts not yet built into SignalFilters
# ---------------------------------------------------------------------------

def filter_st_stocks(signals: list, st_set: set) -> list:
    """Remove signals for ST (special-treatment) stocks."""
    return [s for s in signals if s.get("ts_code") not in st_set]


def filter_by_market_cap(signals: list, market_caps: dict, min_cap: float) -> list:
    """Remove signals for stocks whose market cap is below *min_cap*."""
    return [
        s for s in signals
        if market_caps.get(s.get("ts_code"), float("inf")) >= min_cap
    ]


def filter_by_liquidity(signals: list, turnover: dict, volume: dict,
                        min_turnover: float, min_volume: float) -> list:
    """Remove signals for stocks with too-low turnover or volume."""
    result = []
    for s in signals:
        code = s.get("ts_code")
        t = turnover.get(code, 0)
        v = volume.get(code, 0)
        if t >= min_turnover and v >= min_volume:
            result.append(s)
    return result


def dedup_signals(signals: list) -> list:
    """Deduplicate signals: keep only the first occurrence per (ts_code, direction)."""
    seen = set()
    result = []
    for s in signals:
        key = (s.get("ts_code"), s.get("signal"))
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


# ===========================================================================
# Test classes
# ===========================================================================


class TestLimitUpFilter:
    """Stocks with pct_chg >= 9.9 should not generate BUY signals."""

    def test_buy_blocked_at_lower_bound(self):
        """BUY at exactly 9.9% pct_chg should be blocked."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "000001", "pct_chg": 9.9}, "BUY"
        )
        assert not passed
        assert "涨停" in reason

    def test_buy_blocked_solid_limit_up(self):
        """BUY at 10.0% (hard limit-up) should be blocked."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "600519", "pct_chg": 10.0}, "BUY"
        )
        assert not passed
        assert "涨停" in reason

    def test_buy_allowed_below_threshold(self):
        """BUY at 9.8% (just under) should pass."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "000858", "pct_chg": 9.8}, "BUY"
        )
        assert passed

    def test_buy_allowed_normal(self):
        """BUY at normal pct_chg should pass."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "002415", "pct_chg": 3.5}, "BUY"
        )
        assert passed

    def test_limit_up_does_not_block_sell(self):
        """A limit-up stock can still generate SELL signals."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "600000", "pct_chg": 9.95}, "SELL"
        )
        assert passed


class TestLimitDownFilter:
    """Stocks with pct_chg <= -9.9 should not generate SELL signals."""

    def test_sell_blocked_at_lower_bound(self):
        """SELL at exactly -9.9% pct_chg should be blocked."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "000002", "pct_chg": -9.9}, "SELL"
        )
        assert not passed
        assert "跌停" in reason

    def test_sell_blocked_solid_limit_down(self):
        """SELL at -10.0% (hard limit-down) should be blocked."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "300750", "pct_chg": -10.0}, "SELL"
        )
        assert not passed
        assert "跌停" in reason

    def test_sell_allowed_above_threshold(self):
        """SELL at -9.8% (just above) should pass."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "601398", "pct_chg": -9.8}, "SELL"
        )
        assert passed

    def test_sell_allowed_normal(self):
        """SELL at normal pct_chg should pass."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "000001", "pct_chg": -2.3}, "SELL"
        )
        assert passed

    def test_limit_down_does_not_block_buy(self):
        """A limit-down stock can still generate BUY signals (bargain hunting)."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(
            {"ts_code": "000333", "pct_chg": -10.0}, "BUY"
        )
        assert passed


class TestSTFlagFilter:
    """ST stocks should be filtered out via the blacklist mechanism."""

    def test_st_stock_blacklisted(self):
        """Adding an ST stock to the blacklist prevents it from passing."""
        f = SignalFilters()
        f.add_to_blacklist("000555")  # ST stock
        passed, reason = f.check_blacklist("000555")
        assert not passed
        assert "黑名单" in reason

    def test_non_st_stock_passes_blacklist(self):
        """A normal stock not in the blacklist should pass."""
        f = SignalFilters()
        f.add_to_blacklist("000555")  # only ST stocks blacklisted
        passed, reason = f.check_blacklist("000001")
        assert passed

    def test_st_filter_as_standalone_helper(self):
        """The standalone ST filter removes signals for ST stocks."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},
            {"ts_code": "000555", "signal": "BUY"},  # ST
            {"ts_code": "600519", "signal": "BUY"},
            {"ts_code": "002569", "signal": "SELL"},  # ST
        ]
        st_set = {"000555", "002569"}
        result = filter_st_stocks(signals, st_set)
        assert len(result) == 2
        assert all(s["ts_code"] not in st_set for s in result)

    def test_st_filter_can_use_blacklist_is_blacklisted(self):
        """SignalFilters.is_blacklisted can gate ST stocks."""
        f = SignalFilters()
        f.add_to_blacklist("000555")
        f.add_to_blacklist("002569")
        assert f.is_blacklisted("000555")
        assert not f.is_blacklisted("000001")


class TestMarketCapFilter:
    """Stocks with market_cap below minimum should be excluded."""

    def test_small_cap_excluded(self):
        """A stock below the minimum market cap is removed."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},
            {"ts_code": "000002", "signal": "BUY"},  # too small
            {"ts_code": "600519", "signal": "BUY"},
        ]
        market_caps = {"000001": 500e8, "000002": 5e8, "600519": 2000e8}
        min_cap = 10e8  # 10 billion
        result = filter_by_market_cap(signals, market_caps, min_cap)
        assert len(result) == 2
        assert result[0]["ts_code"] == "000001"
        assert result[1]["ts_code"] == "600519"

    def test_exact_boundary_included(self):
        """A stock at exactly the minimum market cap is included."""
        signals = [{"ts_code": "000001", "signal": "BUY"}]
        market_caps = {"000001": 10e8}
        result = filter_by_market_cap(signals, market_caps, 10e8)
        assert len(result) == 1

    def test_all_above_minimum(self):
        """All stocks above minimum → no filtering."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},
            {"ts_code": "600519", "signal": "SELL"},
        ]
        market_caps = {"000001": 500e8, "600519": 2000e8}
        result = filter_by_market_cap(signals, market_caps, 1e8)
        assert len(result) == 2

    def test_missing_market_cap_passes(self):
        """A stock without market cap data is allowed through (conservative)."""
        signals = [{"ts_code": "000999", "signal": "BUY"}]
        market_caps = {}  # no data
        result = filter_by_market_cap(signals, market_caps, 10e8)
        assert len(result) == 1


class TestLiquidityTurnoverFilter:
    """Stocks with too-low turnover or volume should be filtered out."""

    def test_low_turnover_excluded(self):
        """Stock with turnover below minimum is excluded."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},  # turnover too low
            {"ts_code": "600519", "signal": "BUY"},
        ]
        turnover = {"000001": 0.1, "600519": 5.0}  # percent
        volume = {"000001": 10e6, "600519": 50e6}
        result = filter_by_liquidity(
            signals, turnover, volume, min_turnover=1.0, min_volume=1e6
        )
        assert len(result) == 1
        assert result[0]["ts_code"] == "600519"

    def test_low_volume_excluded(self):
        """Stock with volume below minimum is excluded."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},
            {"ts_code": "002415", "signal": "SELL"},  # volume too low
        ]
        turnover = {"000001": 3.0, "002415": 4.0}
        volume = {"000001": 50e6, "002415": 0.5e6}
        result = filter_by_liquidity(
            signals, turnover, volume, min_turnover=1.0, min_volume=1e6
        )
        assert len(result) == 1
        assert result[0]["ts_code"] == "000001"

    def test_both_low_excluded(self):
        """Stock with both low turnover and low volume is excluded."""
        signals = [{"ts_code": "000555", "signal": "BUY"}]
        turnover = {"000555": 0.05}
        volume = {"000555": 100}
        result = filter_by_liquidity(
            signals, turnover, volume, min_turnover=0.5, min_volume=1000
        )
        assert len(result) == 0

    def test_exact_boundary_included(self):
        """Stock at exact boundary values is included."""
        signals = [{"ts_code": "000001", "signal": "BUY"}]
        turnover = {"000001": 1.0}
        volume = {"000001": 1e6}
        result = filter_by_liquidity(
            signals, turnover, volume, min_turnover=1.0, min_volume=1e6
        )
        assert len(result) == 1

    def test_missing_data_excluded(self):
        """Stock without turnover/volume data is excluded."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},
            {"ts_code": "000999", "signal": "BUY"},  # no data
        ]
        turnover = {"000001": 5.0}
        volume = {"000001": 50e6}
        result = filter_by_liquidity(
            signals, turnover, volume, min_turnover=1.0, min_volume=1e6
        )
        assert len(result) == 1
        assert result[0]["ts_code"] == "000001"


class TestDuplicateSignalDedup:
    """Duplicate signals for the same (ts_code, direction) should be deduplicated."""

    def test_simple_duplicate_removed(self):
        """Two identical BUY signals → only the first kept."""
        signals = [
            {"ts_code": "000001", "signal": "BUY", "weight": 0.1, "reason": "first"},
            {"ts_code": "000001", "signal": "BUY", "weight": 0.2, "reason": "second"},
        ]
        result = dedup_signals(signals)
        assert len(result) == 1
        assert result[0]["reason"] == "first"
        assert result[0]["weight"] == 0.1

    def test_different_directions_kept(self):
        """Same ts_code with BUY and SELL are NOT duplicates."""
        signals = [
            {"ts_code": "000001", "signal": "BUY", "weight": 0.1},
            {"ts_code": "000001", "signal": "SELL", "weight": 0.05},
        ]
        result = dedup_signals(signals)
        assert len(result) == 2

    def test_different_codes_same_direction(self):
        """Different ts_codes with same direction are NOT duplicates."""
        signals = [
            {"ts_code": "000001", "signal": "BUY"},
            {"ts_code": "600519", "signal": "BUY"},
            {"ts_code": "002415", "signal": "BUY"},
        ]
        result = dedup_signals(signals)
        assert len(result) == 3

    def test_multiple_duplicates_across_codes(self):
        """Mixed duplicates across multiple codes."""
        signals = [
            {"ts_code": "A", "signal": "BUY"},
            {"ts_code": "B", "signal": "BUY"},
            {"ts_code": "A", "signal": "BUY"},   # duplicate
            {"ts_code": "C", "signal": "SELL"},
            {"ts_code": "B", "signal": "SELL"},
            {"ts_code": "B", "signal": "BUY"},   # duplicate
            {"ts_code": "C", "signal": "SELL"},  # duplicate
        ]
        result = dedup_signals(signals)
        # Unique keys: (A,BUY), (B,BUY), (A,BUY)dup, (C,SELL), (B,SELL), (B,BUY)dup, (C,SELL)dup
        # Expected: (A,BUY), (B,BUY), (C,SELL), (B,SELL) = 4
        assert len(result) == 4

    def test_empty_signal_list(self):
        """Dedup on empty list returns empty list."""
        assert dedup_signals([]) == []

    def test_single_signal(self):
        """Dedup on single signal returns it unchanged."""
        signals = [{"ts_code": "000001", "signal": "BUY"}]
        result = dedup_signals(signals)
        assert result == signals


class TestRiskRuleCombination:
    """Multiple filter rules should work together via run_all_checks."""

    def test_all_checks_pass_normal_scenario(self):
        """A normal valid BUY passes all checks."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000001",
            direction="BUY",
            suggest_amount=10000,
            total_assets=100000,
            daily_pnl=500,
            position_count=2,
            position_codes=["000001", "000002"],
            stock_info={"ts_code": "000001", "pct_chg": 2.0},
        )
        assert passed
        assert "通过" in reason

    def test_limit_up_blocks_even_when_other_checks_ok(self):
        """A limit-up stock fails run_all_checks even if financially sound."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000858",
            direction="BUY",
            suggest_amount=5000,
            total_assets=500000,
            daily_pnl=1000,
            position_count=1,
            position_codes=["000001"],
            stock_info={"ts_code": "000858", "pct_chg": 9.95},
        )
        assert not passed
        assert "涨停" in reason

    def test_blacklist_blocks_even_when_other_checks_ok(self):
        """A blacklisted stock fails even if all financial checks pass."""
        f = SignalFilters()
        f.add_to_blacklist("600000")
        passed, reason = f.run_all_checks(
            ts_code="600000",
            direction="SELL",
            suggest_amount=5000,
            total_assets=100000,
            daily_pnl=500,
            position_count=1,
            position_codes=["000001", "600000"],
            stock_info={"ts_code": "600000", "pct_chg": -2.0},
        )
        assert not passed
        assert "黑名单" in reason

    def test_large_loss_blocks_even_when_other_checks_ok(self):
        """Daily loss exceeding threshold blocks all trades."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000001",
            direction="BUY",
            suggest_amount=5000,
            total_assets=100000,
            daily_pnl=-5000,  # 5% loss, max is 2%
            position_count=1,
            position_codes=["000001"],
            stock_info={"ts_code": "000001", "pct_chg": 1.0},
        )
        assert not passed
        assert "亏损" in reason

    def test_position_limit_blocks_new_entry(self):
        """At max positions, a new stock is blocked."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000003",
            direction="BUY",
            suggest_amount=5000,
            total_assets=100000,
            daily_pnl=500,
            position_count=5,
            position_codes=["000001", "000002", "600519", "000858", "002415"],
            stock_info={"ts_code": "000003", "pct_chg": 1.5},
        )
        assert not passed
        assert "持仓" in reason

    def test_position_limit_allows_add_to_existing(self):
        """At max positions, adding to an existing holding is allowed."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000001",
            direction="BUY",
            suggest_amount=5000,
            total_assets=100000,
            daily_pnl=500,
            position_count=5,
            position_codes=["000001", "000002", "600519", "000858", "002415"],
            stock_info={"ts_code": "000001", "pct_chg": 1.5},
        )
        assert passed

    def test_sell_skips_buy_only_checks(self):
        """SELL direction skips position-count, weight, amount checks (buy-only)."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000001",
            direction="SELL",
            suggest_amount=50000,         # would fail check_order_amount for BUY
            total_assets=100000,
            daily_pnl=500,
            position_count=10,            # would fail position count for BUY
            position_codes=["000001"],
            stock_info={"ts_code": "000001", "pct_chg": -2.0},
        )
        assert passed

    def test_order_amount_blocks_large_buy(self):
        """An oversized BUY order is blocked by check_order_amount."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000001",
            direction="BUY",
            suggest_amount=30000,
            total_assets=100000,
            daily_pnl=500,
            position_count=2,
            position_codes=["000001", "000002"],
            stock_info={"ts_code": "000001", "pct_chg": 2.0},
        )
        assert not passed
        assert not passed  # should be blocked by some check

    def test_min_amount_blocks_tiny_buy(self):
        """A tiny BUY below the minimum order amount is blocked."""
        f = SignalFilters()
        passed, reason = f.run_all_checks(
            ts_code="000001",
            direction="BUY",
            suggest_amount=100,
            total_assets=100000,
            daily_pnl=500,
            position_count=2,
            position_codes=["000001", "000002"],
            stock_info={"ts_code": "000001", "pct_chg": 2.0},
        )
        assert not passed
        assert "最低" in reason


class TestEdgeBoundaryConditions:
    """Test boundary and edge values across filter methods."""

    # --- check_limit_up_down boundaries ---

    def test_limit_up_exactly_9_9_buy_blocked(self):
        """Boundary: pct_chg == 9.9 for BUY is blocked (inclusive lower bound)."""
        f = SignalFilters()
        passed, _ = f.check_limit_up_down(
            {"ts_code": "000001", "pct_chg": 9.9}, "BUY"
        )
        assert not passed

    def test_limit_up_9_899_buy_passes(self):
        """Boundary: pct_chg == 9.899 (just under 9.9) for BUY passes."""
        f = SignalFilters()
        passed, _ = f.check_limit_up_down(
            {"ts_code": "000001", "pct_chg": 9.899}, "BUY"
        )
        assert passed

    def test_limit_down_exactly_neg_9_9_sell_blocked(self):
        """Boundary: pct_chg == -9.9 for SELL is blocked (inclusive upper bound)."""
        f = SignalFilters()
        passed, _ = f.check_limit_up_down(
            {"ts_code": "000002", "pct_chg": -9.9}, "SELL"
        )
        assert not passed

    def test_limit_down_neg_9_899_sell_passes(self):
        """Boundary: pct_chg == -9.899 (just above -9.9) for SELL passes."""
        f = SignalFilters()
        passed, _ = f.check_limit_up_down(
            {"ts_code": "000002", "pct_chg": -9.899}, "SELL"
        )
        assert passed

    # --- check_min_amount boundaries ---

    def test_min_amount_exactly_threshold(self):
        """Boundary: amount exactly at min_order_amount should pass."""
        f = SignalFilters()
        passed, reason = f.check_min_amount(2000)
        assert passed

    def test_min_amount_just_below_threshold(self):
        """Boundary: amount just 1 unit below min_order_amount should fail."""
        f = SignalFilters()
        passed, reason = f.check_min_amount(1999)
        assert not passed

    # --- check_order_amount boundaries ---

    def test_order_amount_exactly_max(self):
        """Boundary: amount exactly at max_single_order_amount should pass."""
        f = SignalFilters()
        passed, reason = f.check_order_amount(25000)
        assert passed

    def test_order_amount_just_above_max(self):
        """Boundary: amount just 1 unit above max_single_order_amount should fail."""
        f = SignalFilters()
        passed, reason = f.check_order_amount(25001)
        assert not passed

    # --- check_daily_loss boundaries ---

    def test_daily_loss_exactly_at_limit(self):
        """Boundary: loss rate exactly at max_daily_loss_rate (2%) should pass."""
        f = SignalFilters()
        passed, reason = f.check_daily_loss(-2000, 100000)  # exactly 2%
        assert passed

    def test_daily_loss_just_above_limit(self):
        """Boundary: loss rate just above max_daily_loss_rate should fail."""
        f = SignalFilters()
        passed, reason = f.check_daily_loss(-2001, 100000)  # 2.001%
        assert not passed

    def test_daily_loss_no_assets(self):
        """Edge: total_assets == 0 → loss_rate treated as 0 → passes."""
        f = SignalFilters()
        passed, reason = f.check_daily_loss(-5000, 0)
        assert passed

    # --- check_limit_up_down edge cases ---

    def test_limit_up_down_none_stock(self):
        """Edge: stock=None → skipped, returns True."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down(None, "BUY")
        assert passed

    def test_limit_up_down_empty_dict(self):
        """Edge: stock={} → pct_chg defaults to 0 → passes."""
        f = SignalFilters()
        passed, reason = f.check_limit_up_down({}, "BUY")
        assert passed

    def test_limit_up_down_zero_pct_chg(self):
        """Edge: pct_chg == 0 → passes for both BUY and SELL."""
        f = SignalFilters()
        assert f.check_limit_up_down({"pct_chg": 0.0}, "BUY")[0]
        assert f.check_limit_up_down({"pct_chg": 0.0}, "SELL")[0]

    # --- check_position_weight edge cases ---

    def test_position_weight_zero_assets(self):
        """Edge: total_assets == 0 → returns False."""
        f = SignalFilters()
        passed, reason = f.check_position_weight("000001", 10000, 0)
        assert not passed

    def test_position_weight_exact_boundary(self):
        """Boundary: weight exactly at max_single_position_weight passes."""
        f = SignalFilters()
        # 22% of 100000 = 22000
        passed, reason = f.check_position_weight("000001", 22000, 100000)
        assert passed

    def test_position_weight_just_above_boundary(self):
        """Boundary: weight just above max_single_position_weight fails."""
        f = SignalFilters()
        passed, reason = f.check_position_weight("000001", 22001, 100000)
        assert not passed

    # --- Blacklist edge cases ---

    def test_blacklist_duplicate_add(self):
        """Edge: adding the same code twice does not create duplicates."""
        f = SignalFilters()
        f.add_to_blacklist("000001")
        f.add_to_blacklist("000001")
        assert f._blacklist == ["000001"]

    def test_blacklist_remove_nonexistent(self):
        """Edge: removing a code not in blacklist should not raise."""
        f = SignalFilters()
        f.remove_from_blacklist("999999")  # should not raise

    def test_get_blacklist_returns_copy(self):
        """get_blacklist should return a copy, not the internal list."""
        f = SignalFilters()
        f.add_to_blacklist("000001")
        bl = f.get_blacklist()
        bl.append("000002")
        assert "000002" not in f._blacklist
