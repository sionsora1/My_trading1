"""
DataValidator — data quality checks for OHLC bars and fundamentals.

Every validation method returns (is_valid: bool, reason: str).
The reason is empty when is_valid is True, otherwise describes why the
row failed validation.
"""

from typing import Tuple, List
import math


class DataValidator:
    """Static validation and filtering utilities for quant data."""

    # ------------------------------------------------------------------
    # Daily bar validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_daily_bar(row: dict) -> Tuple[bool, str]:
        """Validate a single daily bar row.

        Checks (in order):
          1. OHLC not all zero
          2. pct_chg within [-11, 11]  (A-share limit is +/-10%, 11 allows
             for new-issue / post-restructuring outliers)
          3. volume == 0 AND pct_chg == 0 => suspended stock → reject
          4. high >= low
          5. close within [low * 0.98,  high * 1.02]  (tight sanity band)

        Returns:
            (True, "") if valid, otherwise (False, "<reason>").
        """
        # Extract values safely; treat missing keys as None
        o = row.get("open")
        h = row.get("high")
        l = row.get("low")
        c = row.get("close")
        v = row.get("volume")
        pct = row.get("pct_chg")

        # --- 1. OHLC not all zero ---
        vals = [o, h, l, c]
        if all(v is None or (isinstance(v, float) and math.isclose(v, 0.0, abs_tol=1e-9))
               for v in vals):
            return False, "OHLC all zero"

        # Helper: safe float with NaN guard
        def _f(v):
            if v is None:
                return None
            try:
                fv = float(v)
                if math.isnan(fv) or math.isinf(fv):
                    return None
                return fv
            except (ValueError, TypeError):
                return None

        o, h, l, c = _f(o), _f(h), _f(l), _f(c)
        v = _f(v)
        pct = _f(pct)

        # --- 2. pct_chg within [-11, 11] ---
        if pct is not None and (pct < -11.0 or pct > 11.0):
            return False, f"pct_chg out of range: {pct:.2f}"

        # --- 3. suspended stock detection ---
        if v is not None and v == 0 and pct is not None and pct == 0:
            return False, "suspected suspended stock (vol=0, pct_chg=0)"

        # --- 4. high >= low ---
        if h is not None and l is not None and h < l:
            return False, f"high ({h}) < low ({l})"

        # --- 5. close within [low * 0.98, high * 1.02] ---
        if l is not None and h is not None and c is not None:
            lower_bound = l * 0.98
            upper_bound = h * 1.02
            if c < lower_bound:
                return False, f"close ({c}) below low*0.98 ({lower_bound:.4f})"
            if c > upper_bound:
                return False, f"close ({c}) above high*1.02 ({upper_bound:.4f})"

        return True, ""

    # ------------------------------------------------------------------
    # Minute bar validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_minute_bar(row: dict) -> Tuple[bool, str]:
        """Validate a single minute bar row.

        Checks:
          1. Price values (open, high, low, close) not all zero
          2. high >= low

        Returns:
            (True, "") if valid, otherwise (False, "<reason>").
        """
        def _f(v):
            if v is None:
                return None
            try:
                fv = float(v)
                if math.isnan(fv) or math.isinf(fv):
                    return None
                return fv
            except (ValueError, TypeError):
                return None

        o = _f(row.get("open"))
        h = _f(row.get("high"))
        l = _f(row.get("low"))
        c = _f(row.get("close"))

        # 1. Price values not all zero
        prices = [o, h, l, c]
        if all(p is None or math.isclose(p, 0.0, abs_tol=1e-9) for p in prices):
            return False, "price values all zero"

        # 2. high >= low
        if h is not None and l is not None and h < l:
            return False, f"high ({h}) < low ({l})"

        return True, ""

    # ------------------------------------------------------------------
    # Fundamental validation
    # ------------------------------------------------------------------

    @staticmethod
    def validate_fundamental(row: dict) -> Tuple[bool, str]:
        """Validate a single fundamentals row.

        Checks:
          1. abs(roe) < 2.0   (i.e. ROE between -200 % and +200 %)
          2. gross_margin between -1 and 1  (i.e. -100 % to +100 %)

        Returns:
            (True, "") if valid, otherwise (False, "<reason>").
        """
        def _f(v):
            if v is None:
                return None
            try:
                fv = float(v)
                if math.isnan(fv) or math.isinf(fv):
                    return None
                return fv
            except (ValueError, TypeError):
                return None

        roe = _f(row.get("roe"))
        gm = _f(row.get("gross_margin"))

        # 1. abs(roe) < 2.0
        if roe is not None and abs(roe) >= 2.0:
            return False, f"abs(roe) >= 2.0: {roe:.4f}"

        # 2. gross_margin between -1 and 1
        if gm is not None and (gm < -1.0 or gm > 1.0):
            return False, f"gross_margin out of [-1, 1]: {gm:.4f}"

        return True, ""

    # ------------------------------------------------------------------
    # Batch helpers
    # ------------------------------------------------------------------

    @staticmethod
    def filter_valid_daily_bars(rows: List[dict]) -> List[dict]:
        """Filter a list of daily bar rows, keeping only valid ones.

        Prints the reject count to stdout.

        Returns:
            A new list containing only the valid rows.
        """
        valid: List[dict] = []
        rejected = 0

        for row in rows:
            ok, reason = DataValidator.validate_daily_bar(row)
            if ok:
                valid.append(row)
            else:
                rejected += 1

        if rejected > 0:
            print(f"[DataValidator] filter_valid_daily_bars: "
                  f"{rejected} / {len(rows)} rows rejected")
        return valid

    @staticmethod
    def deduplicate(rows: List[dict], key_fields: List[str]) -> List[dict]:
        """Deduplicate a list of dicts by *key_fields*, keeping the **last**
        occurrence of each duplicate key.

        Example:
            rows = [
                {"ts_code": "A", "date": "2025-01-01", "close": 10.0},
                {"ts_code": "A", "date": "2025-01-01", "close": 10.5},
            ]
            deduplicate(rows, ["ts_code", "date"])
            # => [{"ts_code": "A", "date": "2025-01-01", "close": 10.5}]

        Returns:
            A new list with duplicates removed (stable last-wins order).
        """
        seen: dict = {}
        # Build key -> index map; later rows overwrite earlier ones (last-wins)
        for i, row in enumerate(rows):
            key = tuple(row.get(k) for k in key_fields)
            seen[key] = i

        # Reconstruct list in the original relative order of kept rows
        kept_indices = sorted(seen.values())
        return [rows[i] for i in kept_indices]


