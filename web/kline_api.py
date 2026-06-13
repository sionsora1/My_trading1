"""
K线可视化 API — 股票K线图 + 策略买卖点 + 收益分析
"""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import SQLiteManager

router = APIRouter(prefix="/api/kline", tags=["kline"])


# ============================================================
# 辅助函数：计算技术指标
# ============================================================

def compute_indicators(bars: list[dict]) -> list[dict]:
    """在K线数据上计算MA、收益率等指标"""
    closes = [b['close'] for b in bars]
    volumes = [b['volume'] for b in bars]

    for i, bar in enumerate(bars):
        # MA5
        if i >= 4:
            bar['ma5'] = sum(closes[i-4:i+1]) / 5
        else:
            bar['ma5'] = None

        # MA10
        if i >= 9:
            bar['ma10'] = sum(closes[i-9:i+1]) / 10
        else:
            bar['ma10'] = None

        # MA20
        if i >= 19:
            bar['ma20'] = sum(closes[i-19:i+1]) / 20
        else:
            bar['ma20'] = None

        # MA60
        if i >= 59:
            bar['ma60'] = sum(closes[i-59:i+1]) / 60
        else:
            bar['ma60'] = None

        # Volume MA20
        if i >= 19:
            bar['vol_ma20'] = sum(volumes[i-19:i+1]) / 20
        else:
            bar['vol_ma20'] = None

        # 20日收益率
        if i >= 19 and closes[i-19] > 0:
            bar['return_20d'] = (closes[i] - closes[i-19]) / closes[i-19]
        else:
            bar['return_20d'] = None

        # 5日收益率
        if i >= 4 and closes[i-4] > 0:
            bar['return_5d'] = (closes[i] - closes[i-4]) / closes[i-4]
        else:
            bar['return_5d'] = None

    return bars


# ============================================================
# 策略信号生成
# ============================================================

