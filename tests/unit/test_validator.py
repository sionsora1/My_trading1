"""
tests/unit/test_validator.py — Unit tests for DataValidator.

Test cases:
    TC-V01: Valid daily bar passes validation
    TC-V02: All-zero OHLC is rejected
    TC-V03: Suspended stock (volume=0, pct_chg=0) is rejected
    TC-V04: High < Low inversion is rejected
    TC-V05: pct_chg out of [-11, 11] range is rejected
    TC-V06: filter_valid_daily_bars batch filtering works
"""

from data.validator import DataValidator


class TestDailyBarValidation:
    """Unit tests for DataValidator.validate_daily_bar and
    DataValidator.filter_valid_daily_bars."""

    # ------------------------------------------------------------------
    # TC-V01
    # ------------------------------------------------------------------
    def test_valid_daily_bar_passes(self):
        """TC-V01: A normal OHLC bar with valid data should return (True, "")."""
        row = {
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1_000_000,
            "pct_chg": 2.0,
        }
        ok, reason = DataValidator.validate_daily_bar(row)
        assert ok is True, f"Expected valid bar, got reason: {reason!r}"
        assert reason == "", f"Expected empty reason, got: {reason!r}"

    # ------------------------------------------------------------------
    # TC-V02
    # ------------------------------------------------------------------
    def test_all_zero_ohlc_rejected(self):
        """TC-V02: A bar where all OHLCV and pct_chg are zero should fail."""
        row = {
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0,
            "volume": 0,
            "pct_chg": 0.0,
        }
        ok, reason = DataValidator.validate_daily_bar(row)
        assert ok is False, "Expected invalid for all-zero OHLC"
        assert "OHLC all zero" in reason, (
            f"Expected 'OHLC all zero' reason, got: {reason!r}"
        )

    # ------------------------------------------------------------------
    # TC-V03
    # ------------------------------------------------------------------
    def test_suspended_stock_rejected(self):
        """TC-V03: volume=0 and pct_chg=0 indicates suspension, even with
        non-zero prices."""
        row = {
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.0,
            "volume": 0,
            "pct_chg": 0.0,
        }
        ok, reason = DataValidator.validate_daily_bar(row)
        assert ok is False, "Expected invalid for suspended stock"
        assert "suspended" in reason.lower(), (
            f"Expected suspension reason, got: {reason!r}"
        )

    # ------------------------------------------------------------------
    # TC-V04
    # ------------------------------------------------------------------
    def test_high_less_than_low_rejected(self):
        """TC-V04: When high < low, validation should fail."""
        row = {
            "open": 10.0,
            "high": 9.5,
            "low": 10.0,
            "close": 9.8,
            "volume": 1000,
            "pct_chg": -2.0,
        }
        ok, reason = DataValidator.validate_daily_bar(row)
        assert ok is False, "Expected invalid for high < low"
        assert "high" in reason.lower() and "low" in reason.lower(), (
            f"Expected high < low reason, got: {reason!r}"
        )

    # ------------------------------------------------------------------
    # TC-V05
    # ------------------------------------------------------------------
    def test_pct_chg_out_of_range_rejected(self):
        """TC-V05: pct_chg outside [-11, 11] should be rejected."""
        row = {
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": 1000,
            "pct_chg": 15.0,
        }
        ok, reason = DataValidator.validate_daily_bar(row)
        assert ok is False, f"Expected invalid for pct_chg=15, got valid"
        assert "pct_chg" in reason.lower() or "range" in reason.lower(), (
            f"Expected pct_chg range reason, got: {reason!r}"
        )

    # ------------------------------------------------------------------
    # TC-V06
    # ------------------------------------------------------------------
    def test_filter_valid_daily_bars(self):
        """TC-V06: filter_valid_daily_bars returns only valid rows from a
        mixed list of valid and invalid rows."""
        valid1 = {
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "volume": 1_000_000,
            "pct_chg": 2.0,
        }
        valid2 = {
            "open": 15.0,
            "high": 15.8,
            "low": 14.9,
            "close": 15.3,
            "volume": 500_000,
            "pct_chg": 1.5,
        }
        invalid = {
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.0,
            "volume": 0,
            "pct_chg": 0.0,
        }

        rows = [valid1, invalid, valid2]
        filtered = DataValidator.filter_valid_daily_bars(rows)

        assert len(filtered) == 2, (
            f"Expected 2 valid rows, got {len(filtered)}"
        )
        # Verify the valid rows are the ones we expect (identity check)
        assert valid1 in filtered, "valid1 should be in filtered result"
        assert valid2 in filtered, "valid2 should be in filtered result"
        assert invalid not in filtered, "invalid row should not be in filtered result"
