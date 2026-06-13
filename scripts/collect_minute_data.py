"""
分钟数据采集 — 交易时段定时采集5分钟K线

用法（手动单次采集当天）:
    python scripts/collect_minute_data.py

用法（定时任务，工作日9:30-15:00每5分钟执行）:
    */5 9-15 * * 1-5 python scripts/collect_minute_data.py >> logs/minute_collect.log 2>&1

采集的数据写入 minute_bars 表，供日内策略使用。
"""
import sys, os, time, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime
import akshare as ak
from data.database import SQLiteManager
from data.fetcher import DataFetcher
from config.settings import LIVE_TRADING_CONFIG


def collect_minute_bars(stock_pool: list = None, period: int = 5):
    """
    采集指定股票的分钟K线数据

    Args:
        stock_pool: 股票代码列表，None则使用配置中的默认池
        period: K线周期（1/5/15/30/60分钟）
    """
    if stock_pool is None:
        stock_pool = LIVE_TRADING_CONFIG.get('scan', {}).get('stock_pool', [])[:10]

    now = datetime.now()

    # 非交易时段跳过
    if now.weekday() >= 5:
        print(f'[{now:%H:%M}] Weekend - skipping')
        return 0
    if not ((now.hour == 9 and now.minute >= 30) or
            (10 <= now.hour <= 11) or
            (now.hour == 13 and now.minute >= 1) or
            (now.hour == 14)):
        print(f'[{now:%H:%M}] Outside trading hours - skipping')
        return 0

    db = SQLiteManager()
    total_rows = 0

    for code in stock_pool:
        ts_code = f"{code}.SH" if str(code).startswith('6') else f"{code}.SZ"
        symbol = ts_code.split('.')[0]

        try:
            df = ak.stock_zh_a_hist_min_em(
                symbol=symbol,
                period=str(period),
                start_date=now.strftime('%Y-%m-%d'),
                end_date=now.strftime('%Y-%m-%d'),
                adjust=''
            )

            if df is None or df.empty:
                continue

            rows = []
            for _, row in df.iterrows():
                trade_time = str(row.get('时间', row.get('time', '')))
                if not trade_time:
                    continue

                # Normalize time format: '2024-01-02 09:35:00'
                if len(trade_time) == 10:  # just date
                    continue

                rows.append({
                    'ts_code': ts_code,
                    'trade_time': trade_time,
                    'period': period,
                    'open': float(row.get('开盘', row.get('open', 0)) or 0),
                    'high': float(row.get('最高', row.get('high', 0)) or 0),
                    'low': float(row.get('最低', row.get('low', 0)) or 0),
                    'close': float(row.get('收盘', row.get('close', 0)) or 0),
                    'volume': float(row.get('成交量', row.get('volume', 0)) or 0),
                })

            if rows:
                db.upsert_minute_bars(rows)
                total_rows += len(rows)

            time.sleep(0.3 + random.random() * 0.2)

        except Exception as e:
            # Silently skip individual stock errors during collection
            continue

    db.close()

    if total_rows > 0:
        print(f'[{now:%H:%M}] Collected {total_rows} minute bars ({len(stock_pool)} stocks, {period}min)')
    return total_rows


def main():
    import argparse
    parser = argparse.ArgumentParser(description='分钟数据采集')
    parser.add_argument('--period', type=int, default=5, choices=[1, 5, 15, 30, 60])
    parser.add_argument('--stocks', type=int, default=10, help='Number of stocks to collect')
    parser.add_argument('--loop', action='store_true', help='Keep collecting until end of trading day')
    parser.add_argument('--all', action='store_true', help='Collect all 81 stocks')
    args = parser.parse_args()

    # Determine stock pool
    if args.all:
        db = SQLiteManager()
        pool = db.get_all_stock_codes()
        db.close()
    else:
        pool = LIVE_TRADING_CONFIG.get('scan', {}).get('stock_pool', [])[:args.stocks]

    print(f'Minute data collector started: {len(pool)} stocks, {args.period}min bars')
    print(f'Time: {datetime.now():%Y-%m-%d %H:%M:%S}')

    if args.loop:
        # Loop until market close
        while True:
            now = datetime.now()
            if now.hour >= 15 or now.weekday() >= 5:
                print('Market closed. Stopping.')
                break
            collect_minute_bars(pool, args.period)
            time.sleep(300)  # 5 minutes
    else:
        n = collect_minute_bars(pool, args.period)
        print(f'Done: {n} rows collected')


if __name__ == '__main__':
    main()
