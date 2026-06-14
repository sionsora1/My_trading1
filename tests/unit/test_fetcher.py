"""
tests/unit/test_fetcher.py — Unit tests for DataFetcher class.

Covers:
- TC-F01: _parse_ts_code static method (no network)
- TC-F02: get_minute_data returns DataFrame with expected columns
- TC-F03: get_intraday_data returns DataFrame
- TC-F04: Method signatures for build_market_data_by_date, get_daily_data,
          get_financial_data, get_stock_info, get_realtime_quotes
- TC-F05: Return types of key methods
- TC-F06: Calculator methods (calculate_ma, calculate_returns, etc. — no network)
"""

import pandas as pd
import numpy as np
import pytest
from inspect import signature, Parameter


class TestDataFetcher:
    """Unit tests for DataFetcher."""

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _make_fetcher():
        """Instantiate a fresh DataFetcher."""
        from data.fetcher import DataFetcher
        return DataFetcher()

    @staticmethod
    def _sample_ohlcv_df() -> pd.DataFrame:
        """Return a minimal OHLCV DataFrame for calculator tests.

        Creates 30 rows of synthetic daily bar data so rolling windows
        (up to ma20, vol20, return_20d) have enough observations.
        """
        np.random.seed(42)
        n = 30
        close = 10.0 + np.cumsum(np.random.randn(n) * 0.5)
        # Ensure close never goes negative or zero
        close = np.maximum(close, 0.1)
        df = pd.DataFrame({
            "trade_date": pd.date_range("2025-01-01", periods=n, freq="B"),
            "open": close * (1 + np.random.randn(n) * 0.01),
            "high": close * (1 + np.abs(np.random.randn(n) * 0.02)),
            "low": close * (1 - np.abs(np.random.randn(n) * 0.02)),
            "close": close,
            "vol": np.random.randint(10000, 50000, size=n).astype(float),
        })
        return df

    # ==================================================================
    # TC-F01: _parse_ts_code — static, no network needed
    # ==================================================================

    @pytest.mark.parametrize("ts_code,expected_symbol,expected_market", [
        ("600519", "600519", "sh"),
        ("600519.SH", "600519", "sh"),
        ("000001", "000001", "sz"),
        ("000001.SZ", "000001", "sz"),
        ("002415", "002415", "sz"),
        ("300750", "300750", "sz"),
        ("300750.SZ", "300750", "sz"),
        ("601398", "601398", "sh"),
        ("688001", "688001", "sh"),
        ("688001.SH", "688001", "sh"),
        ("000858", "000858", "sz"),
    ])
    def test_parse_ts_code_variants(
        self, ts_code, expected_symbol, expected_market
    ):
        """TC-F01: _parse_ts_code handles short and full ts_code formats."""
        from data.fetcher import DataFetcher

        symbol, market = DataFetcher._parse_ts_code(ts_code)
        assert symbol == expected_symbol, (
            f"Symbol mismatch for '{ts_code}': "
            f"expected '{expected_symbol}', got '{symbol}'"
        )
        assert market == expected_market, (
            f"Market mismatch for '{ts_code}': "
            f"expected '{expected_market}', got '{market}'"
        )

    def test_parse_ts_code_returns_tuple(self):
        """_parse_ts_code always returns a 2-tuple."""
        from data.fetcher import DataFetcher

        result = DataFetcher._parse_ts_code("600519")
        assert isinstance(result, tuple), (
            f"Expected tuple, got {type(result).__name__}"
        )
        assert len(result) == 2, f"Expected 2 elements, got {len(result)}"

    def test_parse_ts_code_static_method(self):
        """_parse_ts_code is a staticmethod callable without an instance."""
        from data.fetcher import DataFetcher

        # Verify we can call it on the class directly
        symbol, market = DataFetcher._parse_ts_code("000001.SZ")
        assert symbol == "000001"
        assert market == "sz"

    # ==================================================================
    # TC-F02: get_minute_data — network, marked slow + real_data
    # ==================================================================

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_get_minute_data_returns_dataframe(self, real_stock_pool):
        """get_minute_data returns a pd.DataFrame (may be empty on failure)."""
        fetcher = self._make_fetcher()
        code = real_stock_pool[0]
        df = fetcher.get_minute_data(code, period="5")
        assert isinstance(df, pd.DataFrame), (
            f"Expected DataFrame, got {type(df).__name__}"
        )

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_get_minute_data_columns_when_not_empty(self, real_stock_pool):
        """When get_minute_data returns non-empty data, columns include
        ts_code and period."""
        fetcher = self._make_fetcher()
        code = real_stock_pool[0]
        df = fetcher.get_minute_data(code, period="5")
        if df.empty:
            pytest.skip(f"No minute data available for {code} (empty response)")
        assert "ts_code" in df.columns, (
            f"Missing 'ts_code' column. Columns: {list(df.columns)}"
        )
        assert "period" in df.columns, (
            f"Missing 'period' column. Columns: {list(df.columns)}"
        )
        # The ts_code values should match what was requested
        assert (df["ts_code"] == code).all(), (
            f"ts_code column does not match requested code '{code}'"
        )

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_get_minute_data_with_time_range(self, real_stock_pool):
        """get_minute_data accepts start_time and end_time parameters."""
        fetcher = self._make_fetcher()
        code = real_stock_pool[0]
        df = fetcher.get_minute_data(
            code, period="5",
            start_time="2025-06-10 09:30:00",
            end_time="2025-06-10 15:00:00",
        )
        assert isinstance(df, pd.DataFrame)
        # Empty is acceptable if no data for that date; just verify no crash
        if not df.empty:
            assert "ts_code" in df.columns
            assert "period" in df.columns

    # ==================================================================
    # TC-F03: get_intraday_data — network, marked slow + real_data
    # ==================================================================

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_get_intraday_data_returns_dataframe(self, real_stock_pool):
        """get_intraday_data returns a pd.DataFrame."""
        fetcher = self._make_fetcher()
        code = real_stock_pool[0]
        df = fetcher.get_intraday_data(code)
        assert isinstance(df, pd.DataFrame), (
            f"Expected DataFrame, got {type(df).__name__}"
        )

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_get_intraday_data_has_ts_code_when_not_empty(self, real_stock_pool):
        """When get_intraday_data returns non-empty data, it includes
        a ts_code column."""
        fetcher = self._make_fetcher()
        code = real_stock_pool[0]
        df = fetcher.get_intraday_data(code)
        if df.empty:
            pytest.skip(f"No intraday data available for {code} (empty response)")
        assert "ts_code" in df.columns, (
            f"Missing 'ts_code' column. Columns: {list(df.columns)}"
        )

    # ==================================================================
    # TC-F04: Method signatures — verify existence and parameter names
    # ==================================================================

    def test_build_market_data_by_date_signature(self):
        """build_market_data_by_date exists and accepts the right parameters."""
        fetcher = self._make_fetcher()
        sig = signature(fetcher.build_market_data_by_date)
        param_names = list(sig.parameters.keys())
        assert "stock_codes" in param_names, (
            f"Missing 'stock_codes' param. Params: {param_names}"
        )
        assert "start_date" in param_names, (
            f"Missing 'start_date' param. Params: {param_names}"
        )
        assert "end_date" in param_names, (
            f"Missing 'end_date' param. Params: {param_names}"
        )

    def test_get_daily_data_signature(self):
        """get_daily_data exists and accepts (ts_code, start_date, end_date)."""
        fetcher = self._make_fetcher()
        sig = signature(fetcher.get_daily_data)
        param_names = list(sig.parameters.keys())
        assert "ts_code" in param_names, (
            f"Missing 'ts_code' param. Params: {param_names}"
        )
        assert "start_date" in param_names, (
            f"Missing 'start_date' param. Params: {param_names}"
        )
        assert "end_date" in param_names, (
            f"Missing 'end_date' param. Params: {param_names}"
        )

    def test_get_financial_data_signature(self):
        """get_financial_data exists and accepts a symbol parameter."""
        fetcher = self._make_fetcher()
        sig = signature(fetcher.get_financial_data)
        param_names = list(sig.parameters.keys())
        assert "symbol" in param_names, (
            f"Missing 'symbol' param. Params: {param_names}"
        )

    def test_get_stock_info_signature(self):
        """get_stock_info exists and accepts a symbol parameter."""
        fetcher = self._make_fetcher()
        sig = signature(fetcher.get_stock_info)
        param_names = list(sig.parameters.keys())
        assert "symbol" in param_names, (
            f"Missing 'symbol' param. Params: {param_names}"
        )

    def test_get_realtime_quotes_signature(self):
        """get_realtime_quotes exists and accepts a stock_pool parameter."""
        fetcher = self._make_fetcher()
        sig = signature(fetcher.get_realtime_quotes)
        param_names = list(sig.parameters.keys())
        assert "stock_pool" in param_names, (
            f"Missing 'stock_pool' param. Params: {param_names}"
        )

    def test_all_key_methods_exist(self):
        """All key methods are present on a DataFetcher instance."""
        fetcher = self._make_fetcher()
        required_methods = [
            "build_market_data_by_date",
            "get_daily_data",
            "get_financial_data",
            "get_stock_info",
            "get_realtime_quotes",
            "get_minute_data",
            "get_intraday_data",
            "build_stock_data",
            "calculate_ma",
            "calculate_returns",
            "calculate_volatility",
            "_parse_ts_code",
        ]
        for method_name in required_methods:
            assert hasattr(fetcher, method_name), (
                f"DataFetcher is missing method: '{method_name}'"
            )
            method = getattr(fetcher, method_name)
            assert callable(method), (
                f"'{method_name}' exists but is not callable"
            )

    # ==================================================================
    # TC-F05: Return types — verify key methods return correct types
    # ==================================================================

    def test_get_realtime_quotes_returns_dict(self):
        """get_realtime_quotes returns a dict (even when given empty list)."""
        fetcher = self._make_fetcher()
        result = fetcher.get_realtime_quotes([])
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}"
        )

    def test_get_realtime_quotes_empty_list(self):
        """get_realtime_quotes with empty list returns empty dict."""
        fetcher = self._make_fetcher()
        result = fetcher.get_realtime_quotes([])
        assert result == {}, f"Expected empty dict, got {result}"

    def test_get_financial_data_returns_dict_structure(self):
        """get_financial_data returns a dict with expected financial keys.

        Uses a mock approach: if the live call fails, the method still
        returns a dict (via _empty_financial), so the return-type
        assertion always holds.
        """
        fetcher = self._make_fetcher()
        result = fetcher.get_financial_data("600519")
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}"
        )
        expected_keys = {
            "roe", "gross_margin", "profit_growth", "revenue_growth",
            "accrual_ratio", "net_assets", "revenue", "net_profit", "ocf",
        }
        missing = expected_keys - set(result.keys())
        assert not missing, (
            f"Financial data dict missing keys: {missing}"
        )

    def test_get_stock_info_returns_dict(self):
        """get_stock_info returns a dict with expected keys."""
        fetcher = self._make_fetcher()
        result = fetcher.get_stock_info("600519")
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}"
        )
        expected_keys = {"name", "industry", "market_cap", "circ_market_cap", "pe", "pb"}
        missing = expected_keys - set(result.keys())
        assert not missing, (
            f"Stock info dict missing keys: {missing}"
        )

    def test_build_stock_data_returns_dict(self):
        """build_stock_data returns a dict (or empty dict if no data)."""
        fetcher = self._make_fetcher()
        result = fetcher.build_stock_data("600519", lookback_days=30)
        assert isinstance(result, dict), (
            f"Expected dict, got {type(result).__name__}"
        )

    @pytest.mark.slow
    @pytest.mark.real_data
    def test_get_daily_data_return_type(self, real_stock_pool):
        """get_daily_data returns a pd.DataFrame."""
        fetcher = self._make_fetcher()
        df = fetcher.get_daily_data(
            real_stock_pool[0], "20250101", "20250601"
        )
        assert isinstance(df, pd.DataFrame), (
            f"Expected DataFrame, got {type(df).__name__}"
        )

    # ==================================================================
    # TC-F06: Calculator methods — pure functions, no network needed
    # ==================================================================

    def test_calculate_ma_adds_columns(self):
        """calculate_ma adds ma{w} columns for each window."""
        fetcher = self._make_fetcher()
        df = self._sample_ohlcv_df()
        result = fetcher.calculate_ma(df, windows=[5, 10, 20])

        for w in [5, 10, 20]:
            col = f"ma{w}"
            assert col in result.columns, f"Missing column: {col}"
            # First (w-1) values should be NaN
            assert result[col].iloc[:w - 1].isna().all(), (
                f"{col}: first {w - 1} rows should be NaN"
            )
            # The w-th value should be finite
            assert pd.notna(result[col].iloc[w - 1]), (
                f"{col}: row {w - 1} should be non-NaN"
            )

    def test_calculate_ma_returns_dataframe(self):
        """calculate_ma returns a pd.DataFrame."""
        fetcher = self._make_fetcher()
        df = self._sample_ohlcv_df()
        result = fetcher.calculate_ma(df)
        assert isinstance(result, pd.DataFrame), (
            f"Expected DataFrame, got {type(result).__name__}"
        )
        # Original row count preserved
        assert len(result) == len(df), (
            f"Row count changed: {len(result)} != {len(df)}"
        )

    def test_calculate_returns_adds_columns(self):
        """calculate_returns adds return_{p}d columns."""
        fetcher = self._make_fetcher()
        df = self._sample_ohlcv_df()
        result = fetcher.calculate_returns(df, periods=[1, 5, 10])

        for p in [1, 5, 10]:
            col = f"return_{p}d"
            assert col in result.columns, f"Missing column: {col}"

        # 1-day return: first value NaN, second value finite
        assert pd.isna(result["return_1d"].iloc[0]), (
            "return_1d: first row should be NaN"
        )
        assert pd.notna(result["return_1d"].iloc[1]), (
            "return_1d: second row should be non-NaN"
        )

    def test_calculate_volatility_adds_column(self):
        """calculate_volatility adds a volatility_{window}d column."""
        fetcher = self._make_fetcher()
        df = self._sample_ohlcv_df()
        window = 20
        result = fetcher.calculate_volatility(df, window=window)

        col = f"volatility_{window}d"
        assert col in result.columns, f"Missing column: {col}"

        # First `window` values should be NaN (window-1 from rolling + 1 from pct_change)
        assert result[col].iloc[:window].isna().all(), (
            f"{col}: first {window} rows should be NaN"
        )
        assert pd.notna(result[col].iloc[window]), (
            f"{col}: row {window} should be non-NaN"
        )

    def test_calculate_volatility_non_negative(self):
        """Volatility values (when not NaN) should be >= 0."""
        fetcher = self._make_fetcher()
        df = self._sample_ohlcv_df()
        result = fetcher.calculate_volatility(df, window=20)
        col = "volatility_20d"
        valid = result[col].dropna()
        assert (valid >= 0).all(), (
            f"Volatility values should be >= 0, got min={valid.min()}"
        )

    def test_calculate_methods_dont_corrupt_close(self):
        """Calculator methods may add columns in-place but must preserve
        original close values."""
        fetcher = self._make_fetcher()
        df = self._sample_ohlcv_df()
        original_close = df["close"].copy()

        # Run all calculators (they add columns in-place)
        _ = fetcher.calculate_ma(df)
        _ = fetcher.calculate_returns(df)
        _ = fetcher.calculate_volatility(df)

        # Close values must be unchanged even if columns were added
        assert (df["close"] == original_close).all(), (
            "Original close values were corrupted by calculators"
        )
        # Verify the new columns were actually added (side effect is expected)
        assert "ma20" in df.columns, "ma20 column should have been added"
        assert "return_1d" in df.columns, "return_1d column should have been added"
