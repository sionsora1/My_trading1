"""
Technical indicators computation — pure Python, no external dependencies.

All functions operate on lists of values and return corresponding lists.
Used by: web/db_api.py, web/kline_api.py, server.py (kline endpoint)
"""

from typing import Optional


# ═══════════════════════════════════════════════════════════════
# EMA
# ═══════════════════════════════════════════════════════════════

def compute_ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential Moving Average. First *period-1* entries are None."""
    if period <= 0 or not values:
        return [None] * len(values)

    result = [None] * len(values)
    multiplier = 2.0 / (period + 1)

    # Seed with SMA for the first valid value
    first_valid = None
    sma_sum = 0.0
    sma_count = 0
    seed_idx = -1

    for i, v in enumerate(values):
        if v is None:
            continue
        sma_sum += v
        sma_count += 1
        if sma_count == period:
            first_valid = sma_sum / period
            seed_idx = i
            break

    if first_valid is None:
        return result  # not enough data

    result[seed_idx] = first_valid

    for i in range(seed_idx + 1, len(values)):
        v = values[i]
        if v is None:
            result[i] = result[i - 1]
        else:
            result[i] = (v - result[i - 1]) * multiplier + result[i - 1]

    return result


# ═══════════════════════════════════════════════════════════════
# MACD
# ═══════════════════════════════════════════════════════════════

def compute_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list, list, list]:
    """
    Returns (dif, dea, histogram). Each list is same length as input,
    with None for positions where not enough data exists.

    DIF  = EMA(fast) - EMA(slow)
    DEA  = EMA(DIF, signal)
    HIST = 2 * (DIF - DEA)
    """
    n = len(closes)
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    dif = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = ema_fast[i] - ema_slow[i]

    # Compute signal line (DEA) from DIF values
    dea = compute_ema([d if d is not None else 0 for d in dif], signal)
    # Fix DEA: only valid where DIF is valid
    for i in range(n):
        if dif[i] is None:
            dea[i] = None

    hist = [None] * n
    for i in range(n):
        if dif[i] is not None and dea[i] is not None:
            hist[i] = 2.0 * (dif[i] - dea[i])

    return dif, dea, hist


# ═══════════════════════════════════════════════════════════════
# RSI (Wilder's Smoothing)
# ═══════════════════════════════════════════════════════════════

def compute_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """
    Wilder's RSI. Returns list same length as input (None for first *period* bars).

    RSI = 100 - 100 / (1 + avg_gain / avg_loss)
    Uses Wilder's smoothing (EMA-like).
    """
    n = len(closes)
    if n < period + 1:
        return [None] * n

    result = [None] * n
    gains = []
    losses = []

    for i in range(1, n):
        if closes[i] is None or closes[i - 1] is None:
            gains.append(0.0)
            losses.append(0.0)
        else:
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0.0))
            losses.append(max(-delta, 0.0))

    # Initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100.0 - 100.0 / (1.0 + rs)

    # Wilder smoothing
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        idx = i + 1  # offset because gains[0] corresponds to closes[1]
        if avg_loss == 0:
            result[idx] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[idx] = 100.0 - 100.0 / (1.0 + rs)

    return result


# ═══════════════════════════════════════════════════════════════
# Bollinger Bands
# ═══════════════════════════════════════════════════════════════

def compute_bollinger(
    closes: list[float],
    period: int = 20,
    std_mult: float = 2.0,
) -> tuple[list, list, list, list]:
    """
    Returns (upper, mid, lower, width_pct). Each list is same length as input.

    Mid   = MA(period)
    Upper = Mid + std_mult * std
    Lower = Mid - std_mult * std
    Width = (Upper - Lower) / Mid * 100
    """
    n = len(closes)
    upper = [None] * n
    mid = [None] * n
    lower = [None] * n
    width = [None] * n

    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        valid = [v for v in window if v is not None]
        if len(valid) < period:
            continue
        ma = sum(valid) / len(valid)
        variance = sum((v - ma) ** 2 for v in valid) / len(valid)
        std = variance ** 0.5
        mid[i] = ma
        upper[i] = ma + std_mult * std
        lower[i] = ma - std_mult * std
        if ma != 0:
            width[i] = (upper[i] - lower[i]) / ma * 100

    return upper, mid, lower, width


# ═══════════════════════════════════════════════════════════════
# KDJ
# ═══════════════════════════════════════════════════════════════

def compute_kdj(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    n: int = 9,
) -> tuple[list, list, list]:
    """
    Returns (k, d, j). Each list is same length as input.

    RSV = (close - lowest_low_n) / (highest_high_n - lowest_low_n) * 100
    K = 2/3 * prev_K + 1/3 * RSV  (EMA-like smoothing)
    D = 2/3 * prev_D + 1/3 * K
    J = 3*K - 2*D
    """
    length = len(highs)
    k_vals = [None] * length
    d_vals = [None] * length
    j_vals = [None] * length

    prev_k = 50.0
    prev_d = 50.0

    for i in range(n - 1, length):
        window_high = [h for h in highs[i - n + 1 : i + 1] if h is not None]
        window_low = [l for l in lows[i - n + 1 : i + 1] if l is not None]
        close = closes[i]

        if not window_high or not window_low or close is None:
            continue

        highest = max(window_high)
        lowest = min(window_low)

        if highest == lowest:
            rsv = 50.0
        else:
            rsv = (close - lowest) / (highest - lowest) * 100.0

        k = 2.0 / 3.0 * prev_k + 1.0 / 3.0 * rsv
        d = 2.0 / 3.0 * prev_d + 1.0 / 3.0 * k
        j = 3.0 * k - 2.0 * d

        k_vals[i] = round(k, 2)
        d_vals[i] = round(d, 2)
        j_vals[i] = round(j, 2)

        prev_k = k
        prev_d = d

    return k_vals, d_vals, j_vals


# ═══════════════════════════════════════════════════════════════
# Master function — compute all advanced indicators on bars
# ═══════════════════════════════════════════════════════════════

def compute_advanced_indicators(bars: list[dict]) -> list[dict]:
    """
    Compute MACD, RSI, Bollinger, KDJ on a list of bar dicts.

    Each bar dict is expected to have keys: 'close', 'high', 'low'.
    The function mutates bars in-place, adding:
      macd_dif, macd_dea, macd_hist,
      rsi6, rsi12, rsi24,
      boll_upper, boll_mid, boll_lower, boll_width,
      kdj_k, kdj_d, kdj_j

    Returns the same bars list (for chaining).
    """
    if not bars:
        return bars

    closes = [b['close'] for b in bars]
    highs = [b['high'] for b in bars]
    lows = [b['low'] for b in bars]

    # MACD
    dif, dea, hist = compute_macd(closes)
    for i, b in enumerate(bars):
        b['macd_dif'] = round(dif[i], 4) if dif[i] is not None else None
        b['macd_dea'] = round(dea[i], 4) if dea[i] is not None else None
        b['macd_hist'] = round(hist[i], 4) if hist[i] is not None else None

    # RSI (6 / 12 / 24)
    rsi6 = compute_rsi(closes, 6)
    rsi12 = compute_rsi(closes, 12)
    rsi24 = compute_rsi(closes, 24)
    for i, b in enumerate(bars):
        b['rsi6'] = round(rsi6[i], 2) if rsi6[i] is not None else None
        b['rsi12'] = round(rsi12[i], 2) if rsi12[i] is not None else None
        b['rsi24'] = round(rsi24[i], 2) if rsi24[i] is not None else None

    # Bollinger
    upper, mid, lower, width = compute_bollinger(closes)
    for i, b in enumerate(bars):
        b['boll_upper'] = round(upper[i], 2) if upper[i] is not None else None
        b['boll_mid'] = round(mid[i], 2) if mid[i] is not None else None
        b['boll_lower'] = round(lower[i], 2) if lower[i] is not None else None
        b['boll_width'] = round(width[i], 2) if width[i] is not None else None

    # KDJ
    k_vals, d_vals, j_vals = compute_kdj(highs, lows, closes)
    for i, b in enumerate(bars):
        b['kdj_k'] = k_vals[i]
        b['kdj_d'] = d_vals[i]
        b['kdj_j'] = j_vals[i]

    return bars
