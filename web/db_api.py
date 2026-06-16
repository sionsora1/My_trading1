"""
数据库可视化 API — 分时图 / 财务数据 / 多股对比 / CSV导出
"""

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, List
import sys
import os
import csv
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import SQLiteManager
from utils.indicators import compute_advanced_indicators

router = APIRouter(prefix="/api/db", tags=["db_explorer"])


# ═══════════════════════════════════════════════════════════════
# 买卖五档盘口 — GET /api/db/depth/{code}
# ═══════════════════════════════════════════════════════════════

@router.get("/depth/{code}")
def get_order_depth(code: str):
    """获取单只股票实时买卖五档盘口"""
    from data.sources import get_data_source

    source = get_data_source()

    # 通过 AKShare 源获取盘口
    if hasattr(source, 'get_order_depth'):
        result = source.get_order_depth(code)
        if result:
            return result

    # 如果主数据源不支持，尝试直接调用 akshare
    try:
        from data.sources.akshare_source import AKShareSource
        akshare = AKShareSource()
        result = akshare.get_order_depth(code)
        if result:
            return result
    except Exception:
        pass

    raise HTTPException(
        status_code=404,
        detail=f"无法获取 {code} 的盘口数据（非交易时段或股票停牌）"
    )


# ═══════════════════════════════════════════════════════════════
# 分时图 — GET /api/db/minute/{code}
# ═══════════════════════════════════════════════════════════════

@router.get("/minute/{code}")
def get_minute_bars(
    code: str,
    date: str = Query(default='', description='交易日期 YYYYMMDD，留空取最新'),
    period: int = Query(default=5, description='周期(分钟): 1/5/15/30/60'),
):
    """获取单只股票的分钟K线数据"""
    db = SQLiteManager()
    ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

    info = db.get_stock_info(ts_code)
    if not info:
        db.close()
        raise HTTPException(status_code=404, detail=f"股票 {code} 不在数据库中")

    # 获取可用日期
    available_dates = _get_available_minute_dates(db, ts_code)

    # 如果未指定日期，使用最新
    if not date:
        date = available_dates[0] if available_dates else ''

    if not date:
        db.close()
        return {
            'code': code, 'ts_code': ts_code,
            'name': info.get('name', code),
            'date': '', 'period': period,
            'bars': [], 'count': 0,
            'available_dates': available_dates,
        }

    # 格式化日期: YYYYMMDD → YYYY-MM-DD
    date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

    bars = db.get_minute_bars_by_date(ts_code, date, period)
    db.close()

    if not bars:
        return {
            'code': code, 'ts_code': ts_code,
            'name': info.get('name', code),
            'date': date, 'period': period,
            'bars': [], 'count': 0,
            'available_dates': available_dates,
        }

    # 计算 VWAP
    cum_vp = 0.0
    cum_vol = 0.0

    result_bars = []
    for b in bars:
        typical_price = (b['open'] + b['high'] + b['low'] + b['close']) / 4
        cum_vp += typical_price * (b['volume'] or 0)
        cum_vol += (b['volume'] or 0)
        vwap = cum_vp / cum_vol if cum_vol > 0 else typical_price

        # trade_time 格式: "YYYY-MM-DD HH:MM:SS"
        time_str = b['trade_time']
        if ' ' in time_str:
            time_str = time_str.split(' ')[1][:5]  # "HH:MM"

        result_bars.append({
            'time': time_str,
            'open': round(b['open'], 2),
            'high': round(b['high'], 2),
            'low': round(b['low'], 2),
            'close': round(b['close'], 2),
            'volume': b['volume'],
            'amount': b.get('amount', 0),
            'vwap': round(vwap, 2),
        })

    return {
        'code': code, 'ts_code': ts_code,
        'name': info.get('name', code),
        'date': date, 'period': period,
        'bars': result_bars, 'count': len(result_bars),
        'available_dates': available_dates,
    }