def generate_momentum_signals(bars: list[dict]) -> dict:
    """
    动量策略 — 单只股票版本

    买入: 20日收益 > 5% 且 价格 > MA20（上升趋势确认）
    卖出: 价格 < MA20（趋势走弱）或 20日收益 < -5%
    """
    signals = []
    in_position = False
    entry_price = 0
    entry_date = ""
    trades = []

    for i, bar in enumerate(bars):
        if bar['ma20'] is None or bar['return_20d'] is None:
            continue

        close = bar['close']
        ma20 = bar['ma20']
        ret_20d = bar['return_20d']
        date = bar['trade_date']

        signal = None

        if not in_position:
            # 买入条件：20日动量>5% 且 价格在MA20上方
            if ret_20d > 0.05 and close > ma20:
                signal = 'BUY'
                in_position = True
                entry_price = close
                entry_date = date
        else:
            # 卖出条件：跌破MA20 或 动量转负<-5%
            if close < ma20 or ret_20d < -0.05:
                signal = 'SELL'
                in_position = False
                pnl_pct = (close - entry_price) / entry_price
                trades.append({
                    'entry_date': entry_date,
                    'entry_price': round(entry_price, 2),
                    'exit_date': date,
                    'exit_price': round(close, 2),
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'days': (int(date) - int(entry_date)),
                })

        if signal:
            signals.append({
                'date': date,
                'signal': signal,
                'price': round(close, 2),
                'reason': (
                    f'动量{ret_20d:.1%}，价格高于MA20'
                    if signal == 'BUY'
                    else f'价格{"跌破MA20" if close < ma20 else "动量转负"}'
                ),
            })

    # 如果最后还在持仓，以最后一天价格平仓
    if in_position and bars:
        last_close = bars[-1]['close']
        last_date = bars[-1]['trade_date']
        pnl_pct = (last_close - entry_price) / entry_price
        trades.append({
            'entry_date': entry_date,
            'entry_price': round(entry_price, 2),
            'exit_date': last_date,
            'exit_price': round(last_close, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'days': (int(last_date) - int(entry_date)),
        })
        signals.append({
            'date': last_date,
            'signal': 'SELL',
            'price': round(last_close, 2),
            'reason': '回测结束，强制平仓',
        })

    return {
        'signals': signals,
        'trades': trades,
        'metrics': _calc_metrics(trades),
    }


def generate_trend_following_signals(bars: list[dict]) -> dict:
    """
    趋势跟随策略 — 单只股票版本

    买入: MA5 > MA20 > MA60（多头排列）且 价格 > MA20
    卖出: 价格 < MA20 * 0.98（跌破MA20缓冲2%）或 MA5 < MA60（趋势破坏）
    """
    signals = []
    in_position = False
    entry_price = 0
    entry_date = ""
    trades = []

    for i, bar in enumerate(bars):
        if bar['ma5'] is None or bar['ma20'] is None or bar['ma60'] is None:
            continue

        close = bar['close']
        ma5 = bar['ma5']
        ma20 = bar['ma20']
        ma60 = bar['ma60']
        date = bar['trade_date']

        # 多头排列: MA5 > MA20 > MA60
        bull_alignment = ma5 > ma20 > ma60
        # 价格在MA20上方
        above_ma20 = close > ma20

        signal = None
        reason = ""

        if not in_position:
            # 买入条件：多头排列 + 价格>MA20
            if bull_alignment and above_ma20:
                signal = 'BUY'
                reason = '多头排列，价格高于MA20'
                in_position = True
                entry_price = close
                entry_date = date
        else:
            # 卖出条件：跌破MA20*0.98 或 趋势破坏(MA5<MA60)
            stop_line = ma20 * 0.98
            if close < stop_line or ma5 < ma60:
                signal = 'SELL'
                reason_parts = []
                if close < stop_line:
                    reason_parts.append('跌破MA20缓冲')
                if ma5 < ma60:
                    reason_parts.append('趋势破坏')
                reason = ' / '.join(reason_parts)
                in_position = False
                pnl_pct = (close - entry_price) / entry_price
                trades.append({
                    'entry_date': entry_date,
                    'entry_price': round(entry_price, 2),
                    'exit_date': date,
                    'exit_price': round(close, 2),
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'days': (int(date) - int(entry_date)),
                })

        if signal:
            signals.append({
                'date': date,
                'signal': signal,
                'price': round(close, 2),
                'reason': reason,
            })

    # 如果最后还在持仓，以最后一天价格平仓
    if in_position and bars:
        last_close = bars[-1]['close']
        last_date = bars[-1]['trade_date']
        pnl_pct = (last_close - entry_price) / entry_price
        trades.append({
            'entry_date': entry_date,
            'entry_price': round(entry_price, 2),
            'exit_date': last_date,
            'exit_price': round(last_close, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'days': (int(last_date) - int(entry_date)),
        })
        signals.append({
            'date': last_date,
            'signal': 'SELL',
            'price': round(last_close, 2),
            'reason': '回测结束，强制平仓',
        })

    return {
        'signals': signals,
        'trades': trades,
        'metrics': _calc_metrics(trades),
    }


def generate_mean_reversion_signals(bars: list[dict]) -> dict:
    """
    均值回归策略 — 单只股票版本

    买入: 20日跌幅 > 10%（超跌）且价格 < MA20
    卖出: 价格回归MA20上方 或 获利 > 10%
    """
    signals = []
    in_position = False
    entry_price = 0
    entry_date = ""
    trades = []

    for i, bar in enumerate(bars):
        if bar['ma20'] is None or bar['return_20d'] is None:
            continue

        close = bar['close']
        ma20 = bar['ma20']
        ret_20d = bar['return_20d']
        date = bar['trade_date']

        signal = None
        reason = ""

        if not in_position:
            # 买入条件：超跌（20日跌>10%）且价格在MA20下方
            if ret_20d < -0.10 and close < ma20:
                signal = 'BUY'
                reason = f'超跌反弹信号（20日跌幅{ret_20d:.1%}）'
                in_position = True
                entry_price = close
                entry_date = date
        else:
            # 卖出条件：回归MA20 或 获利>10%
            pnl_pct = (close - entry_price) / entry_price
            if close > ma20 or pnl_pct > 0.10:
                signal = 'SELL'
                reason = f'回归均线' if close > ma20 else f'获利了结（+{pnl_pct:.1%}）'
                in_position = False
                trades.append({
                    'entry_date': entry_date,
                    'entry_price': round(entry_price, 2),
                    'exit_date': date,
                    'exit_price': round(close, 2),
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'days': (int(date) - int(entry_date)),
                })

        if signal:
            signals.append({
                'date': date,
                'signal': signal,
                'price': round(close, 2),
                'reason': reason,
            })

    if in_position and bars:
        last_close = bars[-1]['close']
        last_date = bars[-1]['trade_date']
        pnl_pct = (last_close - entry_price) / entry_price
        trades.append({
            'entry_date': entry_date,
            'entry_price': round(entry_price, 2),
            'exit_date': last_date,
            'exit_price': round(last_close, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'days': (int(last_date) - int(entry_date)),
        })
        signals.append({
            'date': last_date,
            'signal': 'SELL',
            'price': round(last_close, 2),
            'reason': '回测结束，强制平仓',
        })

    return {
        'signals': signals,
        'trades': trades,
        'metrics': _calc_metrics(trades),
    }


def generate_ma_crossover_signals(bars: list[dict]) -> dict:
    """
    均线交叉策略 — 单只股票版本

    买入: MA5上穿MA20（金叉），且价格>MA60（长期趋势向上）
    卖出: MA5下穿MA20（死叉）
    """
    signals = []
    in_position = False
    entry_price = 0
    entry_date = ""
    trades = []

    prev_ma5 = None
    prev_ma20 = None

    for i, bar in enumerate(bars):
        if bar['ma5'] is None or bar['ma20'] is None or bar['ma60'] is None:
            prev_ma5 = bar['ma5']
            prev_ma20 = bar['ma20']
            continue

        close = bar['close']
        ma5 = bar['ma5']
        ma20 = bar['ma20']
        ma60 = bar['ma60']
        date = bar['trade_date']

        signal = None
        reason = ""

        # 金叉：MA5上穿MA20
        golden_cross = prev_ma5 is not None and prev_ma20 is not None and prev_ma5 <= prev_ma20 and ma5 > ma20
        # 死叉：MA5下穿MA20
        death_cross = prev_ma5 is not None and prev_ma20 is not None and prev_ma5 >= prev_ma20 and ma5 < ma20

        if not in_position:
            if golden_cross and close > ma60:
                signal = 'BUY'
                reason = f'金叉买入（MA5上穿MA20）'
                in_position = True
                entry_price = close
                entry_date = date
        else:
            if death_cross:
                signal = 'SELL'
                pnl_pct = (close - entry_price) / entry_price
                reason = f'死叉卖出（MA5下穿MA20）'
                in_position = False
                trades.append({
                    'entry_date': entry_date,
                    'entry_price': round(entry_price, 2),
                    'exit_date': date,
                    'exit_price': round(close, 2),
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'days': (int(date) - int(entry_date)),
                })

        if signal:
            signals.append({
                'date': date,
                'signal': signal,
                'price': round(close, 2),
                'reason': reason,
            })

        prev_ma5 = ma5
        prev_ma20 = ma20

    if in_position and bars:
        last_close = bars[-1]['close']
        last_date = bars[-1]['trade_date']
        pnl_pct = (last_close - entry_price) / entry_price
        trades.append({
            'entry_date': entry_date,
            'entry_price': round(entry_price, 2),
            'exit_date': last_date,
            'exit_price': round(last_close, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'days': (int(last_date) - int(entry_date)),
        })
        signals.append({
            'date': last_date,
            'signal': 'SELL',
            'price': round(last_close, 2),
            'reason': '回测结束，强制平仓',
        })

    return {
        'signals': signals,
        'trades': trades,
        'metrics': _calc_metrics(trades),
    }


def generate_breakout_signals(bars: list[dict]) -> dict:
    """
    突破策略 — 单只股票版本

    买入: 价格突破20日最高价（放量突破）
    卖出: 价格跌破20日最低价 或 跌破MA20
    """
    signals = []
    in_position = False
    entry_price = 0
    entry_date = ""
    trades = []

    for i, bar in enumerate(bars):
        if bar['ma20'] is None or i < 20:
            continue

        close = bar['close']
        volume = bar['volume']
        ma20 = bar['ma20']
        date = bar['trade_date']

        # 计算20日最高价和最低价
        high_20d = max(b['high'] for b in bars[i-19:i+1])
        low_20d = min(b['low'] for b in bars[i-19:i+1])
        vol_ma20 = bar.get('vol_ma20', volume)

        signal = None
        reason = ""

        if not in_position:
            # 买入条件：突破20日高点 + 放量确认
            if close >= high_20d * 0.995 and volume > vol_ma20 * 1.2:
                signal = 'BUY'
                reason = f'突破20日高点{high_20d:.2f}，放量确认'
                in_position = True
                entry_price = close
                entry_date = date
        else:
            # 卖出条件：跌破20日低点 或 跌破MA20
            if close < low_20d or close < ma20 * 0.97:
                signal = 'SELL'
                pnl_pct = (close - entry_price) / entry_price
                reason = '跌破20日低点' if close < low_20d else '跌破MA20支撑'
                in_position = False
                trades.append({
                    'entry_date': entry_date,
                    'entry_price': round(entry_price, 2),
                    'exit_date': date,
                    'exit_price': round(close, 2),
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'days': (int(date) - int(entry_date)),
                })

        if signal:
            signals.append({
                'date': date,
                'signal': signal,
                'price': round(close, 2),
                'reason': reason,
            })

    if in_position and bars:
        last_close = bars[-1]['close']
        last_date = bars[-1]['trade_date']
        pnl_pct = (last_close - entry_price) / entry_price
        trades.append({
            'entry_date': entry_date,
            'entry_price': round(entry_price, 2),
            'exit_date': last_date,
            'exit_price': round(last_close, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'days': (int(last_date) - int(entry_date)),
        })
        signals.append({
            'date': last_date,
            'signal': 'SELL',
            'price': round(last_close, 2),
            'reason': '回测结束，强制平仓',
        })

    return {
        'signals': signals,
        'trades': trades,
        'metrics': _calc_metrics(trades),
    }


def generate_low_volatility_signals(bars: list[dict]) -> dict:
    """
    低波动策略 — 单只股票版本

    买入: 波动率低（20日振幅<3%）+ 价格在MA60上方（长期趋势好）
    卖出: 波动率突然放大（>5%）或 跌破MA60
    """
    signals = []
    in_position = False
    entry_price = 0
    entry_date = ""
    trades = []

    for i, bar in enumerate(bars):
        if bar['ma20'] is None or bar['ma60'] is None or i < 20:
            continue

        close = bar['close']
        ma60 = bar['ma60']
        date = bar['trade_date']

        # 计算20日波动率（日均振幅/收盘价）
        avg_volatility = 0
        for j in range(i-19, i+1):
            b = bars[j]
            if b['close'] > 0:
                avg_volatility += (b['high'] - b['low']) / b['close']
        avg_volatility /= 20

        signal = None
        reason = ""

        if not in_position:
            # 买入条件：低波动(<3%) + 长期趋势向上(>MA60)
            if avg_volatility < 0.03 and close > ma60:
                signal = 'BUY'
                reason = f'低波动({avg_volatility:.1%})，趋势向上'
                in_position = True
                entry_price = close
                entry_date = date
        else:
            # 卖出条件：波动放大(>5%) 或 跌破MA60
            if avg_volatility > 0.05 or close < ma60:
                signal = 'SELL'
                pnl_pct = (close - entry_price) / entry_price
                reason = '波动率放大' if avg_volatility > 0.05 else '跌破MA60长期支撑'
                in_position = False
                trades.append({
                    'entry_date': entry_date,
                    'entry_price': round(entry_price, 2),
                    'exit_date': date,
                    'exit_price': round(close, 2),
                    'pnl_pct': round(pnl_pct * 100, 2),
                    'days': (int(date) - int(entry_date)),
                })

        if signal:
            signals.append({
                'date': date,
                'signal': signal,
                'price': round(close, 2),
                'reason': reason,
            })

    if in_position and bars:
        last_close = bars[-1]['close']
        last_date = bars[-1]['trade_date']
        pnl_pct = (last_close - entry_price) / entry_price
        trades.append({
            'entry_date': entry_date,
            'entry_price': round(entry_price, 2),
            'exit_date': last_date,
            'exit_price': round(last_close, 2),
            'pnl_pct': round(pnl_pct * 100, 2),
            'days': (int(last_date) - int(entry_date)),
        })
        signals.append({
            'date': last_date,
            'signal': 'SELL',
            'price': round(last_close, 2),
            'reason': '回测结束，强制平仓',
        })

    return {
        'signals': signals,
        'trades': trades,
        'metrics': _calc_metrics(trades),
    }


def _calc_metrics(trades: list[dict]) -> dict:
    """根据交易记录计算绩效指标"""
    if not trades:
        return {
            'total_trades': 0,
            'win_trades': 0,
            'loss_trades': 0,
            'win_rate': 0,
            'avg_return': 0,
            'total_return': 0,
            'max_return': 0,
            'min_return': 0,
            'avg_hold_days': 0,
        }

    wins = [t for t in trades if t['pnl_pct'] > 0]
    losses = [t for t in trades if t['pnl_pct'] <= 0]
    pnls = [t['pnl_pct'] for t in trades]
    days = [t['days'] for t in trades]

    # 累计收益（复利）
    cumulative = 1.0
    for pnl in pnls:
        cumulative *= (1 + pnl / 100)
    total_return = (cumulative - 1) * 100

    return {
        'total_trades': len(trades),
        'win_trades': len(wins),
        'loss_trades': len(losses),
        'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
        'avg_return': round(sum(pnls) / len(pnls), 2) if pnls else 0,
        'total_return': round(total_return, 2),
        'max_return': round(max(pnls), 2) if pnls else 0,
        'min_return': round(min(pnls), 2) if pnls else 0,
        'avg_hold_days': round(sum(days) / len(days), 1) if days else 0,
    }


# ============================================================
# 策略注册
# ============================================================

STRATEGY_FUNCTIONS = {
    'momentum': {
        'name': '动量策略',
        'description': '基于20日收益率和MA20趋势：买入强势股（20日涨>5%），卖出弱势股',
        'func': generate_momentum_signals,
    },
    'trend_following': {
        'name': '趋势跟随策略',
        'description': 'MA5/MA20/MA60多头排列顺势而为，趋势破坏时退出',
        'func': generate_trend_following_signals,
    },
    'mean_reversion': {
        'name': '均值回归策略',
        'description': '超跌反弹：20日跌>10%时买入，回归MA20或获利>10%卖出',
        'func': generate_mean_reversion_signals,
    },
    'ma_crossover': {
        'name': '均线交叉策略',
        'description': 'MA5上穿MA20金叉买入，MA5下穿MA20死叉卖出（需价格>MA60）',
        'func': generate_ma_crossover_signals,
    },
    'breakout': {
        'name': '突破策略',
        'description': '放量突破20日高点买入，跌破20日低点或MA20支撑卖出',
        'func': generate_breakout_signals,
    },
    'low_volatility': {
        'name': '低波动策略',
        'description': '低波动（日均振幅<3%）+长期趋势向上时买入，波动放大或破位卖出',
        'func': generate_low_volatility_signals,
    },
}


# ============================================================
# API 端点
# ============================================================

@router.get("/stocks")
def get_kline_stocks():
    """获取数据库中所有股票列表（含名称、行业）"""
    db = SQLiteManager()
    codes = db.get_all_stock_codes()
    stocks = []
    for code in codes:
        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
        info = db.get_stock_info(ts_code)
        stocks.append({
            'code': code,
            'ts_code': ts_code,
            'name': info.get('name', code) if info else code,
            'industry': info.get('industry', '') if info else '',
        })
    db.close()
    return {'stocks': stocks, 'total': len(stocks)}


@router.get("/strategies")
def get_strategies():
    """获取可用的策略列表"""
    return {
        'strategies': [
            {'key': k, 'name': v['name'], 'description': v['description']}
            for k, v in STRATEGY_FUNCTIONS.items()
        ]
    }


@router.get("/data/{code}")
def get_kline_data(
    code: str,
    start_date: str = Query(default='20240101', description='开始日期 YYYYMMDD'),
    end_date: str = Query(default='20251231', description='结束日期 YYYYMMDD'),
):
    """获取单只股票的K线数据（OHLCV + 技术指标）"""
    db = SQLiteManager()
    ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

    # 验证股票存在
    info = db.get_stock_info(ts_code)
    if not info:
        db.close()
        raise HTTPException(status_code=404, detail=f"股票 {code} 不在数据库中")

    bars = db.get_daily_bars(ts_code, start_date, end_date)
    db.close()

    if not bars:
        return {
            'code': code,
            'ts_code': ts_code,
            'name': info.get('name', code),
            'bars': [],
            'count': 0,
        }

    # 计算技术指标
    bars = compute_indicators(bars)

    # 格式化输出（只保留前端需要的字段，减少传输量）
    result_bars = []
    for b in bars:
        result_bars.append({
            'date': b['trade_date'],
            'open': round(b['open'], 2),
            'high': round(b['high'], 2),
            'low': round(b['low'], 2),
            'close': round(b['close'], 2),
            'volume': b['volume'],
            'pct_chg': round(b.get('pct_chg', 0) or 0, 2),
            'ma5': round(b['ma5'], 2) if b['ma5'] else None,
            'ma10': round(b['ma10'], 2) if b['ma10'] else None,
            'ma20': round(b['ma20'], 2) if b['ma20'] else None,
            'ma60': round(b['ma60'], 2) if b['ma60'] else None,
        })

    return {
        'code': code,
        'ts_code': ts_code,
        'name': info.get('name', code),
        'industry': info.get('industry', ''),
        'bars': result_bars,
        'count': len(result_bars),
    }


class StrategyRequest(BaseModel):
    code: str
    strategy: str = 'momentum'  # 'momentum' | 'trend_following'
    start_date: str = '20240101'
    end_date: str = '20251231'


@router.post("/strategy")
def compute_strategy_signals(req: StrategyRequest):
    """
    对单只股票运行策略，返回K线数据 + 买卖信号 + 收益指标

    支持策略:
    - momentum: 动量策略（20日收益 + MA20趋势）
    - trend_following: 趋势跟随策略（MA多头排列）
    """
    if req.strategy not in STRATEGY_FUNCTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"未知策略 '{req.strategy}'，可选: {list(STRATEGY_FUNCTIONS.keys())}"
        )

    db = SQLiteManager()
    ts_code = f"{req.code}.SH" if req.code.startswith('6') else f"{req.code}.SZ"

    info = db.get_stock_info(ts_code)
    if not info:
        db.close()
        raise HTTPException(status_code=404, detail=f"股票 {req.code} 不在数据库中")

    bars = db.get_daily_bars(ts_code, req.start_date, req.end_date)
    db.close()

    if not bars:
        raise HTTPException(status_code=404, detail=f"股票 {req.code} 在指定日期范围内无数据")

    # 计算技术指标
    bars = compute_indicators(bars)

    # 运行策略
    strategy_info = STRATEGY_FUNCTIONS[req.strategy]
    result = strategy_info['func'](bars)

    # 格式化K线数据
    result_bars = []
    for b in bars:
        result_bars.append({
            'date': b['trade_date'],
            'open': round(b['open'], 2),
            'high': round(b['high'], 2),
            'low': round(b['low'], 2),
            'close': round(b['close'], 2),
            'volume': b['volume'],
            'pct_chg': round(b.get('pct_chg', 0) or 0, 2),
            'ma5': round(b['ma5'], 2) if b['ma5'] else None,
            'ma10': round(b['ma10'], 2) if b['ma10'] else None,
            'ma20': round(b['ma20'], 2) if b['ma20'] else None,
            'ma60': round(b['ma60'], 2) if b['ma60'] else None,
        })

    return {
        'code': req.code,
        'ts_code': ts_code,
        'name': info.get('name', req.code),
        'industry': info.get('industry', ''),
        'strategy': req.strategy,
        'strategy_name': strategy_info['name'],
        'bars': result_bars,
        'signals': result['signals'],
        'trades': result['trades'],
        'metrics': result['metrics'],
    }
