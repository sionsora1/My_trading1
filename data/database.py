"""
SQLiteManager – local persistence layer for quant_strategy v2.0

Manages all local data in a single SQLite database with WAL journal mode,
foreign keys, and proper indexing. The design eliminates look-ahead bias
in financial data queries.
"""

import sqlite3
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta
from contextlib import contextmanager

# Allow the module to work both when imported and when run directly
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config.settings import (
    DATA_CACHE_DIR,
    DATABASE_PATH,
)


class SQLiteManager:
    """Thread-safe(ish) SQLite manager with WAL mode for local persistence."""

    def __init__(self, db_path: str = None):
        """Initialise the manager, connect, and ensure the schema exists.

        Args:
            db_path: Override path to the SQLite file.  Defaults to the
                     value from config.settings.DATABASE_PATH.
        """
        self._db_path = db_path or DATABASE_PATH

        # Ensure the parent directory exists
        os.makedirs(os.path.dirname(os.path.abspath(self._db_path)), exist_ok=True)

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._create_tables()
        self._create_indexes()

    # ------------------------------------------------------------------
    # Connection helper
    # ------------------------------------------------------------------

    @contextmanager
    def _transaction(self):
        """Context manager that commits on success, rolls back on error."""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self):
        """Create all 7 tables if they do not already exist."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_bars (
                ts_code     TEXT NOT NULL,
                trade_date  TEXT NOT NULL,
                open        REAL,
                high        REAL,
                low         REAL,
                close       REAL,
                volume      REAL,
                amount      REAL,
                turnover    REAL,
                pct_chg     REAL,
                PRIMARY KEY (ts_code, trade_date)
            );

            CREATE TABLE IF NOT EXISTS minute_bars (
                ts_code     TEXT NOT NULL,
                trade_time  TEXT NOT NULL,
                period     INTEGER NOT NULL,
                open        REAL,
                high        REAL,
                low         REAL,
                close       REAL,
                volume      REAL,
                PRIMARY KEY (ts_code, trade_time, period)
            );

            CREATE TABLE IF NOT EXISTS fundamentals (
                ts_code         TEXT NOT NULL,
                report_date     TEXT NOT NULL,
                roe             REAL,
                gross_margin    REAL,
                revenue         REAL,
                net_profit      REAL,
                ocf             REAL,
                net_assets      REAL,
                revenue_growth  REAL,
                profit_growth   REAL,
                accrual_ratio   REAL,
                PRIMARY KEY (ts_code, report_date)
            );

            CREATE TABLE IF NOT EXISTS trade_calendar (
                trade_date       TEXT PRIMARY KEY,
                is_open          INTEGER NOT NULL DEFAULT 1,
                pre_trade_date   TEXT,
                next_trade_date  TEXT
            );

            CREATE TABLE IF NOT EXISTS stock_info (
                ts_code      TEXT PRIMARY KEY,
                name         TEXT,
                industry     TEXT,
                market       TEXT,
                list_date    TEXT,
                delist_date  TEXT,
                pe           REAL,
                pb           REAL,
                market_cap   REAL
            );

            CREATE TABLE IF NOT EXISTS data_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_code     TEXT,
                data_type   TEXT,
                update_time TEXT,
                row_count   INTEGER,
                md5_hash    TEXT,
                status      TEXT
            );

            CREATE TABLE IF NOT EXISTS account_snapshot (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_time  TEXT NOT NULL,
                total_assets   REAL,
                available_cash REAL,
                market_value   REAL,
                positions_json TEXT
            );
            """
        )

    def _create_indexes(self):
        """Create indexes on frequently-queried columns."""
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_bars_date
                ON daily_bars(trade_date);
            CREATE INDEX IF NOT EXISTS idx_daily_bars_ts_date
                ON daily_bars(ts_code, trade_date);
            CREATE INDEX IF NOT EXISTS idx_minute_bars_time
                ON minute_bars(trade_time);
            CREATE INDEX IF NOT EXISTS idx_minute_bars_ts_time_period
                ON minute_bars(ts_code, trade_time, period);
            CREATE INDEX IF NOT EXISTS idx_fundamentals_date
                ON fundamentals(report_date);
            CREATE INDEX IF NOT EXISTS idx_fundamentals_ts_date
                ON fundamentals(ts_code, report_date);
            CREATE INDEX IF NOT EXISTS idx_calendar_date
                ON trade_calendar(trade_date);
            CREATE INDEX IF NOT EXISTS idx_data_log_type
                ON data_log(data_type, ts_code);
            """
        )

    # ------------------------------------------------------------------
    # daily_bars CRUD
    # ------------------------------------------------------------------

    def upsert_daily_bars(self, rows: list[dict]):
        """Insert or replace a batch of daily bar rows.

        Each row dict must contain keys matching the daily_bars columns:
        ts_code, trade_date, open, high, low, close, volume, amount,
        turnover, pct_chg.
        """
        sql = """
            INSERT OR REPLACE INTO daily_bars
                (ts_code, trade_date, open, high, low, close,
                 volume, amount, turnover, pct_chg)
            VALUES
                (:ts_code, :trade_date, :open, :high, :low, :close,
                 :volume, :amount, :turnover, :pct_chg)
        """
        with self._transaction() as conn:
            conn.executemany(sql, rows)

    def get_daily_bars(self, ts_code: str, start_date: str, end_date: str) -> list[dict]:
        """Return daily bars for *ts_code* between *start_date* and *end_date*
        (inclusive), ordered by trade_date ascending.
        """
        sql = """
            SELECT ts_code, trade_date, open, high, low, close,
                   volume, amount, turnover, pct_chg
            FROM daily_bars
            WHERE ts_code = ? AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date ASC
        """
        cur = self._conn.execute(sql, (ts_code, start_date, end_date))
        return [dict(row) for row in cur.fetchall()]

    def get_latest_trade_date(self, ts_code: str) -> str | None:
        """Return the most recent trade_date for *ts_code*, or None."""
        sql = "SELECT MAX(trade_date) FROM daily_bars WHERE ts_code = ?"
        cur = self._conn.execute(sql, (ts_code,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    # ------------------------------------------------------------------
    # minute_bars CRUD
    # ------------------------------------------------------------------

    def upsert_minute_bars(self, rows: list[dict]):
        """Insert or replace a batch of minute bar rows."""
        sql = """
            INSERT OR REPLACE INTO minute_bars
                (ts_code, trade_time, period, open, high, low, close, volume)
            VALUES
                (:ts_code, :trade_time, :period, :open, :high, :low, :close, :volume)
        """
        with self._transaction() as conn:
            conn.executemany(sql, rows)

    def get_minute_bars(self, ts_code: str, start_time: str,
                        end_time: str, period: int) -> list[dict]:
        """Return minute bars in a time window for a given period."""
        sql = """
            SELECT ts_code, trade_time, period, open, high, low, close, volume
            FROM minute_bars
            WHERE ts_code = ? AND period = ?
              AND trade_time >= ? AND trade_time <= ?
            ORDER BY trade_time ASC
        """
        cur = self._conn.execute(sql, (ts_code, period, start_time, end_time))
        return [dict(row) for row in cur.fetchall()]

    def cleanup_old_minute_bars(self, keep_days: int = 5):
        """Delete minute bars older than *keep_days* days from today."""
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        sql = "DELETE FROM minute_bars WHERE trade_time < ?"
        with self._transaction() as conn:
            conn.execute(sql, (cutoff,))

    # ------------------------------------------------------------------
    # fundamentals CRUD — look-ahead-bias free
    # ------------------------------------------------------------------

    def upsert_fundamentals(self, rows: list[dict]):
        """Insert or replace a batch of fundamental data rows."""
        sql = """
            INSERT OR REPLACE INTO fundamentals
                (ts_code, report_date, roe, gross_margin, revenue,
                 net_profit, ocf, net_assets, revenue_growth,
                 profit_growth, accrual_ratio)
            VALUES
                (:ts_code, :report_date, :roe, :gross_margin, :revenue,
                 :net_profit, :ocf, :net_assets, :revenue_growth,
                 :profit_growth, :accrual_ratio)
        """
        with self._transaction() as conn:
            conn.executemany(sql, rows)

    def get_financial_for_date(self, ts_code: str, trade_date: str) -> dict | None:
        """Return the **most recent** fundamentals record whose report_date
        is on or before *trade_date*.

        This is THE critical method for eliminating look-ahead bias:
        we never peek at future financial reports.
        """
        sql = """
            SELECT ts_code, report_date, roe, gross_margin, revenue,
                   net_profit, ocf, net_assets, revenue_growth,
                   profit_growth, accrual_ratio
            FROM fundamentals
            WHERE ts_code = ? AND report_date <= ?
            ORDER BY report_date DESC
            LIMIT 1
        """
        cur = self._conn.execute(sql, (ts_code, trade_date))
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # trade_calendar CRUD
    # ------------------------------------------------------------------

    def upsert_calendar(self, rows: list[dict]):
        """Insert or replace trade calendar rows."""
        sql = """
            INSERT OR REPLACE INTO trade_calendar
                (trade_date, is_open, pre_trade_date, next_trade_date)
            VALUES
                (:trade_date, :is_open, :pre_trade_date, :next_trade_date)
        """
        with self._transaction() as conn:
            conn.executemany(sql, rows)

    def is_trade_day(self, date_str: str) -> bool:
        """Return True if *date_str* is a known trading day."""
        sql = "SELECT is_open FROM trade_calendar WHERE trade_date = ?"
        cur = self._conn.execute(sql, (date_str,))
        row = cur.fetchone()
        return bool(row and row["is_open"])

    def get_prev_trade_date(self, date_str: str) -> str | None:
        """Return the immediately-preceding trade date, or None."""
        sql = """
            SELECT pre_trade_date FROM trade_calendar WHERE trade_date = ?
        """
        cur = self._conn.execute(sql, (date_str,))
        row = cur.fetchone()
        return row["pre_trade_date"] if row else None

    def get_next_trade_date(self, date_str: str) -> str | None:
        """Return the immediately-following trade date, or None."""
        sql = """
            SELECT next_trade_date FROM trade_calendar WHERE trade_date = ?
        """
        cur = self._conn.execute(sql, (date_str,))
        row = cur.fetchone()
        return row["next_trade_date"] if row else None

    def calendar_row_count(self) -> int:
        """Return number of rows in trade_calendar table."""
        row = self._conn.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()
        return row[0] if row else 0

    def date_in_calendar(self, date_str: str) -> bool:
        """Return True if *date_str* has a row in trade_calendar (any is_open value)."""
        sql = "SELECT 1 FROM trade_calendar WHERE trade_date = ?"
        cur = self._conn.execute(sql, (date_str,))
        return cur.fetchone() is not None

    def get_calendar_dates(self, start: str, end: str) -> list[str]:
        """Return open trade dates between *start* and *end* (inclusive).

        Only returns dates where is_open = 1, ordered ascending.
        """
        sql = """
            SELECT trade_date FROM trade_calendar
            WHERE trade_date >= ? AND trade_date <= ?
              AND is_open = 1
            ORDER BY trade_date ASC
        """
        cur = self._conn.execute(sql, (start, end))
        return [row["trade_date"] for row in cur.fetchall()]

    # ------------------------------------------------------------------
    # stock_info CRUD
    # ------------------------------------------------------------------

    def upsert_stock_info(self, rows: list[dict]):
        """Insert or replace stock info rows."""
        sql = """
            INSERT OR REPLACE INTO stock_info
                (ts_code, name, industry, market, list_date, delist_date,
                 pe, pb, market_cap)
            VALUES
                (:ts_code, :name, :industry, :market, :list_date, :delist_date,
                 :pe, :pb, :market_cap)
        """
        with self._transaction() as conn:
            conn.executemany(sql, rows)

    def get_stock_info(self, ts_code: str) -> dict | None:
        """Return stock info for *ts_code*, or None."""
        sql = """
            SELECT ts_code, name, industry, market, list_date, delist_date,
                   pe, pb, market_cap
            FROM stock_info
            WHERE ts_code = ?
        """
        cur = self._conn.execute(sql, (ts_code,))
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # data_log CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def md5_hash(data) -> str:
        """Return the MD5 hex digest of *data*.

        *data* may be a string, bytes, list, or dict.  Dictionaries and
        lists are JSON-serialised with sorted keys for reproducibility.
        """
        if isinstance(data, (dict, list)):
            data = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if isinstance(data, str):
            data = data.encode("utf-8")
        return hashlib.md5(data).hexdigest()

    def log_update(self, ts_code: str, data_type: str, row_count: int,
                   data_hash: str, status: str = "ok"):
        """Insert a data-log entry recording a data refresh."""
        sql = """
            INSERT INTO data_log
                (ts_code, data_type, update_time, row_count, md5_hash, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._transaction() as conn:
            conn.execute(sql, (ts_code, data_type, now, row_count,
                               data_hash, status))

    # ------------------------------------------------------------------
    # account_snapshot CRUD
    # ------------------------------------------------------------------

    def save_account_snapshot(self, total_assets: float,
                              available_cash: float,
                              market_value: float,
                              positions: dict):
        """Persist a snapshot of the account state.

        *positions* is serialised to JSON for storage.
        """
        sql = """
            INSERT INTO account_snapshot
                (snapshot_time, total_assets, available_cash,
                 market_value, positions_json)
            VALUES (?, ?, ?, ?, ?)
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pos_json = json.dumps(positions, ensure_ascii=False)
        with self._transaction() as conn:
            conn.execute(sql, (now, total_assets, available_cash,
                               market_value, pos_json))

    def get_latest_snapshot(self) -> dict | None:
        """Return the most recent account snapshot, or None."""
        sql = """
            SELECT id, snapshot_time, total_assets, available_cash,
                   market_value, positions_json
            FROM account_snapshot
            ORDER BY id DESC
            LIMIT 1
        """
        cur = self._conn.execute(sql)
        row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("positions_json"):
            result["positions"] = json.loads(result.pop("positions_json"))
        return result

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def list_tables(self) -> list[str]:
        """Return the names of all user tables in the database."""
        sql = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        cur = self._conn.execute(sql)
        return [row["name"] for row in cur.fetchall()]

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()


# ======================================================================
# Quick manual verification (run: python data/database.py)
# ======================================================================
if __name__ == "__main__":
    import tempfile

    # Use a temp database so the real one is never clobbered
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        # Monkey-patch so SQLiteManager uses the temp file
        original_path = DATABASE_PATH
        import config.settings as _cfg
        _cfg.DATABASE_PATH = tmp_path

        mgr = SQLiteManager(db_path=tmp_path)

        print("=" * 60)
        print("SQLiteManager – verification")
        print("=" * 60)

        # 1. Tables
        tables = mgr.list_tables()
        print(f"\n[1] Tables created: {len(tables)}")
        for t in tables:
            print(f"    - {t}")
        assert len(tables) >= 7, f"Expected >=7 tables, got {len(tables)}"

        # 2. Insert a daily bar
        mgr.upsert_daily_bars([
            {
                "ts_code": "000001.SZ",
                "trade_date": "2025-06-10",
                "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
                "volume": 1_000_000, "amount": 10_200_000,
                "turnover": 0.05, "pct_chg": 0.02,
            }
        ])
        print("\n[2] Inserted 1 daily bar")

        # 3. Read it back
        rows = mgr.get_daily_bars("000001.SZ", "2025-01-01", "2025-12-31")
        assert len(rows) == 1
        assert rows[0]["close"] == 10.2
        print(f"[3] Read back: close={rows[0]['close']}, ts_code={rows[0]['ts_code']}")

        # 4. get_latest_trade_date
        latest = mgr.get_latest_trade_date("000001.SZ")
        assert latest == "2025-06-10"
        print(f"[4] Latest trade date: {latest}")

        # 5. get_financial_for_date — look-ahead bias test
        mgr.upsert_fundamentals([
            {"ts_code": "000001.SZ", "report_date": "2025-03-31",
             "roe": 0.10, "gross_margin": 0.35, "revenue": 50e8,
             "net_profit": 5e8, "ocf": 3e8, "net_assets": 40e8,
             "revenue_growth": 0.08, "profit_growth": 0.12,
             "accrual_ratio": 0.02},
            {"ts_code": "000001.SZ", "report_date": "2025-06-30",
             "roe": 0.12, "gross_margin": 0.37, "revenue": 55e8,
             "net_profit": 6e8, "ocf": 4e8, "net_assets": 42e8,
             "revenue_growth": 0.10, "profit_growth": 0.15,
             "accrual_ratio": 0.03},
            {"ts_code": "000001.SZ", "report_date": "2025-09-30",
             "roe": 0.14, "gross_margin": 0.39, "revenue": 60e8,
             "net_profit": 7e8, "ocf": 5e8, "net_assets": 44e8,
             "revenue_growth": 0.12, "profit_growth": 0.18,
             "accrual_ratio": 0.01},
        ])

        # Trade date 2025-07-15 -> should see Q2 report (2025-06-30), NOT Q3
        fin = mgr.get_financial_for_date("000001.SZ", "2025-07-15")
        assert fin is not None, "Expected a financial record"
        assert fin["report_date"] == "2025-06-30", \
            f"Look-ahead BIAS! Expected 2025-06-30, got {fin['report_date']}"
        assert fin["roe"] == 0.12
        print(f"[5] get_financial_for_date (2025-07-15) -> "
              f"report_date={fin['report_date']} roe={fin['roe']}  (OK, no look-ahead)")

        # Trade date 2025-03-31 -> should see Q1 report
        fin = mgr.get_financial_for_date("000001.SZ", "2025-03-31")
        assert fin["report_date"] == "2025-03-31"
        print(f"[5b] get_financial_for_date (2025-03-31) -> "
              f"report_date={fin['report_date']}  (OK)")

        # Trade date 2025-01-01 -> no report yet (before Q1)
        fin = mgr.get_financial_for_date("000001.SZ", "2025-01-01")
        assert fin is None, f"Expected None (no report before Q1), got {fin}"
        print(f"[5c] get_financial_for_date (2025-01-01) -> None  (OK, before any report)")

        # 6. Calendar test
        mgr.upsert_calendar([
            {"trade_date": "2025-06-10", "is_open": 1,
             "pre_trade_date": "2025-06-09",
             "next_trade_date": "2025-06-11"},
        ])
        assert mgr.is_trade_day("2025-06-10") is True
        assert mgr.is_trade_day("2025-06-08") is False  # Sunday, not in DB
        assert mgr.get_prev_trade_date("2025-06-10") == "2025-06-09"
        assert mgr.get_next_trade_date("2025-06-10") == "2025-06-11"
        print("[6] Calendar methods: OK")

        # 7. Stock info test
        mgr.upsert_stock_info([{
            "ts_code": "000001.SZ", "name": "平安银行",
            "industry": "银行", "market": "SZ",
            "list_date": "19910403", "delist_date": None,
            "pe": 5.2, "pb": 0.6, "market_cap": 200e8,
        }])
        info = mgr.get_stock_info("000001.SZ")
        assert info and info["name"] == "平安银行"
        print(f"[7] Stock info: {info['name']}, PE={info['pe']}")

        # 8. md5_hash static method
        h = SQLiteManager.md5_hash({"a": 1, "b": 2})
        assert len(h) == 32 and isinstance(h, str)
        # Determinism: same input -> same hash
        assert SQLiteManager.md5_hash({"b": 2, "a": 1}) == h
        print(f"[8] md5_hash: {h[:8]}...  (deterministic OK)")

        # 9. Data log
        mgr.log_update("000001.SZ", "daily", 1, h, "ok")
        print("[9] Data log: OK")

        # 10. Account snapshot
        mgr.save_account_snapshot(100_000.0, 80_000.0, 20_000.0,
                                  {"000001.SZ": {"shares": 1000, "cost": 10.0}})
        snap = mgr.get_latest_snapshot()
        assert snap and snap["total_assets"] == 100_000.0
        assert snap["positions"]["000001.SZ"]["shares"] == 1000
        print(f"[10] Account snapshot: assets={snap['total_assets']}, "
              f"positions={len(snap['positions'])}")

        # 11. Minute bars + cleanup
        mgr.upsert_minute_bars([
            {"ts_code": "000001.SZ", "trade_time": "2025-06-10 09:35:00",
             "period": 5, "open": 10.0, "high": 10.1, "low": 9.99,
             "close": 10.05, "volume": 5000},
        ])
        mb = mgr.get_minute_bars("000001.SZ", "2025-06-10 09:30:00",
                                 "2025-06-10 10:00:00", 5)
        assert len(mb) == 1 and mb[0]["close"] == 10.05
        mgr.cleanup_old_minute_bars(keep_days=9999)  # large window keeps our row
        mb2 = mgr.get_minute_bars("000001.SZ", "2025-06-10 09:30:00",
                                  "2025-06-10 10:00:00", 5)
        assert len(mb2) == 1  # still there
        print("[11] Minute bars + cleanup: OK")

        # --- Final summary ---
        print("\n" + "=" * 60)
        print("ALL CHECKS PASSED")
        print("=" * 60)
    finally:
        mgr.close()
        os.unlink(tmp_path)
