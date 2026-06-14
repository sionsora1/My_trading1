"""
tests/unit/test_database.py — Unit tests for SQLiteManager CRUD operations.

Covers:
  TC-DB01: All 7 tables must exist after __init__
  TC-DB02: upsert_daily_bars inserts new data
  TC-DB03: upsert_daily_bars updates existing data
  TC-DB04: get_daily_bars returns correct date range
  TC-DB05: get_financial_for_date prevents look-ahead bias
  TC-DB06: md5_hash is deterministic
"""
import pytest


class TestSQLiteManager:
    """Unit tests for SQLiteManager CRUD operations."""

    # ------------------------------------------------------------------
    # TC-DB01: All 7 tables must exist
    # ------------------------------------------------------------------

    def test_all_tables_exist(self, real_db):
        """Verify that after __init__, all 7 expected tables exist."""
        tables = real_db.list_tables()
        expected = [
            "daily_bars",
            "minute_bars",
            "fundamentals",
            "trade_calendar",
            "stock_info",
            "data_log",
            "account_snapshot",
        ]
        for table in expected:
            assert table in tables, (
                f"Table '{table}' not found in database. "
                f"Existing tables: {tables}"
            )

    # ------------------------------------------------------------------
    # TC-DB02: upsert_daily_bars inserts new data
    # ------------------------------------------------------------------

    def test_upsert_daily_bars_insert(self, real_db, real_stock_pool):
        """Insert a row with real-looking data and verify it is stored."""
        code = real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        test_row = {
            "ts_code": ts_code,
            "trade_date": "1999-01-04",
            "open": 10.5,
            "high": 11.2,
            "low": 10.3,
            "close": 10.8,
            "volume": 5000000.0,
            "amount": 54000000.0,
            "turnover": 0.03,
            "pct_chg": 1.5,
        }
        real_db.upsert_daily_bars([test_row])

        # Read back and assert all fields
        rows = real_db.get_daily_bars(ts_code, "1999-01-04", "1999-01-04")
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        row = rows[0]
        assert row["ts_code"] == ts_code
        assert row["trade_date"] == "1999-01-04"
        assert row["open"] == 10.5
        assert row["high"] == 11.2
        assert row["low"] == 10.3
        assert row["close"] == 10.8
        assert row["volume"] == 5000000.0
        assert row["amount"] == 54000000.0
        assert row["turnover"] == 0.03
        assert row["pct_chg"] == 1.5

    # ------------------------------------------------------------------
    # TC-DB03: upsert_daily_bars updates existing data
    # ------------------------------------------------------------------

    def test_upsert_daily_bars_update(self, real_db, real_stock_pool):
        """Insert data for same (ts_code, trade_date) with different values;
        verify the update overwrites correctly."""
        code = real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        trade_date = "1999-01-05"

        # First insert — original values
        row_v1 = {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "open": 20.0,
            "high": 21.0,
            "low": 19.5,
            "close": 20.5,
            "volume": 1000000.0,
            "amount": 20500000.0,
            "turnover": 0.01,
            "pct_chg": -0.5,
        }
        real_db.upsert_daily_bars([row_v1])

        # Insert a second time with different values for the same key
        row_v2 = {
            "ts_code": ts_code,
            "trade_date": trade_date,
            "open": 22.0,
            "high": 23.0,
            "low": 21.5,
            "close": 22.5,
            "volume": 2000000.0,
            "amount": 45000000.0,
            "turnover": 0.02,
            "pct_chg": 2.0,
        }
        real_db.upsert_daily_bars([row_v2])

        # Read back — should contain only the updated (v2) values
        rows = real_db.get_daily_bars(ts_code, trade_date, trade_date)
        assert len(rows) == 1, (
            f"Expected exactly 1 row after upsert, got {len(rows)}"
        )
        row = rows[0]
        assert row["close"] == 22.5, (
            f"Expected close=22.5 (updated value), got {row['close']}"
        )
        assert row["open"] == 22.0
        assert row["high"] == 23.0
        assert row["low"] == 21.5
        assert row["volume"] == 2000000.0
        assert row["amount"] == 45000000.0
        assert row["turnover"] == 0.02
        assert row["pct_chg"] == 2.0

    # ------------------------------------------------------------------
    # TC-DB04: get_daily_bars returns correct date range
    # ------------------------------------------------------------------

    def test_get_daily_bars_date_range(self, real_db, real_stock_pool):
        """Verify get_daily_bars returns a list and respects date boundaries."""
        # Use the second stock from the pool to avoid collision with DB02/DB03
        code = real_stock_pool[1] if len(real_stock_pool) > 1 else real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        # Insert 3 consecutive trading days
        dates = ["2000-01-03", "2000-01-04", "2000-01-05"]
        for i, trade_date in enumerate(dates):
            row = {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": 10.0 + i,
                "high": 11.0 + i,
                "low": 9.5 + i,
                "close": 10.5 + i,
                "volume": 1000000.0,
                "amount": 10500000.0,
                "turnover": 0.01,
                "pct_chg": float(i),
            }
            real_db.upsert_daily_bars([row])

        # Query a single day
        rows = real_db.get_daily_bars(ts_code, "2000-01-04", "2000-01-04")
        assert isinstance(rows, list), "Result should be a list"
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        assert rows[0]["trade_date"] == "2000-01-04"

        # Query the full range — all 3 rows
        rows = real_db.get_daily_bars(ts_code, "2000-01-01", "2000-12-31")
        assert len(rows) == 3, f"Expected 3 rows in full range, got {len(rows)}"

        # Query a range before any data — empty list
        rows = real_db.get_daily_bars(ts_code, "1999-01-01", "1999-12-31")
        assert len(rows) == 0, (
            f"Expected 0 rows for date range before data, got {len(rows)}"
        )

    # ------------------------------------------------------------------
    # TC-DB05: get_financial_for_date prevents look-ahead bias
    # ------------------------------------------------------------------

    def test_financial_no_lookahead_bias(self, real_db, real_stock_pool):
        """get_financial_for_date should only return records where
        report_date <= trade_date — never a future report date."""
        code = real_stock_pool[0]
        ts_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"

        # Insert two quarterly financial reports
        fundamentals = [
            {
                "ts_code": ts_code,
                "report_date": "1999-03-31",
                "roe": 0.10,
                "gross_margin": 0.35,
                "revenue": 50e8,
                "net_profit": 5e8,
                "ocf": 3e8,
                "net_assets": 40e8,
                "revenue_growth": 0.08,
                "profit_growth": 0.12,
                "accrual_ratio": 0.02,
            },
            {
                "ts_code": ts_code,
                "report_date": "1999-06-30",
                "roe": 0.12,
                "gross_margin": 0.37,
                "revenue": 55e8,
                "net_profit": 6e8,
                "ocf": 4e8,
                "net_assets": 42e8,
                "revenue_growth": 0.10,
                "profit_growth": 0.15,
                "accrual_ratio": 0.03,
            },
        ]
        real_db.upsert_fundamentals(fundamentals)

        # Trade date 1999-05-15 is between Q1 (1999-03-31) and Q2 (1999-06-30).
        # The query must return Q1, NOT Q2 — this prevents look-ahead bias.
        result = real_db.get_financial_for_date(ts_code, "1999-05-15")
        assert result is not None, (
            "Expected a financial record for trade_date 1999-05-15"
        )
        assert result["report_date"] == "1999-03-31", (
            f"Look-ahead BIAS! Expected report_date=1999-03-31 (Q1), "
            f"got {result['report_date']} (Q2 or later)"
        )
        assert result["roe"] == 0.10, (
            f"Expected Q1 roe=0.10, got {result['roe']}"
        )

    # ------------------------------------------------------------------
    # TC-DB06: md5_hash is deterministic
    # ------------------------------------------------------------------

    def test_md5_hash_deterministic(self, real_db):
        """md5_hash should return the same hash for the same input,
        and a different hash for different input."""
        from data.database import SQLiteManager

        data_a = {"a": 1, "b": 2}
        data_b = {"b": 2, "a": 1}  # same logical content, different key order
        data_c = {"a": 1, "b": 3}  # different value

        h_a = SQLiteManager.md5_hash(data_a)
        h_b = SQLiteManager.md5_hash(data_b)
        h_c = SQLiteManager.md5_hash(data_c)

        # Same logical content must produce the same hash
        # (sorted keys in json.dumps ensure order-independence)
        assert h_a == h_b, (
            f"Same data should produce same hash (sorted keys): "
            f"{h_a} != {h_b}"
        )

        # Hash must be a 32-character hex string
        assert isinstance(h_a, str), "Hash must be a string"
        assert len(h_a) == 32, f"Hash must be 32 hex chars, got {len(h_a)}"

        # Different content must produce different hashes
        assert h_a != h_c, (
            f"Different data should produce different hashes: "
            f"{h_a} == {h_c}"
        )

        # Strings are also supported
        assert SQLiteManager.md5_hash("hello") == SQLiteManager.md5_hash("hello"), (
            "Same string must produce same hash"
        )
        assert SQLiteManager.md5_hash("hello") != SQLiteManager.md5_hash("world"), (
            "Different strings must produce different hashes"
        )
