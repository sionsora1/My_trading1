"""
Integration tests for BacktestEngine.

TC-BE01: No look-ahead bias in financial data — verify that
         get_financial_for_date() never returns a report_date
         that is after the given trade_date.

TC-BE02: Backtest runs with real data without error — verify
         that run() returns the expected result structure with
         all required metric keys present.
"""

import pytest


class TestBacktestEngine:
    """Integration tests for BacktestEngine against real data sources."""

    # ── TC-BE01: No look-ahead bias ──────────────────────────────────

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_no_look_ahead_bias_in_financials(
        self, real_db, real_stock_pool, cached_market_data, cached_fundamentals
    ):
        """
        TC-BE01: Verify get_financial_for_date() does NOT return a
        financial report whose report_date is after the given trade_date.

        For every stock-date combination in the cached market data
        (sampled to keep the test fast), the returned fundamentals row
        must have report_date <= trade_date.
        """
        if not cached_market_data:
            pytest.skip("No market data available in cache")

        # Sample: use the first 3 stocks and every 30th date to stay fast
        codes = real_stock_pool[:3]
        dates = sorted(cached_market_data.keys())
        sampled_dates = dates[::max(1, len(dates) // 15)][:15]

        violations = []
        checked = 0

        for code in codes:
            ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            for trade_date in sampled_dates:
                fin = real_db.get_financial_for_date(ts_code, trade_date)
                if fin is None:
                    continue  # no financial data is acceptable
                checked += 1
                report_date = fin.get("report_date", "")
                if report_date and report_date > trade_date:
                    violations.append(
                        f"{code} @ trade_date={trade_date}: "
                        f"report_date={report_date} is in the future"
                    )

        assert checked > 0, (
            "No financial data found for any sampled stock-date pair — "
            "check that fundamentals have been populated in the test DB"
        )
        assert not violations, (
            f"Look-ahead bias detected: {len(violations)} violations\n"
            + "\n".join(violations[:10])
        )

    # ── TC-BE02: Backtest runs without error ─────────────────────────

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_backtest_runs_with_real_data(
        self, cached_market_data
    ):
        """
        TC-BE02: Instantiate BacktestEngine, configure it, run a
        backtest with real market data and a simple strategy, then
        verify the result dict contains the expected top-level keys
        and the metrics sub-dict contains all required fields.
        """
        if not cached_market_data:
            pytest.skip("No market data available in cache")

        from backtest.engine import BacktestEngine, BacktestConfig
        from strategy.momentum_strategy import MomentumStrategy

        # ── Configure a conservative backtest ──
        config = BacktestConfig(
            initial_capital=100_000,
            commission_rate=0.0003,
            slippage_rate=0.002,
            max_position_num=3,
            max_single_weight=0.15,
            stop_loss_rate=-0.08,
            move_stop_rate=-0.10,
            rebalance_frequency="daily",
            t_plus_1=False,  # disable T+1 to allow trades immediately
        )

        engine = BacktestEngine(config)
        strategy = MomentumStrategy({"lookback": 20, "top_n": 3})

        # Run with print_report=False to keep output clean
        result = engine.run(
            cached_market_data,
            strategy,
            print_report=False,
        )

        # ── Verify top-level keys ──
        expected_top_keys = {
            "metrics",
            "daily_nav",
            "benchmark_nav",
            "trade_records",
            "daily_operations",
            "final_portfolio",
        }
        actual_top_keys = set(result.keys())
        missing_top = expected_top_keys - actual_top_keys
        assert not missing_top, (
            f"Missing top-level keys in run() result: {missing_top}"
        )

        # ── Verify metrics sub-dict keys ──
        metrics = result["metrics"]
        expected_metric_keys = {
            "total_return",
            "annual_return",
            "max_drawdown",
            "annual_volatility",
            "sharpe_ratio",
            "calmar_ratio",
            "win_rate",
            "trade_win_rate",
            "profit_loss_ratio",
            "annual_turnover",
            "total_trades",
            "total_commission",
            "total_slippage",
            "cost_ratio",
            "start_date",
            "end_date",
            "trading_days",
        }
        actual_metric_keys = set(metrics.keys())
        missing_metrics = expected_metric_keys - actual_metric_keys
        assert not missing_metrics, (
            f"Missing metric keys: {missing_metrics}"
        )

        # ── Verify types / sanity ──
        assert isinstance(result["daily_nav"], list), "daily_nav should be a list"
        assert isinstance(result["trade_records"], list), "trade_records should be a list"
        assert isinstance(result["daily_operations"], list), "daily_operations should be a list"
        assert isinstance(result["final_portfolio"], dict), "final_portfolio should be a dict"

        # Basic sanity checks on metrics values
        assert -1.0 <= metrics["total_return"] <= 100.0, (
            f"total_return={metrics['total_return']} out of reasonable range"
        )
        assert -1.0 <= metrics["max_drawdown"] <= 0.0, (
            f"max_drawdown={metrics['max_drawdown']} should be <= 0"
        )
        assert metrics["trading_days"] > 0, "should have at least 1 trading day"
        assert metrics["start_date"] <= metrics["end_date"], (
            "start_date should be before end_date"
        )