def _get_available_minute_dates(db: SQLiteManager, ts_code: str, limit: int = 20) -> list:
    """获取某只股票有分钟数据的最近日期列表"""
    sql = """
        SELECT DISTINCT substr(trade_time, 1, 10) as d
        FROM minute_bars
        WHERE ts_code = ?
        ORDER BY d DESC
        LIMIT ?
    """
    cur = db._conn.execute(sql, (ts_code, limit))
    return [row['d'].replace('-', '') for row in cur.fetchall()]


# ═══════════════════════════════════════════════════════════════
# 财务数据面板 — GET /api/db/financial/{code}
# ═══════════════════════════════════════════════════════════════

@router.get("/financial/{code}")
def get_financial_data(code: str):
    """获取股票财务数据（基本面 + PE/PB Band）"""
    db = SQLiteManager()
    ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

    info = db.get_stock_info(ts_code)
    if not info:
        db.close()
        raise HTTPException(status_code=404, detail=f"股票 {code} 不在数据库中")

    # 1. 财务数据（基本面）
    fundamentals = []
    try:
        sql = """
            SELECT report_date, roe, gross_margin, revenue, net_profit,
                   revenue_growth, profit_growth, net_assets,
                   total_assets, operating_revenue, operating_profit,
                   operating_cf, total_shares, float_shares
            FROM fundamentals
            WHERE ts_code = ?
            ORDER BY report_date DESC
            LIMIT 20
        """
        cur = db._conn.execute(sql, (ts_code,))
        for row in cur.fetchall():
            d = dict(row)
            # 金额单位转换：元 → 亿元
            for k in ('revenue', 'net_profit', 'net_assets', 'total_assets',
                       'operating_revenue', 'operating_profit'):
                if d.get(k):
                    d[k] = round(d[k] / 100000000, 2)  # 转亿
            # 比率转为百分比
            for k in ('roe', 'gross_margin', 'revenue_growth', 'profit_growth'):
                if d.get(k):
                    d[k] = round(d[k] * 100, 2)
            fundamentals.append(d)
    except Exception:
        pass  # 表可能为空

    # 2. PE/PB Band（近3年日线 + 最新财务数据估算）
    pe_pb_history = []
    try:
        bars = db.get_daily_bars(ts_code, '20230101', '20261231')
        if bars and fundamentals:
            # 使用最近一期年报的每股收益和每股净资产
            latest_annual = None
            for f in fundamentals:
                if f.get('report_date', '').endswith('1231'):
                    latest_annual = f
                    break
            if not latest_annual:
                latest_annual = fundamentals[0] if fundamentals else None

            if latest_annual:
                total_shares = latest_annual.get('total_shares') or info.get('market_cap', 0)
                net_profit = latest_annual.get('net_profit', 0)
                net_assets = latest_annual.get('net_assets', 0)

                # 每股收益（EPS）和每股净资产（BPS）
                if total_shares and total_shares > 0:
                    # total_shares from fundamentals is in 亿元 after conversion? No — check.
                    # Actually total_shares from the table is in shares, not converted
                    pass

                # 简化：从 stock_info 获取 PE/PB（akshare 提供了实时 PE/PB）
                current_pe = info.get('pe')
                current_pb = info.get('pb')

                for b in bars[-750:]:  # 最近 ~3 年
                    close = b['close']
                    date = b['trade_date']
                    # 使用 stock_info 的 PE/PB 和当天收盘价反推 EPS/BPS
                    # PE = close / EPS  → EPS = close / PE
                    # 然后 pe_band = close / EPS  (daily)
                    if current_pe and current_pe > 0 and close > 0:
                        eps = close / current_pe  # 近似值
                        pe_daily = round(close / eps, 2) if eps > 0 else None
                    else:
                        pe_daily = None

                    if current_pb and current_pb > 0 and close > 0:
                        bps = close / current_pb
                        pb_daily = round(close / bps, 2) if bps > 0 else None
                    else:
                        pb_daily = None

                    if pe_daily or pb_daily:
                        pe_pb_history.append({
                            'date': date,
                            'close': round(close, 2),
                            'pe': pe_daily,
                            'pb': pb_daily,
                        })
    except Exception:
        pass

    # 3. 摘要
    finance_summary = {
        'latest_roe': fundamentals[0]['roe'] if fundamentals else None,
        'latest_gross_margin': fundamentals[0]['gross_margin'] if fundamentals else None,
        'market_cap': info.get('market_cap'),
        'pe_current': info.get('pe'),
        'pb_current': info.get('pb'),
    }
    if len(fundamentals) >= 4:
        growths = [f['revenue_growth'] for f in fundamentals[:4] if f.get('revenue_growth') is not None]
        finance_summary['avg_revenue_growth_3y'] = round(sum(growths) / len(growths), 2) if growths else None
        pgrowths = [f['profit_growth'] for f in fundamentals[:4] if f.get('profit_growth') is not None]
        finance_summary['avg_profit_growth_3y'] = round(sum(pgrowths) / len(pgrowths), 2) if pgrowths else None

    db.close()

    return {
        'code': code, 'ts_code': ts_code,
        'name': info.get('name', code),
        'industry': info.get('industry', ''),
        'fundamentals': fundamentals,
        'pe_pb_history': pe_pb_history,
        'finance_summary': finance_summary,
    }


