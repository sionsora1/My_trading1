"""
Rolling Walk-Forward Backtest — 滚动窗口策略稳定性检验

Tests strategy performance across many overlapping time windows to reveal:
  - Is the strategy consistently profitable or was one window just lucky?
  - What fraction of windows are profitable?
  - What's the worst-case window?

用法:
    python scripts/rolling_backtest.py                     # 默认参数
    python scripts/rolling_backtest.py --window 365 --step 60
    python scripts/rolling_backtest.py --strategy momentum --stocks 20
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

from backtest.engine import BacktestEngine, BacktestConfig
from strategy import get_strategy, STRATEGY_REGISTRY
from data.fetcher import DataFetcher, DataCache
from data.database import SQLiteManager


def rolling_backtest(
    stock_pool: list,
    strategy_name: str = 'momentum',
    total_start: str = '20240101',
    total_end: str = '20260612',
    window_days: int = 252,   # 1 trading year
    step_days: int = 60,      # step forward 3 months
    max_stocks: int = 20,
) -> dict:
    """
    滚动窗口回测：在多个重叠时间窗口上测试策略稳定性

    Returns:
        dict with window_results list and summary statistics
    """
    print(f'=== Rolling Backtest: {strategy_name} ===')
    print(f'Window: {window_days} days | Step: {step_days} days')
    print(f'Stocks: {min(len(stock_pool), max_stocks)} | Range: {total_start} ~ {total_end}')
    print()

    # Generate window date ranges
    start_dt = datetime.strptime(total_start, '%Y%m%d')
    end_dt = datetime.strptime(total_end, '%Y%m%d')

    windows = []
    current_start = start_dt
    while True:
        current_end = current_start + timedelta(days=window_days)
        if current_end > end_dt:
            break
        windows.append((
            current_start.strftime('%Y%m%d'),
            current_end.strftime('%Y%m%d'),
        ))
        current_start += timedelta(days=step_days)

    print(f'Total windows: {len(windows)}')
    if len(windows) == 0:
        return {'error': 'No valid windows found'}
    if len(windows) > 50:
        print(f'Limiting to first 50 windows')
        windows = windows[:50]

    # Fetch data once for the full range
    fetcher = DataFetcher()
    cache = DataCache()
    pool = stock_pool[:max_stocks]

    cache_filename = f'market_data_{total_start}_{total_end}_{len(pool)}stocks_rolling'
    market_data = cache.load_market_data(cache_filename)

    if not market_data or len(market_data) < 100:
        print(f'Fetching full-range data for {len(pool)} stocks...')
        market_data = fetcher.build_market_data_by_date(pool, total_start, total_end)
        if market_data and len(market_data) > 0:
            cache.save_market_data(market_data, cache_filename)
    else:
        print(f'Using cached data ({len(market_data)} trading days)')

    if not market_data:
        return {'error': 'Failed to fetch market data'}

    # Run backtest on each window
    window_results = []
    config = BacktestConfig.from_dict({
        'initial_capital': 100000,
        'max_position_num': 5,
        'max_single_weight': 0.15,
        'stop_loss_rate': -0.08,
    })

    for i, (w_start, w_end) in enumerate(windows):
        # Filter market_data to this window
        window_data = {
            d: stocks for d, stocks in market_data.items()
            if w_start <= d <= w_end
        }

        if len(window_data) < 30:
            continue

        try:
            engine = BacktestEngine(config)
            strategy = get_strategy(strategy_name)
            result = engine.run(window_data, strategy, print_report=False)

            m = result['metrics']
            window_results.append({
                'window': i + 1,
                'start': w_start,
                'end': w_end,
                'days': len(window_data),
                'total_return': m['total_return'],
                'annual_return': m['annual_return'],
                'max_drawdown': m['max_drawdown'],
                'sharpe_ratio': m['sharpe_ratio'],
                'win_rate': m['trade_win_rate'],
                'total_trades': m['total_trades'],
            })

            status = '+' if m['total_return'] > 0 else '-'
            print(f'  [{i+1:>3}/{len(windows)}] {w_start}~{w_end}  '
                  f'{status}{abs(m["total_return"]):.1%}  DD:{m["max_drawdown"]:.1%}  '
                  f'Trades:{m["total_trades"]}')

        except Exception as e:
            print(f'  [{i+1:>3}/{len(windows)}] {w_start}~{w_end}  ERROR: {e}')

    if not window_results:
        return {'error': 'No successful windows'}

    # Summary statistics
    returns = [w['total_return'] for w in window_results]
    dds = [w['max_drawdown'] for w in window_results]
    sharpes = [w['sharpe_ratio'] for w in window_results]

    returns_arr = np.array(returns)

    summary = {
        'strategy': strategy_name,
        'strategy_name': STRATEGY_REGISTRY.get(strategy_name, {}).get('name', strategy_name),
        'total_windows': len(window_results),
        'profitable_windows': int(np.sum(returns_arr > 0)),
        'profitable_pct': float(np.mean(returns_arr > 0) * 100),
        'mean_return': float(np.mean(returns_arr)),
        'median_return': float(np.median(returns_arr)),
        'std_return': float(np.std(returns_arr)),
        'min_return': float(np.min(returns_arr)),
        'max_return': float(np.max(returns_arr)),
        'mean_drawdown': float(np.mean(dds)),
        'max_drawdown': float(np.min(dds)),  # most negative
        'mean_sharpe': float(np.mean(sharpes)),
        'consistency_score': float(np.mean(returns_arr > 0) / (1 + np.std(returns_arr))),
        # Worst/best windows
        'worst_window': window_results[int(np.argmin(returns_arr))],
        'best_window': window_results[int(np.argmax(returns_arr))],
    }

    return {
        'summary': summary,
        'windows': window_results,
    }


def print_summary(result: dict):
    """Print readable summary"""
    s = result['summary']

    print()
    print('=' * 60)
    print(f'Rolling Backtest Summary: {s["strategy_name"]}')
    print('=' * 60)
    print(f'  Windows tested:     {s["total_windows"]}')
    print(f'  Profitable windows: {s["profitable_windows"]}/{s["total_windows"]} '
          f'({s["profitable_pct"]:.0f}%)')
    print()
    print(f'  Mean return:        {s["mean_return"]:+.2%}')
    print(f'  Median return:      {s["median_return"]:+.2%}')
    print(f'  Std of returns:     {s["std_return"]:.2%}')
    print(f'  Best window:        {s["max_return"]:+.2%}')
    print(f'  Worst window:       {s["min_return"]:+.2%}')
    print()
    print(f'  Mean drawdown:      {s["mean_drawdown"]:.2%}')
    print(f'  Worst drawdown:     {s["max_drawdown"]:.2%}')
    print(f'  Mean Sharpe:        {s["mean_sharpe"]:.2f}')
    print()
    print(f'  Consistency Score:  {s["consistency_score"]:.3f}')
    print(f'    (0=random, 1=perfectly consistent)')
    print()
    print(f'  Worst window: {s["worst_window"]["start"]}~{s["worst_window"]["end"]} '
          f'-> {s["worst_window"]["total_return"]:+.2%}')
    print(f'  Best window:  {s["best_window"]["start"]}~{s["best_window"]["end"]} '
          f'-> {s["best_window"]["total_return"]:+.2%}')
    print('=' * 60)

    # Interpretation
    if s['consistency_score'] > 0.5:
        print('\nInterpretation: Strategy is CONSISTENT — high confidence it will perform similarly going forward.')
    elif s['profitable_pct'] >= 50:
        print('\nInterpretation: Strategy is MARGINALLY profitable — positive expectation but high variance.')
    else:
        print('\nInterpretation: Strategy is UNRELIABLE — more losing windows than winning ones. Do not use for live trading.')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Rolling Walk-Forward Backtest')
    parser.add_argument('--strategy', type=str, default='momentum',
                        choices=list(STRATEGY_REGISTRY.keys()),
                        help='Strategy to test')
    parser.add_argument('--window', type=int, default=252,
                        help='Window size in days (default 252 = 1 year)')
    parser.add_argument('--step', type=int, default=60,
                        help='Step size in days (default 60 = ~3 months)')
    parser.add_argument('--stocks', type=int, default=20,
                        help='Number of stocks to test (default 20)')
    parser.add_argument('--start', type=str, default='20240101',
                        help='Total start date')
    parser.add_argument('--end', type=str, default='20260612',
                        help='Total end date')
    parser.add_argument('--all-strategies', action='store_true',
                        help='Test all strategies')
    args = parser.parse_args()

    # Load stock pool from DB
    db = SQLiteManager()
    codes = db.get_all_stock_codes()
    db.close()

    strategies_to_test = [args.strategy]
    if args.all_strategies:
        strategies_to_test = [k for k in STRATEGY_REGISTRY
                              if k not in ('ai_factor', 'sector_rotation', 'intraday_reversal')]

    for s_name in strategies_to_test:
        result = rolling_backtest(
            stock_pool=codes,
            strategy_name=s_name,
            total_start=args.start,
            total_end=args.end,
            window_days=args.window,
            step_days=args.step,
            max_stocks=args.stocks,
        )

        if 'summary' in result:
            print_summary(result)
        else:
            print(f'{s_name}: {result.get("error", "unknown error")}')


if __name__ == '__main__':
    main()
