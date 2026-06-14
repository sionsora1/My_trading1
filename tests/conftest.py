"""
tests/conftest.py — Global pytest fixtures for quant_strategy

Uses real AKShare data, cached to system temp directory (cross-session reuse).
All fixtures are session-scoped for efficiency.
"""
import pytest
import os
import json
import sys
import tempfile

# Ensure the project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Data cache in system temp directory for cross-session reuse
CACHE_DIR = os.path.join(tempfile.gettempdir(), "quant_test_cache")


@pytest.fixture(scope="session")
def real_stock_pool():
    """Load stock codes from the live trading pool (81 stocks).

    Falls back to a minimal set of 5 well-known stocks if the pool file
    is missing or empty.
    """
    pool_file = os.path.join(PROJECT_ROOT, "data_cache", "live_stock_pool.json")
    if os.path.exists(pool_file):
        with open(pool_file, "r", encoding="utf-8") as f:
            pool = json.load(f)
        # pool is a list of {"code": "...", "name": "...", "industry": "..."}
        codes = [s["code"] for s in pool if s.get("code")]
        if codes:
            return codes
    # Fallback: minimal diversified set
    return ["600519", "000858", "002415", "300750", "601398"]


@pytest.fixture(scope="session")
def real_db():
    """Session-scoped SQLiteManager pointing at a test database.

    Uses a temp-directory DB so the real quant_strategy.db is never touched.
    """
    from data.database import SQLiteManager

    test_db_path = os.path.join(CACHE_DIR, "test_quant.db")
    os.makedirs(CACHE_DIR, exist_ok=True)
    db = SQLiteManager(db_path=test_db_path)
    yield db
    db.close()


@pytest.fixture(scope="session")
def cached_market_data(real_stock_pool):
    """Session-scoped real market data, keyed by date string (YYYYMMDD).

    On first access: fetches ~1 year of daily data from AKShare via
    DataFetcher.build_market_data_by_date, then caches to a JSON file.
    Subsequent accesses within the same day hit the cache.
    Uses a smaller subset (max 10 stocks) for test data to avoid timeouts.
    """
    from data.fetcher import DataFetcher
    from datetime import datetime

    # Use a small subset for test data to keep fetch times reasonable
    test_pool = real_stock_pool[:10]

    today = datetime.now().strftime("%Y%m%d")
    cache_file = os.path.join(
        CACHE_DIR,
        f"test_market_{today}_{len(test_pool)}stocks.json",
    )

    # Cache hit (same calendar day)
    if os.path.exists(cache_file):
        with open(cache_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data and len(data) > 10:
            return data

    # Cache miss — fetch from AKShare (with timeout protection)
    print(f"\n[Fixture] Fetching market data from AKShare "
          f"({len(test_pool)} stocks)...")
    try:
        fetcher = DataFetcher()
        start = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y%m%d")
        market_data = fetcher.build_market_data_by_date(
            test_pool, start, today
        )

        if market_data and len(market_data) > 10:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(market_data, f, ensure_ascii=False, indent=2)
            print(f"[Fixture] Cached to {cache_file} "
                  f"({len(market_data)} trading days)")
        return market_data if market_data else {}
    except Exception as e:
        print(f"[Fixture] Market data fetch failed: {e}")
        print(f"[Fixture] Tests requiring cached_market_data will skip")
        return {}


@pytest.fixture(scope="session")
def cached_fundamentals(real_stock_pool, real_db):
    """Session-scoped real fundamental data, upserted into real_db.

    Checks whether the database already has fundamentals for the first
    stock; if so, skips the fetch.  Otherwise pulls financial data from
    AKShare via DataFetcher.get_financial_data and upserts it.
    """
    from data.fetcher import DataFetcher

    # Quick check: does the DB already have data?
    sample_code = real_stock_pool[0]
    existing = real_db.get_financial_for_date(sample_code, "20250930")
    if existing:
        return  # already populated

    print(f"\n[Fixture] Fetching financial data from AKShare...")
    fetcher = DataFetcher()
    for code in real_stock_pool:
        try:
            # get_financial_data returns a dict keyed by metric name
            fin = fetcher.get_financial_data(code)
            if not fin or fin.get("revenue", 0) == 0:
                continue

            # Map to the fundamentals table schema.
            # We use today's date as report_date placeholder since the
            # AKShare financial_abstract endpoint returns the latest
            # available quarter without an explicit date field.
            from datetime import datetime
            today_str = datetime.now().strftime("%Y%m%d")
            mapped = [{
                "ts_code": code,
                "report_date": today_str,
                "roe": fin.get("roe", 0) or 0,
                "gross_margin": fin.get("gross_margin", 0) or 0,
                "revenue": fin.get("revenue", 0) or 0,
                "net_profit": fin.get("net_profit", 0) or 0,
                "ocf": fin.get("ocf", 0) or 0,
                "net_assets": fin.get("net_assets", 0) or 0,
                "revenue_growth": fin.get("revenue_growth", 0) or 0,
                "profit_growth": fin.get("profit_growth", 0) or 0,
                "accrual_ratio": fin.get("accrual_ratio", 0) or 0,
            }]
            real_db.upsert_fundamentals(mapped)
        except Exception as e:
            print(f"  [Fixture] {code} financial data failed: {e}")


@pytest.fixture(scope="session")
def cached_calendar(real_db):
    """Session-scoped trade calendar, synced to real_db once.

    If the trade_calendar table already has data this fixture is a no-op.
    """
    from data.calendar import TradeCalendar

    row_count = real_db.calendar_row_count()
    if row_count > 0:
        return

    print(f"\n[Fixture] Syncing trade calendar from AKShare...")
    cal = TradeCalendar(real_db)
    try:
        cal.sync_to_db()
    except Exception as e:
        print(f"  [Fixture] Calendar sync failed (network may be down): {e}")