# ═══════════════════════════════════════════════════════════════
# 多股对比 — POST /api/db/compare
# ═══════════════════════════════════════════════════════════════

class CompareRequest(BaseModel):
    codes: List[str]  # 2-5 stock codes
    start_date: str = '20240101'
    end_date: str = '20251231'


@router.post("/compare")
def compare_stocks(req: CompareRequest):
    """多股归一化对比（base=100）"""
    if len(req.codes) < 2:
        raise HTTPException(status_code=400, detail="至少选择2只股票")
    if len(req.codes) > 5:
        raise HTTPException(status_code=400, detail="最多选择5只股票")

    db = SQLiteManager()

    # 拉取所有股票的日线
    all_series = {}
    stock_info_list = []

    for code in req.codes:
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
        info = db.get_stock_info(ts_code)
        if not info:
            db.close()
            raise HTTPException(status_code=404, detail=f"股票 {code} 不在数据库中")

        bars = db.get_daily_bars(ts_code, req.start_date, req.end_date)
        if not bars:
            db.close()
            raise HTTPException(status_code=404, detail=f"股票 {code} 在指定日期范围内无数据")

        date_close = {b['trade_date']: b['close'] for b in bars if b['close']}
        all_series[code] = {
            'name': info.get('name', code),
            'industry': info.get('industry', ''),
            'data': date_close,
        }

    db.close()

    # 找到所有股票共有的日期（交集），按日期排序
    common_dates = sorted(set.intersection(*[set(s['data'].keys()) for s in all_series.values()]))

    if len(common_dates) < 2:
        raise HTTPException(status_code=400, detail="选择的股票没有足够重叠的交易日")

    # 归一化到 base=100
    stocks_result = []
    normalized = {}

    for code, series in all_series.items():
        prices = [series['data'][d] for d in common_dates]
        base_price = prices[0]
        if base_price <= 0:
            continue
        normalized_vals = [round(p / base_price * 100, 2) for p in prices]
        normalized[code] = normalized_vals

        end_price = prices[-1]
        total_return = round((end_price / base_price - 1) * 100, 2)

        # 波动率
        returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
        avg_ret = sum(returns) / len(returns) if returns else 0
        variance = sum((r - avg_ret) ** 2 for r in returns) / len(returns) if returns else 0
        volatility = round((variance ** 0.5) * (252 ** 0.5) * 100, 2)  # 年化

        stocks_result.append({
            'code': code,
            'name': series['name'],
            'industry': series['industry'],
            'start_price': round(base_price, 2),
            'end_price': round(end_price, 2),
            'total_return_pct': total_return,
            'volatility': volatility,
        })

    return {
        'stocks': stocks_result,
        'dates': common_dates,
        'normalized_values': normalized,
        'count': len(stocks_result),
    }


