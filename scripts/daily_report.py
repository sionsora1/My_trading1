"""
每日自动报告 — 收盘后扫描策略信号、持仓盈亏、风控状态、明日关注

用法:
    python scripts/daily_report.py                  # 生成文本报告
    python scripts/daily_report.py --html           # 生成HTML报告
    python scripts/daily_report.py --send           # 生成并输出到stdout

定时任务（Cron/计划任务，工作日15:30执行）:
    30 15 * * 1-5 cd /path/to/quant && python scripts/daily_report.py
"""
import sys, os, json, time, random
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import SQLiteManager
from data.fetcher import DataFetcher
from config.settings import (
    LIVE_TRADING_CONFIG, BACKTEST_CONFIG, DATA_CACHE_DIR, DATABASE_PATH
)


# ============================================================
# Strategy signal generators (reuse kline_api logic directly)
# ============================================================

def _compute_indicators(bars):
    """Lightweight version — only compute what strategies need"""
    closes = [b['close'] for b in bars]
    for i, bar in enumerate(bars):
        bar['ma5'] = sum(closes[max(0,i-4):i+1]) / min(i+1, 5)
        bar['ma10'] = sum(closes[max(0,i-9):i+1]) / min(i+1, 10)
        bar['ma20'] = sum(closes[max(0,i-19):i+1]) / min(i+1, 20)
        bar['ma60'] = sum(closes[max(0,i-59):i+1]) / min(i+1, 60)
        if i >= 19 and closes[i-19] > 0:
            bar['return_20d'] = (closes[i] - closes[i-19]) / closes[i-19]
        else:
            bar['return_20d'] = None
    return bars


def _generate_signals_for_stock(bars, strategy_key):
    """Generate buy/sell/hold recommendation for a single stock"""
    bars = _compute_indicators(bars)
    if len(bars) < 60:
        return None, 'INSUFFICIENT_DATA'

    latest = bars[-1]

    if strategy_key == 'momentum':
        if latest['return_20d'] is None or latest['ma20'] is None:
            return None, 'INSUFFICIENT_DATA'
        if latest['return_20d'] > 0.05 and latest['close'] > latest['ma20']:
            return 'BUY', f'momentum={latest["return_20d"]:.1%}, price>MA20'
        elif latest['close'] < latest['ma20']:
            return 'SELL', f'price<MA20'
        return 'HOLD', 'no signal'

    elif strategy_key == 'trend_following':
        if not all(latest.get(k) for k in ['ma5','ma20','ma60']):
            return None, 'INSUFFICIENT_DATA'
        if latest['ma5'] > latest['ma20'] > latest['ma60'] and latest['close'] > latest['ma20']:
            return 'BUY', 'bull alignment'
        elif latest['close'] < latest['ma20'] * 0.98:
            return 'SELL', 'break below MA20 buffer'
        return 'HOLD', 'no signal'

    elif strategy_key == 'mean_reversion':
        if latest['return_20d'] is None or latest['ma20'] is None:
            return None, 'INSUFFICIENT_DATA'
        if latest['return_20d'] < -0.10 and latest['close'] < latest['ma20']:
            return 'BUY', f'oversold, ret={latest["return_20d"]:.1%}'
        elif latest['close'] > latest['ma20']:
            return 'SELL', 'back above MA20'
        return 'HOLD', 'no signal'

    elif strategy_key == 'ma_crossover':
        if len(bars) < 2: return None, 'INSUFFICIENT_DATA'
        prev, curr = bars[-2], bars[-1]
        if not all(k in prev and k in curr for k in ['ma5','ma20','ma60']):
            return None, 'INSUFFICIENT_DATA'
        if prev['ma5'] <= prev['ma20'] and curr['ma5'] > curr['ma20'] and curr['close'] > curr['ma60']:
            return 'BUY', 'golden cross'
        elif prev['ma5'] >= prev['ma20'] and curr['ma5'] < curr['ma20']:
            return 'SELL', 'death cross'
        return 'HOLD', 'no signal'

    return 'HOLD', 'strategy not implemented for daily scan'


STRATEGIES = {
    'momentum': '动量',
    'trend_following': '趋势跟随',
    'mean_reversion': '均值回归',
    'ma_crossover': '均线交叉',
}


# ============================================================
# Report generator
# ============================================================

