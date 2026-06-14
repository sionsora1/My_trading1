"""
tests/integration/test_data_pipeline.py — Integration tests for the data pipeline:
AKShare → validation → DB insert → read-back.

Test cases:
    TC-DP01: Full pipeline end-to-end — construct data, validate, insert, read back
    TC-DP02: Data consistency — DB round-trip preserves all values exactly
    TC-DP03: Invalid data interception — invalid rows are filtered before DB insert
"""

import pytest
from data.validator import DataValidator


class TestDataPipeline:
    """Integration tests for the data pipeline: validation → insert → read-back."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_bar(ts_code, trade_date, open_, high, low, close,
                  volume=1_000_000, amount=None, turnover=0.01, pct_chg=1.0):
        """Build a synthetic daily bar row dict with defaulted fields."""
        if amount is None:
            amount = volume * close
        return {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "turnover": turnover,
            "pct_chg": pct_chg,
        }

    # ------------------------------------------------------------------
    # TC-DP01: Full pipeline end-to-end
    # ------------------------------------------------------------------

    def test_pipeline_end_to_end(self, real_db, real_stock_pool):
        """TC-DP01: Construct data → validate → insert to DB → read back.

        Verify data integrity through the full pipeline chain.
        """
        code = real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        # 1. Construct synthetic daily bar data (5 trading days)
        rows = []
        base_close = 20.0
        for i in range(5):
            trade_date = f"2010-01-{4 + i:02d}"
            close = base_close + i * 0.5
            rows.append(self._make_bar(
                ts_code=ts_code,
                trade_date=trade_date,
                open_=close - 0.2,
                high=close + 0.3,
                low=close - 0.4,
                close=close,
                pct_chg=2.0,
            ))

        # 2. Validate each row
        for row in rows:
            ok, reason = DataValidator.validate_daily_bar(row)
            assert ok, f"Expected valid bar, got: {reason}"

        # 3. Insert to DB
        real_db.upsert_daily_bars(rows)

        # 4. Read back from DB
        read_back = real_db.get_daily_bars(ts_code, "2010-01-01", "2010-12-31")

        # 5. Verify data integrity
        assert len(read_back) == 5, (
            f"Expected 5 rows, got {len(read_back)}"
        )

        # Check each inserted date is present with correct close prices
        inserted_closes = {row["trade_date"]: row["close"] for row in rows}

        for db_row in read_back:
            assert db_row["ts_code"] == ts_code, (
                f"ts_code mismatch: {db_row['ts_code']} != {ts_code}"
            )
            date = db_row["trade_date"]
            assert date in inserted_closes, (
                f"Unexpected date in DB: {date}"
            )
            assert db_row["close"] == inserted_closes[date], (
                f"Close mismatch for {date}: "
                f"expected {inserted_closes[date]}, got {db_row['close']}"
            )
            # Additional field sanity checks
            assert db_row["open"] is not None
            assert db_row["high"] is not None
            assert db_row["low"] is not None
            assert db_row["volume"] > 0

    # ------------------------------------------------------------------
    # TC-DP02: Data consistency
    # ------------------------------------------------------------------

    def test_data_consistency(self, real_db, real_stock_pool):
        """TC-DP02: Data read from DB should match what was inserted exactly.

        Same close prices, same dates, same OHLCV values after round-trip.
        """
        # Use the second stock to avoid interfering with DP01's data
        code = real_stock_pool[1] if len(real_stock_pool) > 1 else real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        # Construct precise values for 3 trading days
        batch = [
            {
                "ts_code": ts_code,
                "trade_date": "2011-06-01",
                "open": 15.35,
                "high": 15.90,
                "low": 15.10,
                "close": 15.72,
                "volume": 8_500_000.0,
                "amount": 133_620_000.0,
                "turnover": 0.042,
                "pct_chg": 1.23,
            },
            {
                "ts_code": ts_code,
                "trade_date": "2011-06-02",
                "open": 15.72,
                "high": 16.01,
                "low": 15.55,
                "close": 15.88,
                "volume": 7_200_000.0,
                "amount": 114_336_000.0,
                "turnover": 0.036,
                "pct_chg": 1.02,
            },
            {
                "ts_code": ts_code,
                "trade_date": "2011-06-03",
                "open": 15.88,
                "high": 16.22,
                "low": 15.76,
                "close": 15.95,
                "volume": 6_100_000.0,
                "amount": 97_295_000.0,
                "turnover": 0.031,
                "pct_chg": 0.44,
            },
        ]

        # Validate all rows
        for row in batch:
            ok, reason = DataValidator.validate_daily_bar(row)
            assert ok, f"Expected valid bar for date {row['trade_date']}, got: {reason}"

        # Insert to DB
        real_db.upsert_daily_bars(batch)

        # Read back
        read_back = real_db.get_daily_bars(ts_code, "2011-06-01", "2011-06-03")
        assert len(read_back) == len(batch), (
            f"Expected {len(batch)} rows, got {len(read_back)}"
        )

        # Sort both by trade_date for pairwise comparison
        read_back.sort(key=lambda x: x["trade_date"])
        batch.sort(key=lambda x: x["trade_date"])

        # Assert every field matches exactly for each row
        for original, retrieved in zip(batch, read_back):
            date = original["trade_date"]
            assert retrieved["ts_code"] == original["ts_code"], (
                f"ts_code mismatch on {date}"
            )
            assert retrieved["trade_date"] == date, (
                f"trade_date mismatch: {retrieved['trade_date']} != {date}"
            )
            assert retrieved["open"] == original["open"], (
                f"open mismatch on {date}: {retrieved['open']} != {original['open']}"
            )
            assert retrieved["high"] == original["high"], (
                f"high mismatch on {date}: {retrieved['high']} != {original['high']}"
            )
            assert retrieved["low"] == original["low"], (
                f"low mismatch on {date}: {retrieved['low']} != {original['low']}"
            )
            assert retrieved["close"] == original["close"], (
                f"close mismatch on {date}: {retrieved['close']} != {original['close']}"
            )
            assert retrieved["volume"] == original["volume"], (
                f"volume mismatch on {date}: {retrieved['volume']} != {original['volume']}"
            )
            assert retrieved["amount"] == original["amount"], (
                f"amount mismatch on {date}: {retrieved['amount']} != {original['amount']}"
            )
            assert retrieved["turnover"] == original["turnover"], (
                f"turnover mismatch on {date}: {retrieved['turnover']} != {original['turnover']}"
            )
            assert retrieved["pct_chg"] == original["pct_chg"], (
                f"pct_chg mismatch on {date}: {retrieved['pct_chg']} != {original['pct_chg']}"
            )

    # ------------------------------------------------------------------
    # TC-DP03: Invalid data interception
    # ------------------------------------------------------------------

    def test_invalid_data_filtered_before_db(self, real_db, real_stock_pool):
        """TC-DP03: Invalid rows (e.g., all-zero OHLCV) should be filtered out
        by the validator before reaching the database.
        """
        code = real_stock_pool[2] if len(real_stock_pool) > 2 else real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        # Build a mix of valid and invalid rows
        mixed_rows = [
            # Valid row 1
            {
                "ts_code": ts_code,
                "trade_date": "2012-03-01",
                "open": 25.0,
                "high": 25.8,
                "low": 24.6,
                "close": 25.5,
                "volume": 2_000_000,
                "amount": 51_000_000,
                "turnover": 0.02,
                "pct_chg": 1.5,
            },
            # Invalid: all-zero OHLCV
            {
                "ts_code": ts_code,
                "trade_date": "2012-03-02",
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "close": 0.0,
                "volume": 0,
                "amount": 0,
                "turnover": 0.0,
                "pct_chg": 0.0,
            },
            # Valid row 2
            {
                "ts_code": ts_code,
                "trade_date": "2012-03-03",
                "open": 25.5,
                "high": 26.1,
                "low": 25.3,
                "close": 25.9,
                "volume": 1_500_000,
                "amount": 38_850_000,
                "turnover": 0.015,
                "pct_chg": 1.57,
            },
            # Invalid: high < low
            {
                "ts_code": ts_code,
                "trade_date": "2012-03-04",
                "open": 25.9,
                "high": 25.0,
                "low": 26.0,
                "close": 25.7,
                "volume": 1_000_000,
                "amount": 25_700_000,
                "turnover": 0.01,
                "pct_chg": -0.77,
            },
            # Valid row 3
            {
                "ts_code": ts_code,
                "trade_date": "2012-03-05",
                "open": 25.9,
                "high": 26.0,
                "low": 25.4,
                "close": 25.6,
                "volume": 1_800_000,
                "amount": 46_080_000,
                "turnover": 0.018,
                "pct_chg": -1.16,
            },
        ]

        # Total rows before filtering
        assert len(mixed_rows) == 5, (
            f"Expected 5 mixed rows, got {len(mixed_rows)}"
        )

        # Filter through validator
        valid_rows = DataValidator.filter_valid_daily_bars(mixed_rows)

        # Assert invalid rows were removed
        assert len(valid_rows) == 3, (
            f"Expected 3 valid rows after filtering, got {len(valid_rows)}"
        )

        # Verify the valid rows are exactly the ones we expect
        valid_dates = {row["trade_date"] for row in valid_rows}
        expected_valid_dates = {"2012-03-01", "2012-03-03", "2012-03-05"}
        assert valid_dates == expected_valid_dates, (
            f"Expected valid dates {expected_valid_dates}, got {valid_dates}"
        )

        # Verify specific invalid rows are NOT in the valid set
        invalid_dates = {"2012-03-02", "2012-03-04"}
        assert invalid_dates.isdisjoint(valid_dates), (
            f"Invalid dates {invalid_dates} should not be in valid set"
        )

        # Insert only the validated rows to DB
        real_db.upsert_daily_bars(valid_rows)

        # Read back from DB — should only contain the valid rows
        db_rows = real_db.get_daily_bars(ts_code, "2012-03-01", "2012-03-05")
        db_dates = {row["trade_date"] for row in db_rows}

        # DB should contain only the 3 valid dates, not the 2 invalid ones
        assert len(db_rows) == 3, (
            f"Expected 3 rows in DB, got {len(db_rows)}"
        )
        assert db_dates == expected_valid_dates, (
            f"DB dates {db_dates} should equal expected valid dates {expected_valid_dates}"
        )

        # Double-check: invalid dates are definitely NOT in the DB
        for invalid_date in invalid_dates:
            assert invalid_date not in db_dates, (
                f"Invalid date {invalid_date} leaked into the database!"
            )

    # ------------------------------------------------------------------
    # TC-DP-slow: Full pipeline with real AKShare data (network required)
    # ------------------------------------------------------------------

    @pytest.mark.slow
    def test_pipeline_with_real_data(self, real_db, real_stock_pool, cached_market_data):
        """Full pipeline test using real AKShare market data when available.

        If cached_market_data is empty (network unavailable), skip the test.
        Otherwise, push real data through validator, insert to DB, and read back.
        """
        if not cached_market_data:
            pytest.skip("cached_market_data is empty (network may be down)")

        # Process real market data for the first stock
        code = real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        # Collect all daily bar rows for this stock from the cached data
        bars = []
        for date_str, stocks in cached_market_data.items():
            if not isinstance(stocks, dict):
                continue
            stock_data = stocks.get(code)
            if not isinstance(stock_data, dict):
                continue

            bars.append({
                "ts_code": ts_code,
                "trade_date": date_str,
                "open": stock_data.get("open", 0) or 0,
                "high": stock_data.get("high", 0) or 0,
                "low": stock_data.get("low", 0) or 0,
                "close": stock_data.get("close", 0) or 0,
                "volume": stock_data.get("volume", 0) or 0,
                "amount": (stock_data.get("volume", 0) or 0) * (stock_data.get("close", 0) or 0),
                "turnover": stock_data.get("turnover", 0) or 0,
                "pct_chg": (float(stock_data.get("return_1d", 0) or 0)) * 100,
            })

        # Must have at least a few bars to continue
        if len(bars) < 3:
            pytest.skip(f"Not enough daily bars for {code} (got {len(bars)})")

        # Validate all rows
        valid_bars = DataValidator.filter_valid_daily_bars(bars)

        # At least one bar should survive validation
        assert len(valid_bars) > 0, (
            f"All {len(bars)} bars for {code} were rejected by the validator"
        )

        # Insert valid bars to DB
        real_db.upsert_daily_bars(valid_bars)

        # Read back — sort dates for comparison
        sorted_bars = sorted(valid_bars, key=lambda r: r["trade_date"])
        first_date = sorted_bars[0]["trade_date"]
        last_date = sorted_bars[-1]["trade_date"]

        db_rows = real_db.get_daily_bars(ts_code, first_date, last_date)

        # DB should have at least as many rows as valid bars
        assert len(db_rows) >= len(valid_bars), (
            f"DB has {len(db_rows)} rows, but we inserted {len(valid_bars)} valid bars"
        )

        # Verify close prices match between inserted and read-back data
        inserted_map = {row["trade_date"]: row["close"] for row in valid_bars}
        for db_row in db_rows:
            date = db_row["trade_date"]
            if date in inserted_map:
                assert db_row["close"] == inserted_map[date], (
                    f"Close mismatch for {date}: "
                    f"inserted {inserted_map[date]}, DB has {db_row['close']}"
                )
