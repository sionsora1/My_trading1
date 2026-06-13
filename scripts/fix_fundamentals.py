"""
Fix 1.1: Fill fundamentals & stock_info with real data from Eastmoney push2 API
"""
import sys, os, time, random, requests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import SQLiteManager
import akshare as ak


def update_stock_info_from_push2(db, codes):
    """Use push2 API to get real PE/PB/MCap/Industry for all stocks"""
    print('[Step 1] Updating stock_info with real PE/PB/MCap/Industry...')

    updated = 0
    # Batch 50 per request
    for i in range(0, len(codes), 50):
        batch = codes[i:i+50]
        secids = []
        for code in batch:
            prefix = '1.' if code.startswith('6') else '0.'
            secids.append(f'{prefix}{code}')

        try:
            url = 'http://push2.eastmoney.com/api/qt/ulist.np/get'
            params = {
                'fltt': '2', 'invt': '2',
                'fields': 'f2,f9,f10,f12,f14,f20,f100',
                'secids': ','.join(secids),
                '_': int(time.time() * 1000),
            }
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'http://quote.eastmoney.com/',
            }
            resp = requests.get(url, params=params, headers=headers, timeout=15)

            if resp.status_code != 200:
                print(f'  Batch {i//50+1}: HTTP {resp.status_code}')
                continue

            data = resp.json()
            if not data.get('data') or not data['data'].get('diff'):
                print(f'  Batch {i//50+1}: Empty response')
                continue

            rows = []
            for item in data['data']['diff']:
                code = str(item.get('f12', ''))
                ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                name = str(item.get('f14', ''))
                industry = str(item.get('f100', ''))
                pe = float(item.get('f9', 0) or 0)
                pb = float(item.get('f10', 0) or 0)
                mcap = float(item.get('f20', 0) or 0)

                if mcap > 0:  # Use market cap as indicator of valid data (PE can be negative)
                    rows.append({
                        'ts_code': ts_code,
                        'name': name,
                        'industry': industry,
                        'market': 'SH' if code.startswith('6') else 'SZ',
                        'market_cap': mcap,
                        'pe': pe,
                        'pb': pb,
                        'list_date': '',
                        'delist_date': '',
                    })

            if rows:
                db.upsert_stock_info(rows)
                updated += len(rows)

            time.sleep(0.3 + random.random() * 0.3)

        except Exception as e:
            print(f'  Batch {i//50+1}: Error - {e}')
            time.sleep(2)

    print(f'  Updated {updated}/{len(codes)} stocks with real PE/PB/MCap')
    return updated


def update_fundamentals_from_akshare(db, codes):
    """Try to fetch financial statements for fundamentals table"""
    print('[Step 2] Fetching financial statements...')

    updated = 0
    for i, code in enumerate(codes):
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

        try:
            # Try financial analysis indicator
            df = ak.stock_financial_analysis_indicator(symbol=code)
            if df is None or df.empty:
                time.sleep(1 + random.random())
                continue

            # Get latest 8 quarters
            rows_to_insert = []
            for _, row in df.tail(8).iterrows():
                report_date = str(row.get('日期', row.get('报告期', '')))[:10].replace('-', '')[:8]
                if len(report_date) != 8:
                    continue

                roe = float(row.get('净资产收益率', row.get('ROE', 0)) or 0)
                gross_margin = float(row.get('销售毛利率', row.get('毛利率', 0)) or 0)
                revenue_growth = float(row.get('营业收入同比增长率', row.get('营收增长率', 0)) or 0)
                profit_growth = float(row.get('归母净利润同比增长率', row.get('净利润增长率', 0)) or 0)

                if roe > 0 or gross_margin > 0:
                    rows_to_insert.append({
                        'ts_code': ts_code,
                        'report_date': report_date,
                        'roe': roe / 100.0 if abs(roe) > 1 else roe,
                        'gross_margin': gross_margin / 100.0 if abs(gross_margin) > 1 else gross_margin,
                        'revenue': 0,
                        'net_profit': 0,
                        'ocf': 0,
                        'net_assets': 0,
                        'revenue_growth': revenue_growth / 100.0 if abs(revenue_growth) > 1 else revenue_growth,
                        'profit_growth': profit_growth / 100.0 if abs(profit_growth) > 1 else profit_growth,
                        'accrual_ratio': 0.0,
                    })

            if rows_to_insert:
                db.upsert_fundamentals(rows_to_insert)
                updated += 1
                info = db.get_stock_info(ts_code)
                name = info['name'] if info else code
                if i < 5 or i % 10 == 0:
                    print(f'  [{i+1}/{len(codes)}] {code} {name}: +{len(rows_to_insert)} quarters')

            time.sleep(0.8 + random.random() * 0.4)

        except Exception as e:
            msg = str(e)[:60]
            if i < 5:
                print(f'  [{i+1}/{len(codes)}] {code}: {msg}')
            time.sleep(2)

    print(f'  Fundamentals updated: {updated}/{len(codes)} stocks')
    return updated


def main():
    db = SQLiteManager()
    codes = db.get_all_stock_codes()
    print(f'=== Fix 1.1: Real data for {len(codes)} stocks ===')
    print()

    # Step 1: PE/PB/MCap/Industry from push2 (always works)
    update_stock_info_from_push2(db, codes)

    # Step 2: Financial statements (may partially fail on weekends)
    update_fundamentals_from_akshare(db, codes)

    # Verify
    print()
    print('=== Verification ===')

    cur = db._conn.execute('SELECT COUNT(*) FROM fundamentals')
    n_fund = cur.fetchone()[0]
    cur = db._conn.execute('SELECT COUNT(DISTINCT ts_code) FROM fundamentals')
    n_fund_stocks = cur.fetchone()[0]

    cur = db._conn.execute('SELECT COUNT(*) FROM stock_info WHERE pe != 20.0 OR pb != 3.0')
    n_real = cur.fetchone()[0]
    cur = db._conn.execute('SELECT COUNT(*) FROM stock_info WHERE pe = 20.0 AND pb = 3.0')
    n_fake = cur.fetchone()[0]

    print(f'  fundamentals: {n_fund} rows ({n_fund_stocks} stocks)')
    print(f'  stock_info: {n_real} real, {n_fake} still placeholder')

    # Show some examples
    cur = db._conn.execute('''
        SELECT ts_code, name, pe, pb, market_cap, industry
        FROM stock_info WHERE pe != 20.0 OR pb != 3.0
        LIMIT 5
    ''')
    print('\n  Sample real data:')
    for r in cur.fetchall():
        print(f'    {r["ts_code"]} {r["name"]}: PE={r["pe"]:.1f} PB={r["pb"]:.2f} '
              f'MCap={r["market_cap"]/1e8:.0f}yi {r["industry"]}')

    cur = db._conn.execute('''
        SELECT ts_code, report_date, roe, gross_margin, profit_growth
        FROM fundamentals ORDER BY ts_code, report_date DESC LIMIT 5
    ''')
    print('\n  Sample fundamentals:')
    for r in cur.fetchall():
        print(f'    {r["ts_code"]} {r["report_date"]}: ROE={r["roe"]:.3f} '
              f'GM={r["gross_margin"]:.3f} PG={r["profit_growth"]:.3f}')

    db.close()
    print('\nDone!')


if __name__ == '__main__':
    main()
