"""
TradeCalendar -- A-share trading calendar management for quant_strategy v2.0

Wraps SQLiteManager calendar methods, provides AKShare data loading,
and falls back to simple weekday logic when no database or no calendar data.
"""

import os
import sys
from datetime import datetime, timedelta

# Allow the module to work both when imported and when run directly
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


class TradeCalendar:
    """A-share trading calendar manager.

    Wraps the SQLiteManager trade_calendar methods with:
      - AKShare data loading (load_from_akshare, sync_to_db)
      - Fallback to weekday logic when no DB or no calendar data
    """

    def __init__(self, db=None):
        """Initialise the calendar manager.

        Args:
            db: Optional SQLiteManager instance.  If None, calendar
                queries fall back to simple weekday logic.
        """
        self._db = db

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @staticmethod
    def load_from_akshare() -> list[dict]:
        """Fetch the trading calendar from AKShare (sina source).

        Returns:
            List of dicts with keys ``trade_date`` (str, YYYYMMDD) and
            ``is_open`` (int, 1 for open).
        """
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        rows = []
        for _, row in df.iterrows():
            trade_date_raw = str(row["trade_date"])
            # Normalise to YYYYMMDD -- handles both 'YYYY-MM-DD' and 'YYYYMMDD'
            trade_date = trade_date_raw.replace("-", "")
            rows.append({
                "trade_date": trade_date,
                "is_open": 1,
            })
        return rows

    def sync_to_db(self):
        """Fetch calendar from AKShare, build pre/next links, and persist.

        Loads all trade dates, sorts them, computes ``pre_trade_date`` and
        ``next_trade_date`` for each, then writes the batch via
        ``db.upsert_calendar()``.

        If ``self._db`` is None this is a no-op (the data has nowhere to go).
        """
        if self._db is None:
            return

        rows = self.load_from_akshare()
        if not rows:
            return

        # Sort by trade_date ascending
        rows.sort(key=lambda r: r["trade_date"])

        # Build pre / next trade date relationships
        count = len(rows)
        for i, row in enumerate(rows):
            row["pre_trade_date"] = rows[i - 1]["trade_date"] if i > 0 else None
            row["next_trade_date"] = rows[i + 1]["trade_date"] if i < count - 1 else None

        self._db.upsert_calendar(rows)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_calendar_data(self) -> bool:
        """Return True if the trade_calendar table has at least one row."""
        if self._db is None:
            return False
        return self._db.calendar_row_count() > 0

    @staticmethod
    def _is_weekday(date_str: str) -> bool:
        """Return True if *date_str* (YYYYMMDD) falls on Mon-Fri."""
        dt = datetime.strptime(date_str, "%Y%m%d")
        return dt.weekday() < 5  # Monday=0 … Friday=4

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def is_trade_day(self, date_str: str) -> bool:
        """Check if *date_str* (YYYYMMDD) is a trading day.

        If a populated database is available, delegates to
        ``db.is_trade_day()``.  When the date is not in the calendar table
        at all (e.g. outside the loaded range), falls back to a weekday
        check -- consistent with ``get_prev_trade_date`` /
        ``get_next_trade_date``.  If there is no DB, always falls back to
        a simple weekday check (Mon-Fri).
        """
        if self._has_calendar_data():
            result = self._db.is_trade_day(date_str)
            if not result and not self._db.date_in_calendar(date_str):
                # Date not in the calendar table -- fall back to weekday logic
                return self._is_weekday(date_str)
            return result
        return self._is_weekday(date_str)

    def get_trade_dates(self, start: str, end: str) -> list[str]:
        """Return all trading days between *start* and *end* (inclusive).

        If a populated database is available, queries ``trade_calendar``
        for open dates in the range.  Falls back to generating all
        weekdays when there is no DB or the DB has no calendar data yet
        (new install).
        """
        if self._db is not None and self._has_calendar_data():
            dates = self._db.get_calendar_dates(start, end)
            if dates:
                return dates
        # DB not available / empty -- fall back to weekdays
        return self._fallback_trade_dates(start, end)

    def _fallback_trade_dates(self, start: str, end: str) -> list[str]:
        """Generate all weekdays between *start* and *end* (inclusive)."""
        dt_start = datetime.strptime(start, "%Y%m%d")
        dt_end = datetime.strptime(end, "%Y%m%d")
        result = []
        current = dt_start
        while current <= dt_end:
            if current.weekday() < 5:
                result.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return result

    def get_prev_trade_date(self, date_str: str) -> str | None:
        """Return the trading day immediately before *date_str*, or None.

        If a populated database is available, delegates to
        ``db.get_prev_trade_date()``.  Otherwise walks backwards from
        *date_str* to find the previous weekday.
        """
        if self._db is not None and self._has_calendar_data():
            result = self._db.get_prev_trade_date(date_str)
            if result is not None:
                return result
        return self._fallback_prev_trade_date(date_str)

    def _fallback_prev_trade_date(self, date_str: str) -> str | None:
        """Walk backwards from *date_str* to find the previous weekday."""
        dt = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=1)
        while dt.weekday() >= 5:  # skip Saturday / Sunday
            dt -= timedelta(days=1)
        return dt.strftime("%Y%m%d")

    def get_next_trade_date(self, date_str: str) -> str | None:
        """Return the trading day immediately after *date_str*, or None.

        If a populated database is available, delegates to
        ``db.get_next_trade_date()``.  Otherwise walks forwards from
        *date_str* to find the next weekday.
        """
        if self._db is not None and self._has_calendar_data():
            result = self._db.get_next_trade_date(date_str)
            if result is not None:
                return result
        return self._fallback_next_trade_date(date_str)

    def _fallback_next_trade_date(self, date_str: str) -> str | None:
        """Walk forwards from *date_str* to find the next weekday."""
        dt = datetime.strptime(date_str, "%Y%m%d") + timedelta(days=1)
        while dt.weekday() >= 5:  # skip Saturday / Sunday
            dt += timedelta(days=1)
        return dt.strftime("%Y%m%d")

    @staticmethod
    def today() -> str:
        """Return today's date in ``YYYYMMDD`` format."""
        return datetime.now().strftime("%Y%m%d")