# ═══════════════════════════════════════════════════════════════
# CSV 导出 — GET /api/db/kline/{code}/export
# ═══════════════════════════════════════════════════════════════

@router.get("/kline/{code}/export")
def export_kline_csv(
    code: str,
    start_date: str = Query(default='20240101'),
    end_date: str = Query(default='20251231'),
    format: str = Query(default='csv'),
):
    """导出股票K线数据为CSV文件（含所有技术指标）"""
    db = SQLiteManager()
    ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

    info = db.get_stock_info(ts_code)
    if not info:
        db.close()
        raise HTTPException(status_code=404, detail=f"股票 {code} 不在数据库中")

    bars = db.get_daily_bars(ts_code, start_date, end_date)
    db.close()

    if not bars:
        raise HTTPException(status_code=404, detail=f"股票 {code} 在指定日期范围内无数据")

    # 计算技术指标
    closes = [b['close'] for b in bars]
    for i, bar in enumerate(bars):
        if i >= 4:
            bar['ma5'] = sum(closes[i-4:i+1]) / 5
        else:
            bar['ma5'] = None
        if i >= 9:
            bar['ma10'] = sum(closes[i-9:i+1]) / 10
        else:
            bar['ma10'] = None
        if i >= 19:
            bar['ma20'] = sum(closes[i-19:i+1]) / 20
        else:
            bar['ma20'] = None
        if i >= 59:
            bar['ma60'] = sum(closes[i-59:i+1]) / 60
        else:
            bar['ma60'] = None

    compute_advanced_indicators(bars)

    # 构建 CSV
    output = io.StringIO()
    # UTF-8 BOM for Excel compatibility
    output.write('﻿')

    columns = [
        'date', 'open', 'high', 'low', 'close', 'volume', 'pct_chg',
        'ma5', 'ma10', 'ma20', 'ma60',
        'macd_dif', 'macd_dea', 'macd_hist',
        'rsi6', 'rsi12', 'rsi24',
        'boll_upper', 'boll_mid', 'boll_lower', 'boll_width',
        'kdj_k', 'kdj_d', 'kdj_j',
    ]

    writer = csv.DictWriter(output, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()

    for b in bars:
        row = {
            'date': b.get('trade_date', ''),
            'open': b.get('open'),
            'high': b.get('high'),
            'low': b.get('low'),
            'close': b.get('close'),
            'volume': b.get('volume'),
            'pct_chg': b.get('pct_chg'),
            'ma5': round(b['ma5'], 2) if b.get('ma5') else '',
            'ma10': round(b['ma10'], 2) if b.get('ma10') else '',
            'ma20': round(b['ma20'], 2) if b.get('ma20') else '',
            'ma60': round(b['ma60'], 2) if b.get('ma60') else '',
            'macd_dif': b.get('macd_dif', ''),
            'macd_dea': b.get('macd_dea', ''),
            'macd_hist': b.get('macd_hist', ''),
            'rsi6': b.get('rsi6', ''),
            'rsi12': b.get('rsi12', ''),
            'rsi24': b.get('rsi24', ''),
            'boll_upper': b.get('boll_upper', ''),
            'boll_mid': b.get('boll_mid', ''),
            'boll_lower': b.get('boll_lower', ''),
            'boll_width': b.get('boll_width', ''),
            'kdj_k': b.get('kdj_k', ''),
            'kdj_d': b.get('kdj_d', ''),
            'kdj_j': b.get('kdj_j', ''),
        }
        writer.writerow(row)

    csv_content = output.getvalue()
    output.close()

    filename = f"{code}_{start_date}_{end_date}.csv"

    return Response(
        content=csv_content.encode('utf-8-sig'),
        media_type='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
