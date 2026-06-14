"""
tests/unit/test_calendar.py — Unit tests for TradeCalendar.

TC-C01: Weekend fallback without database
TC-C02: Known trade day query via database
TC-C03: get_trade_dates returns only weekdays
"""

from datetime import datetime

from data.calendar import TradeCalendar


class TestTradeCalendar:
    """Unit tests for TradeCalendar."""

    # ------------------------------------------------------------------
    # TC-C01: Weekend fallback without database
    # ------------------------------------------------------------------
    def test_weekend_fallback_without_database(self):
        """TC-C01: TradeCalendar() with no db falls back to weekday logic.

        Saturday ("20260613") should return False from is_trade_day(),
        Monday ("20260615") should return True.
        """
        cal = TradeCalendar()

        # 2026-06-13 is a Saturday
        assert cal.is_trade_day("20260613") is False, (
            "Saturday (20260613) should not be a trade day"
        )

        # 2026-06-15 is a Monday
        assert cal.is_trade_day("20260615") is True, (
            "Monday (20260615) should be a trade day"
        )

    # ------------------------------------------------------------------
    # TC-C02: Known trade day query via database
    # ------------------------------------------------------------------
    def test_is_trade_day_with_database(self, real_db, cached_calendar):
        """TC-C02: With a populated database, is_trade_day works correctly.

        2026-06-12 is a Friday.  When the calendar has data, either the
        database returns True for this date, or the date is outside the
        loaded range and the method falls back to weekday logic — in
        either case a Friday must be True.
        """
        cal = TradeCalendar(real_db)
        result = cal.is_trade_day("20260612")

        assert result is True, (
            f"2026-06-12 (Friday) should be a trade day, got {result}"
        )

    # ------------------------------------------------------------------
    # TC-C03: get_trade_dates returns only weekdays
    # ------------------------------------------------------------------
    def test_get_trade_dates_only_weekdays(self, real_db, cached_calendar):
        """TC-C03: get_trade_dates returns only Mon-Fri dates in YYYYMMDD
        format — no Saturday or Sunday dates."""
        cal = TradeCalendar(real_db)
        dates = cal.get_trade_dates("20260601", "20260612")

        # Must return at least one trade date
        assert len(dates) > 0, (
            "get_trade_dates should return at least one trade date"
        )

        for d in dates:
            # Format: exactly 8 characters, all digits
            assert len(d) == 8, f"Expected YYYYMMDD format, got '{d}'"
            assert d.isdigit(), f"Date string must be all digits, got '{d}'"

            # Must be a weekday (Mon=0 … Fri=4)
            dt = datetime.strptime(d, "%Y%m%d")
            assert dt.weekday() < 5, (
                f"Weekend date {d} (weekday={dt.weekday()}) "
                f"should not appear in trade dates"
            )
