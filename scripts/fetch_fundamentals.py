"""
Fix 1.1: Fetch real fundamentals and stock_info data from AKShare
"""
import sys, os, time, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import SQLiteManager
import akshare as ak
import pandas as pd


def main():
    db = SQLiteManager()
    codes = db.get_all_stock_codes()
    print(f'=== Fix 1.1: Real fundamentals for {len(codes)} stocks ===')

    # Step 1: Fetch PE/PB/market_cap/industry for all stocks
    print('[Step 1] Real stock info (PE/PB/MCap/Industry)...')
    try:
        df_all = ak.stock_a_spot_em()
        df_all['code'] = df_all['代码'].astype(str)
        print(f'  Got {len(df_all)} stocks from spot data')

        updated_info = 0
        for code in codes:
            try:
                match = df_all[df_all['code'] == code]
                if match.empty:
                    continue
                row = match.iloc[0]
                ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                pe = float(row.get('市盈率-动态', 0) or 0)
                pb = float(row.get('市净率', 0) or 0)
                mcap = float(row.get('总市值', 0) or 0)
                if pe > 0:
                    db.upsert_stock_info([{
                        'ts_code': ts_code,
                        'name': str(row.get('名称', '')),
                        'industry': str(row.get('所属行业', '')),
                        'market': 'SH' if code.startswith('6') else 'SZ',
                        'market_cap': mcap,
                        'pe': pe,
                        'pb': pb,
                        'list_date': '',
                        'delist_date': '',
                    }])
                    updated_info += 1
            except Exception:
                continue
        print(f'  Updated {updated_info}/{len(codes)} stocks')
    except Exception as e:
        print(f'  ERROR: {e}')

    # Step 2: Fetch financial statements
    print('[Step 2] Financial statements (ROE, margins, growth)...')
    updated_fund = 0

    for i, code in enumerate(codes):
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

        try:
            df_fin = ak.stock_financial_abstract_ths(symbol=code, indicator='按报告期')
            if df_fin is None or df_fin.empty:
                time.sleep(0.5 + random.random())
                continue

            # Find report date columns
            report_dates = []
            for col in df_fin.columns:
                col_str = str(col)
                if len(col_str) >= 8 and col_str[:4].isdigit():
                    report_dates.append(col_str[:10].replace('-', '')[:8])

            if not report_dates:
                time.sleep(0.5 + random.random())
                continue

            rows_to_insert = []
            for report_date in report_dates[-8:]:
                col = None
                for c in df_fin.columns:
                    if str(c)[:10].replace('-', '')[:8] == report_date:
                        col = c
                        break
                if col is None:
                    continue

                row_data = {'ts_code': ts_code, 'report_date': report_date}

                for _, fin_row in df_fin.iterrows():
                    indicator = str(fin_row.get('指标', fin_row.get('选项', '')))
                    value = fin_row.get(col, 0)
                    if pd.isna(value):
                        value = 0
                    value = float(value)

                    if 'ROE' in indicator or '净资产收益率' in indicator:
                        if value > 0 and value < 100 and not row_data.get('roe'):
                            row_data['roe'] = value / 100.0 if value > 1 else value
                    elif '毛利率' in indicator:
                        if value > 0 and value < 100 and not row_data.get('gross_margin'):
                            row_data['gross_margin'] = value / 100.0 if value > 1 else value
                    elif '营业总收入' in indicator and '增长率' in indicator:
                        if not row_data.get('revenue_growth'):
                            row_data['revenue_growth'] = value / 100.0 if abs(value) > 1 else value
                    elif '净利润' in indicator and '增长率' in indicator:
                        if not row_data.get('profit_growth'):
                            row_data['profit_growth'] = value / 100.0 if abs(value) > 1 else value
                    elif '营业总收入' in indicator and '增长率' not in indicator:
                        if value > 0 and not row_data.get('revenue'):
                            row_data['revenue'] = value
                    elif '净利润' in indicator and '增长率' not in indicator and '归' in indicator:
                        if not row_data.get('net_profit'):
                            row_data['net_profit'] = value

                row_data.setdefault('roe', 0.0)
                row_data.setdefault('gross_margin', 0.0)
                row_data.setdefault('revenue', 0.0)
                row_data.setdefault('net_profit', 0.0)
                row_data.setdefault('ocf', 0.0)
                row_data.setdefault('net_assets', 0.0)
                row_data.setdefault('revenue_growth', 0.0)
                row_data.setdefault('profit_growth', 0.0)
                row_data.setdefault('accrual_ratio', 0.0)

                if row_data['roe'] > 0 or row_data['revenue'] > 0:
                    rows_to_insert.append(row_data)

            if rows_to_insert:
                db.upsert_fundamentals(rows_to_insert)
                updated_fund += 1
                info = db.get_stock_info(ts_code)
                name = info['name'] if info else code
                print(f'  [{i+1}/{len(codes)}] {code} {name}: +{len(rows_to_insert)} quarters')

            time.sleep(0.8 + random.random() * 0.4)

        except Exception as e:
            print(f'  [{i+1}/{len(codes)}] {code}: ERR - {str(e)[:60]}')
            time.sleep(2)

    print(f'Fundamentals updated: {updated_fund}/{len(codes)} stocks')

    # Verify
    cur = db._conn.execute('SELECT COUNT(*) FROM fundamentals')
    print(f'\nfundamentals rows: {cur.fetchone()[0]}')
    cur = db._conn.execute('SELECT COUNT(DISTINCT ts_code) FROM fundamentals')
    print(f'Stocks with fundamentals: {cur.fetchone()[0]}')

    cur = db._conn.execute("SELECT ts_code, pe, pb, market_cap FROM stock_info WHERE pe != 20.0 OR pb != 3.0 LIMIT 5")
    real = cur.fetchall()
    print(f'Real PE/PB stocks: {len(real)} in sample')
    for r in real:
        print(f'  {r["ts_code"]}: PE={r["pe"]:.1f} PB={r["pb"]:.2f} MCap={r["market_cap"]/1e8:.0f} yi')

    cur = db._conn.execute('SELECT COUNT(*) FROM stock_info WHERE pe = 20.0 AND pb = 3.0')
    fake_left = cur.fetchone()[0]
    print(f'Still placeholder: {fake_left}/{len(codes)}')

    db.close()
    print('Done!')


if __name__ == '__main__':
    main()