# ======================================================================
# Quick manual verification (run: python data/validator.py)
# ======================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("DataValidator — verification")
    print("=" * 60)

    # --- 1. validate_daily_bar -------------------------------------------------
    print("\n[1] validate_daily_bar")
    good = {
        "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
        "volume": 1_000_000, "pct_chg": 2.0,
    }
    ok, reason = DataValidator.validate_daily_bar(good)
    assert ok, f"Expected valid, got: {reason}"
    print(f"    valid row: OK")

    # OHLC all zero
    zero_row = {
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0,
        "volume": 0, "pct_chg": 0.0,
    }
    ok, reason = DataValidator.validate_daily_bar(zero_row)
    assert not ok, "Expected invalid for all-zero OHLC"
    print(f"    all-zero OHLC rejected: {reason}")

    # pct_chg out of range
    pct_bad = {
        "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0,
        "volume": 1000, "pct_chg": 15.0,
    }
    ok, reason = DataValidator.validate_daily_bar(pct_bad)
    assert not ok, "Expected invalid for pct_chg > 11"
    print(f"    pct_chg=15 rejected: {reason}")

    # suspended stock
    suspended = {
        "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
        "volume": 0, "pct_chg": 0.0,
    }
    ok, reason = DataValidator.validate_daily_bar(suspended)
    assert not ok, "Expected invalid for suspended"
    print(f"    suspended stock rejected: {reason}")

    # high < low
    hl_bad = {
        "open": 10.0, "high": 9.5, "low": 10.0, "close": 9.8,
        "volume": 1000, "pct_chg": -2.0,
    }
    ok, reason = DataValidator.validate_daily_bar(hl_bad)
    assert not ok, "Expected invalid for high < low"
    print(f"    high<low rejected: {reason}")

    # close out of band
    close_bad = {
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 9.0,
        "volume": 1000, "pct_chg": -5.0,
    }
    ok, reason = DataValidator.validate_daily_bar(close_bad)
    assert not ok, f"Expected invalid for close < low*0.98 ({9.5*0.98:.2f}), got valid"
    print(f"    close={close_bad['close']} < low*0.98={9.5*0.98:.2f} rejected: {reason}")

    # --- 2. validate_minute_bar ------------------------------------------------
    print("\n[2] validate_minute_bar")
    mgood = {"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 500}
    ok, reason = DataValidator.validate_minute_bar(mgood)
    assert ok, f"Expected valid, got: {reason}"
    print(f"    valid minute bar: OK")

    mzero = {"open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0}
    ok, reason = DataValidator.validate_minute_bar(mzero)
    assert not ok, f"Expected invalid, got valid"
    print(f"    all-zero minute rejected: {reason}")

    mhl = {"open": 10.0, "high": 9.8, "low": 10.1, "close": 10.0, "volume": 500}
    ok, reason = DataValidator.validate_minute_bar(mhl)
    assert not ok, f"Expected invalid for high<low, got valid"
    print(f"    high<low minute rejected: {reason}")

    # --- 3. validate_fundamental -----------------------------------------------
    print("\n[3] validate_fundamental")
    fgood = {"roe": 0.15, "gross_margin": 0.40}
    ok, reason = DataValidator.validate_fundamental(fgood)
    assert ok, f"Expected valid, got: {reason}"
    print(f"    valid fundamental: OK")

    froe_bad = {"roe": 3.0, "gross_margin": 0.40}
    ok, reason = DataValidator.validate_fundamental(froe_bad)
    assert not ok, f"Expected invalid for roe=3.0"
    print(f"    roe=3.0 rejected: {reason}")

    fgm_bad = {"roe": 0.10, "gross_margin": 1.5}
    ok, reason = DataValidator.validate_fundamental(fgm_bad)
    assert not ok, f"Expected invalid for gm=1.5"
    print(f"    gross_margin=1.5 rejected: {reason}")

    # --- 4. filter_valid_daily_bars --------------------------------------------
    print("\n[4] filter_valid_daily_bars")
    rows = [good, zero_row, pct_bad, suspended, hl_bad, close_bad]
    filtered = DataValidator.filter_valid_daily_bars(rows)
    assert len(filtered) == 1, f"Expected 1 valid row, got {len(filtered)}"
    print(f"    {len(filtered)} valid out of {len(rows)} rows")

    # --- 5. deduplicate --------------------------------------------------------
    print("\n[5] deduplicate")
    dup_rows = [
        {"ts_code": "A", "date": "2025-01-01", "close": 10.0},
        {"ts_code": "A", "date": "2025-01-01", "close": 10.5},
        {"ts_code": "B", "date": "2025-01-01", "close": 20.0},
        {"ts_code": "A", "date": "2025-01-02", "close": 11.0},
    ]
    deduped = DataValidator.deduplicate(dup_rows, ["ts_code", "date"])
    assert len(deduped) == 3, f"Expected 3 unique rows, got {len(deduped)}"
    # Check last-wins: the row for A on 2025-01-01 should have close=10.5
    a_row = [r for r in deduped if r["ts_code"] == "A" and r["date"] == "2025-01-01"]
    assert len(a_row) == 1 and a_row[0]["close"] == 10.5, \
        f"Expected close=10.5 (last-wins), got {a_row}"
    print(f"    {len(deduped)} unique rows (last-wins confirmed: close=10.5)")

    # --- Final summary ---
    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