# ======================================================================
# Verification script
# ======================================================================
if __name__ == "__main__":
    import tempfile

    from data.database import SQLiteManager

    print("=" * 60)
    print("TradeCalendar -- verification")
    print("=" * 60)

    # ---- 1. Calendar WITHOUT a database (pure weekday fallback) ----
    cal = TradeCalendar(db=None)
    print("\n[1] No-DB mode")

    today_str = cal.today()
    print(f"    today() = {today_str}")

    # is_trade_day: only weekdays are true
    # Pick a known Monday and Saturday to verify
    # 2026-06-08 is a Monday, 2026-06-13 is a Saturday
    assert cal.is_trade_day("20260608") is True,   "Monday should be trade day"
    assert cal.is_trade_day("20260613") is False,  "Saturday should NOT be trade day"
    print("    is_trade_day(Mon 20260608) = True   OK")
    print("    is_trade_day(Sat 20260613) = False  OK")

    # get_trade_dates: week-long range
    dates = cal.get_trade_dates("20260608", "20260614")
    # Mon-Fri expected (5 days)
    assert len(dates) == 5, f"Expected 5 weekdays, got {len(dates)}: {dates}"
    assert dates[0] == "20260608"
    assert dates[-1] == "20260612"
    print(f"    get_trade_dates(20260608, 20260614) -> {len(dates)} days  OK")

    # get_prev_trade_date / get_next_trade_date
    prev = cal.get_prev_trade_date("20260610")  # Wed
    nxt = cal.get_next_trade_date("20260610")
    assert prev == "20260609", f"Expected 20260609, got {prev}"
    assert nxt == "20260611", f"Expected 20260611, got {nxt}"
    print(f"    get_prev_trade_date(20260610) = {prev}  OK")
    print(f"    get_next_trade_date(20260610) = {nxt}  OK")

    # Edge: Monday -> prev is Friday
    prev_mon = cal.get_prev_trade_date("20260608")
    assert prev_mon == "20260605", f"Expected 20260605 (Fri), got {prev_mon}"
    print(f"    get_prev_trade_date(Mon 20260608) = {prev_mon} (Fri)  OK")

    # Edge: Friday -> next is Monday
    nxt_fri = cal.get_next_trade_date("20260612")
    assert nxt_fri == "20260615", f"Expected 20260615 (Mon), got {nxt_fri}"
    print(f"    get_next_trade_date(Fri 20260612) = {nxt_fri} (Mon)  OK")

    # ---- 2. Calendar WITH a database ----
    print("\n[2] DB mode")

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        db = SQLiteManager(db_path=tmp_path)
        cal_db = TradeCalendar(db=db)

        # 2a. Empty DB: should fall back to weekday logic
        print("    [a] Empty DB fallback")
        assert cal_db.is_trade_day("20260608") is True   # Monday, fallback
        assert cal_db.is_trade_day("20260613") is False  # Saturday, fallback
        print("        is_trade_day fallback: OK")

        dates_fb = cal_db.get_trade_dates("20260608", "20260614")
        assert len(dates_fb) == 5
        print(f"        get_trade_dates fallback: {len(dates_fb)} days  OK")

        # 2b. Insert a few calendar rows to simulate partial data
        print("    [b] With calendar data")
        db.upsert_calendar([
            {"trade_date": "20260608", "is_open": 1,
             "pre_trade_date": "20260605", "next_trade_date": "20260609"},
            {"trade_date": "20260609", "is_open": 1,
             "pre_trade_date": "20260608", "next_trade_date": "20260610"},
            {"trade_date": "20260610", "is_open": 1,
             "pre_trade_date": "20260609", "next_trade_date": "20260611"},
            {"trade_date": "20260611", "is_open": 1,
             "pre_trade_date": "20260610", "next_trade_date": "20260612"},
            {"trade_date": "20260612", "is_open": 0,   # holiday / closed
             "pre_trade_date": "20260611", "next_trade_date": "20260615"},
        ])

        # is_trade_day via DB
        assert cal_db.is_trade_day("20260608") is True
        assert cal_db.is_trade_day("20260612") is False  # is_open=0
        assert cal_db.is_trade_day("20260613") is False  # not in DB, no fallback
        print("        is_trade_day via DB: OK")

        # get_trade_dates via DB: only open days
        dates_db = cal_db.get_trade_dates("20260608", "20260614")
        assert len(dates_db) == 4           # 5 rows, but 20260612 is closed
        assert "20260612" not in dates_db
        print(f"        get_trade_dates via DB: {len(dates_db)} open days  OK")

        # prev / next from pre-built relationships
        prev_db = cal_db.get_prev_trade_date("20260610")
        nxt_db = cal_db.get_next_trade_date("20260610")
        assert prev_db == "20260609", f"Expected 20260609, got {prev_db}"
        assert nxt_db == "20260611", f"Expected 20260611, got {nxt_db}"
        print(f"        get_prev_trade_date(20260610) = {prev_db}  OK")
        print(f"        get_next_trade_date(20260610) = {nxt_db}  OK")

        # 2c. Date NOT in DB -- fallback to weekday walk
        print("    [c] Missing date fallback to weekday walk")
        prev_miss = cal_db.get_prev_trade_date("20260614")  # Sunday, not in DB
        # db returns None, fallback walks to Friday 20260612
        assert prev_miss == "20260612", f"Expected 20260612 (Fri), got {prev_miss}"
        print(f"        get_prev_trade_date(Sun 20260614) -> {prev_miss}  OK")

        db.close()
    finally:
        os.unlink(tmp_path)

    # ---- 3. AKShare loading (optional, may fail offline) ----
    print("\n[3] AKShare loading")
    try:
        rows = TradeCalendar.load_from_akshare()
        print(f"    Loaded {len(rows)} trade dates from akshare")
        if rows:
            sample = rows[0]
            print(f"    Sample: trade_date={sample['trade_date']}, "
                  f"is_open={sample['is_open']}")
            # Verify structure
            assert "trade_date" in sample
            assert "is_open" in sample
            assert len(sample["trade_date"]) == 8  # YYYYMMDD
            print("    Structure check: OK")
    except Exception as e:
        print(f"    SKIPPED (akshare not available or network error): {e}")

    # ---- Final summary ----
    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
