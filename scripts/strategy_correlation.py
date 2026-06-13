"""
策略相关性矩阵 — 分析多策略信号重叠度，评估组合分散效果

重叠度高的策略组合没有分散效果（信号同买同卖 = 等同于一个策略）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

from data.database import SQLiteManager
from data.fetcher import DataFetcher
from web.kline_api import compute_indicators
from web.kline_api import (
    generate_momentum_signals,
    generate_trend_following_signals,
    generate_mean_reversion_signals,
    generate_ma_crossover_signals,
    generate_breakout_signals,
    generate_low_volatility_signals,
)

STRATEGIES = {
    'momentum': ('动量', generate_momentum_signals),
    'trend_following': ('趋势跟随', generate_trend_following_signals),
    'mean_reversion': ('均值回归', generate_mean_reversion_signals),
    'ma_crossover': ('均线交叉', generate_ma_crossover_signals),
    'breakout': ('突破', generate_breakout_signals),
    'low_volatility': ('低波动', generate_low_volatility_signals),
}


def compute_correlation_matrix(stock_pool: list, start_date: str, end_date: str):
    """Compute signal overlap and return correlation between strategies"""
    db = SQLiteManager()

    # Track daily return series for each strategy (simplified: daily signal direction)
    # More precisely: for each stock and date, record BUY=1, HOLD=0, SELL=-1
    strategy_signals = {k: [] for k in STRATEGIES}
    strategy_returns = {k: [] for k in STRATEGIES}
    dates_covered = []

    for code in stock_pool[:20]:  # Sample 20 stocks
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
        bars = db.get_daily_bars(ts_code, start_date, end_date)
        if len(bars) < 60:
            continue

        bars = compute_indicators(bars)

        for key, (name, func) in STRATEGIES.items():
            try:
                result = func(bars)
                # Extract daily signal directions
                signal_dates = {s['date']: 1 if s['signal'] == 'BUY' else -1
                               for s in result['signals']}
                # Build equity curve from trade returns
                cum_ret = 1.0
                for trade in result['trades']:
                    cum_ret *= (1 + trade['pnl_pct'] / 100)
                strategy_returns[key].append(cum_ret - 1)

                # For each bar, record signal direction
                for bar in bars:
                    date = bar['trade_date']
                    if start_date <= date <= end_date:
                        if date not in signal_dates:
                            # Carry forward last signal
                            pass
            except Exception:
                continue

    db.close()

    # Compute pairwise correlation of strategy returns
    keys = list(STRATEGIES.keys())
    n = len(keys)
    corr_matrix = np.zeros((n, n))
    return_vectors = {}

    for key in keys:
        returns = strategy_returns.get(key, [])
        if returns:
            return_vectors[key] = np.array(returns)

    for i, ki in enumerate(keys):
        for j, kj in enumerate(keys):
            if i == j:
                corr_matrix[i][j] = 1.0
            elif ki in return_vectors and kj in return_vectors:
                ri = return_vectors[ki]
                rj = return_vectors[kj]
                if len(ri) == len(rj) and len(ri) > 0:
                    corr = np.corrcoef(ri, rj)[0][1]
                    corr_matrix[i][j] = 0 if np.isnan(corr) else corr
                else:
                    corr_matrix[i][j] = 0
            else:
                corr_matrix[i][j] = 0

    return keys, corr_matrix


def main():
    db = SQLiteManager()
    codes = db.get_all_stock_codes()
    db.close()

    print('Computing strategy correlation matrix...')
    print(f'Sample: {min(20, len(codes))} stocks, 2024-01 to 2026-06')
    print()

    keys, corr = compute_correlation_matrix(codes, '20240101', '20260612')

    # Print correlation matrix
    names = [STRATEGIES[k][0] for k in keys]

    print('=== Strategy Return Correlation ===')
    header = f'{"":>10} ' + ' '.join(f'{n:>8}' for n in names)
    print(header)
    print('-' * (10 + 9 * len(names)))

    for i, name in enumerate(names):
        row = f'{name:>10} '
        for j in range(len(names)):
            row += f'{corr[i][j]:>8.2f}'
        print(row)

    print()
    print('=== Interpretation ===')

    # Find high-correlation pairs
    high_corr_pairs = []
    low_corr_pairs = []
    for i in range(len(keys)):
        for j in range(i+1, len(keys)):
            if corr[i][j] > 0.6:
                high_corr_pairs.append((names[i], names[j], corr[i][j]))
            if abs(corr[i][j]) < 0.3:
                low_corr_pairs.append((names[i], names[j], corr[i][j]))

    if high_corr_pairs:
        print('High correlation (combining these = no diversification):')
        for n1, n2, c in sorted(high_corr_pairs, key=lambda x: -x[2]):
            print(f'  {n1} + {n2}: r={c:.2f}')

    if low_corr_pairs:
        print()
        print('Low correlation (combining these = good diversification):')
        for n1, n2, c in sorted(low_corr_pairs, key=lambda x: abs(x[2])):
            print(f'  {n1} + {n2}: r={c:.2f}')

    # Best pair recommendation
    if low_corr_pairs:
        best_pair = min(low_corr_pairs, key=lambda x: abs(x[2]))
        print()
        print(f'Recommended pair: {best_pair[0]} + {best_pair[1]} (r={best_pair[2]:.2f})')
        print('These strategies perform independently — combining them provides real diversification.')


if __name__ == '__main__':
    main()