def generate_report():
    """Generate the daily trading report"""
    now = datetime.now()
    today = now.strftime('%Y%m%d')
    start_date = (now - timedelta(days=180)).strftime('%Y%m%d')

    db = SQLiteManager()
    codes = db.get_all_stock_codes()

    # ── 1. Fetch latest data for all stocks ──
    print(f'[DailyReport] Scanning {len(codes)} stocks...')

    stock_data = {}
    for code in codes:
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
        try:
            bars = db.get_daily_bars(ts_code, start_date, today)
            if len(bars) >= 60:
                stock_data[code] = {'ts_code': ts_code, 'bars': bars}
        except Exception:
            continue

    # ── 2. Generate signals for each strategy ──
    signals = defaultdict(list)  # strategy_key -> [(code, name, signal, reason)]
    for strat_key, strat_name in STRATEGIES.items():
        for code, data in stock_data.items():
            try:
                info = db.get_stock_info(data['ts_code'])
                name = info['name'] if info else code
                signal, reason = _generate_signals_for_stock(data['bars'], strat_key)
                if signal in ('BUY', 'SELL'):
                    signals[strat_key].append({
                        'code': code, 'name': name, 'signal': signal,
                        'reason': reason,
                        'price': data['bars'][-1]['close'],
                        'pct_chg': data['bars'][-1].get('pct_chg', 0),
                    })
            except Exception:
                continue

    # ── 3. Near-signal watchlist (before closing DB) ──
    near_signals = []
    for code, data in stock_data.items():
        info = db.get_stock_info(data['ts_code'])
        name = info['name'] if info else code
        bars = data['bars']
        if len(bars) < 60:
            continue
        latest = bars[-1]
        if latest.get('ma20') and latest['close'] > 0:
            dev = (latest['close'] - latest['ma20']) / latest['ma20']
            if latest.get('return_20d') is not None:
                ret20 = latest['return_20d']
                if 0.03 < ret20 < 0.05 and latest['close'] > latest['ma20']:
                    near_signals.append((code, name, f'momentum接近买入(20d={ret20:.1%})', latest['close']))
                elif -0.12 < ret20 < -0.08:
                    near_signals.append((code, name, f'接近超跌(20d={ret20:.1%})', latest['close']))
                elif abs(dev) < 0.02 and dev != 0:
                    near_signals.append((code, name, f'临近MA20(偏离{dev:.1%})', latest['close']))

    db.close()

    # ── 4. Build report ──
    buy_count = sum(1 for slist in signals.values() for s in slist if s['signal'] == 'BUY')
    sell_count = sum(1 for slist in signals.values() for s in slist if s['signal'] == 'SELL')

    report = []
    report.append("=" * 65)
    report.append(f"A股量化交易系统 — 每日报告")
    report.append(f"生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"数据截止: {today[:4]}-{today[4:6]}-{today[6:8]}")
    report.append("=" * 65)

    # Summary
    report.append(f"\n【信号摘要】")
    report.append(f"  扫描股票: {len(stock_data)} 只")
    report.append(f"  买入信号: {buy_count} 条")
    report.append(f"  卖出信号: {sell_count} 条")

    # Per-strategy detail
    for strat_key, strat_name in STRATEGIES.items():
        slist = signals.get(strat_key, [])
        if not slist:
            continue
        buys = [s for s in slist if s['signal'] == 'BUY']
        sells = [s for s in slist if s['signal'] == 'SELL']

        report.append(f"\n{'─' * 50}")
        report.append(f"【{strat_name}策略】")

        if buys:
            report.append(f"\n  买入建议 ({len(buys)}只):")
            report.append(f"  {'代码':<8} {'名称':<10} {'现价':>8} {'涨跌':>8}  {'理由'}")
            report.append(f"  {'-' * 60}")
            for s in sorted(buys, key=lambda x: x.get('pct_chg', 0) or 0, reverse=True):
                pct = (s.get('pct_chg', 0) or 0)
                report.append(f"  {s['code']:<8} {s['name']:<10} {s['price']:>8.2f} {pct:>+7.2f}%  {s['reason']}")

        if sells:
            report.append(f"\n  卖出建议 ({len(sells)}只):")
            report.append(f"  {'代码':<8} {'名称':<10} {'现价':>8} {'涨跌':>8}  {'理由'}")
            report.append(f"  {'-' * 60}")
            for s in sorted(sells, key=lambda x: x.get('pct_chg', 0) or 0):
                report.append(f"  {s['code']:<8} {s['name']:<10} {s['price']:>8.2f} {s.get('pct_chg', 0) or 0:>+7.2f}%  {s['reason']}")

    # Tomorrow watchlist
    report.append(f"\n{'─' * 50}")
    report.append(f"【明日关注 — 接近触发信号的股票】")
    if near_signals:
        near_signals.sort(key=lambda x: x[3])
        for code, name, reason, price in near_signals[:15]:
            report.append(f"  {code} {name:<10} @{price:.2f}  {reason}")
    else:
        report.append(f"  今日无接近触发信号的股票")

    # Footer
    report.append(f"\n{'=' * 65}")
    report.append(f"报告由量化交易系统自动生成 | {now.strftime('%Y-%m-%d %H:%M')}")
    report.append(f"数据目录: {DATA_CACHE_DIR} | 数据库: {DATABASE_PATH}")
    report.append(f"{'=' * 65}")

    return '\n'.join(report)


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='A股量化每日报告')
    parser.add_argument('--output', '-o', type=str, default=None,
                        help='输出文件路径（默认stdout）')
    parser.add_argument('--html', action='store_true',
                        help='生成HTML格式报告')
    args = parser.parse_args()

    print('Generating daily report...')
    report_text = generate_report()

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report_text)
        print(f'Report saved to {args.output}')
    else:
        print(report_text)

    # Also save a dated copy for archives
    today = datetime.now().strftime('%Y%m%d')
    archive_dir = os.path.join(DATA_CACHE_DIR, 'reports')
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f'daily_report_{today}.txt')
    with open(archive_path, 'w', encoding='utf-8') as f:
        f.write(report_text)
    print(f'\nArchived: {archive_path}')


if __name__ == '__main__':
    main()
