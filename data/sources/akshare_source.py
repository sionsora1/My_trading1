"""
AKShare 数据源 — 免费开源金融数据接口
"""
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from datetime import datetime
import time
import random
import requests

from data.sources.base import BaseDataSource
from utils.logger import get_logger

logger = get_logger('data', 'data.log')


class AKShareDataSource(BaseDataSource):
    """AKShare 数据源 — 免费开源金融数据接口，数据丰富"""

    @property
    def name(self) -> str:
        return 'akshare'

    # ============================================================
    # 股票列表
    # ============================================================

    def get_stock_list(self) -> pd.DataFrame:
        """获取A股股票列表"""
        import akshare as ak

        df = ak.stock_info_a_code_name()
        df.columns = ['symbol', 'name']

        # 生成ts_code
        df['ts_code'] = df['symbol'].apply(
            lambda x: f"{x}.SH" if x.startswith('6') else f"{x}.SZ"
        )

        # 过滤ST和退市
        df = df[~df['name'].str.contains('ST|退市', na=False)]

        # 过滤科创板和北交所
        df = df[~df['symbol'].str.startswith(('688', '8', '4'))]

        df['industry'] = '未知'
        df['area'] = '中国'
        df['market'] = 'A股'
        df['list_date'] = ''

        return df[['ts_code', 'symbol', 'name', 'area', 'industry', 'market', 'list_date']]

    # ============================================================
    # 实时行情（东方财富API，无需token）
    # ============================================================

    def get_realtime_quotes(self, codes: List[str]) -> Dict[str, dict]:
        """
        获取实时行情快照

        Args:
            codes: 股票代码列表，如 ['600519', '002415']

        Returns:
            {ts_code: {name, close(现价), open, high, low, volume, amount, change_pct, bid1, ask1, ...}}
        """
        if not codes:
            return {}

        # 非交易时段不请求实时行情（节约请求，避免空数据）
        now = datetime.now()
        h, m, w = now.hour, now.minute, now.weekday()
        if w >= 5 or not ((h == 9 and m >= 25) or (10 <= h <= 11) or (13 <= h <= 14) or (h == 15 and m <= 5)):
            return {}

        # 构造东方财富市场代码
        em_codes = []
        code_map = {}  # em_code -> clean_code
        for code in codes:
            code = str(code).split('.')[0]
            if code.startswith('6'):
                em_code = f'1.{code}'
            else:
                em_code = f'0.{code}'
            em_codes.append(em_code)
            code_map[str(code)] = code

        # 分批请求（使用 Session 保持连接，模拟浏览器）
        results = {}
        batch_size = 50
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'http://quote.eastmoney.com/',
            'Connection': 'keep-alive',
        })

        for i in range(0, len(em_codes), batch_size):
            batch = em_codes[i:i + batch_size]
            secids = ','.join(batch)

            try:
                url = 'http://push2.eastmoney.com/api/qt/ulist.np/get'
                params = {
                    'fltt': '2',
                    'invt': '2',
                    'fields': 'f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18',
                    'secids': secids,
                    '_': int(time.time() * 1000),
                }
                resp = session.get(url, params=params, timeout=10)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                if not data.get('data') or not data['data'].get('diff'):
                    continue

                for item in data['data']['diff']:
                    em_code = item.get('f12', '')
                    ts_code = code_map.get(str(em_code))
                    if not ts_code:
                        # 尝试通过市场代码匹配
                        market = item.get('f13', 0)
                        ts_code = str(em_code)
                        if market == 1:
                            ts_code = f"SH{em_code}"
                        elif market == 0:
                            ts_code = f"SZ{em_code}"

                    clean_code = ts_code.replace('SH', '').replace('SZ', '')

                    results[clean_code] = {
                        'name': item.get('f14', ''),
                        'ts_code': clean_code,
                        'close': item.get('f2', 0) or 0,       # 最新价
                        'change_pct': item.get('f3', 0) or 0,   # 涨跌幅
                        'change': item.get('f4', 0) or 0,       # 涨跌额
                        'volume': item.get('f5', 0) or 0,       # 成交量
                        'amount': item.get('f6', 0) or 0,       # 成交额
                        'turnover': item.get('f7', 0) or 0,     # 换手率
                        'high': item.get('f15', 0) or 0,        # 最高
                        'low': item.get('f16', 0) or 0,         # 最低
                        'open': item.get('f17', 0) or 0,        # 今开
                        'pre_close': item.get('f18', 0) or 0,   # 昨收
                    }

            except Exception as e:
                logger.warning('实时行情请求失败', extra={'data': {'batch': i, 'error': str(e)}})
                continue

            # 随机间隔 0.5~1.5 秒，避免被识别为爬虫
            if i + batch_size < len(em_codes):
                time.sleep(0.5 + random.random())

        return results

    # ============================================================
    # 买卖五档盘口
    # ============================================================

    def get_order_depth(self, code: str) -> dict | None:
        """
        获取单只股票的买卖五档盘口数据

        Args:
            code: 6位股票代码，如 '600519'

        Returns:
            {code, name, price, pre_close, change_pct,
             bids: [{price, volume} x5], asks: [{price, volume} x5],
             time, status}
            或 None（非交易时段或请求失败）
        """
        import requests as req
        import random as rnd

        # 构造东方财富市场代码
        if code.startswith('6'):
            secid = f'1.{code}'
            market = 1
        else:
            secid = f'0.{code}'
            market = 0

        try:
            session = req.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': '*/*',
                'Accept-Language': 'zh-CN,zh;q=0.9',
                'Referer': 'http://quote.eastmoney.com/',
            })

            # 东方财富个股快照 API — 包含买卖五档字段
            # f19-f23: 卖一~卖五价, f24-f28: 买一~买五价
            # f39-f43: 卖一~卖五量, f44-f48: 买一~买五量
            fields = 'f2,f3,f12,f14,f15,f16,f17,f18,f19,f20,f21,f22,f23,f24,f25,f26,f27,f28,f39,f40,f41,f42,f43,f44,f45,f46,f47,f48,f86'
            url = 'http://push2.eastmoney.com/api/qt/stock/get'
            params = {
                'fltt': '2',
                'invt': '2',
                'fields': fields,
                'secid': secid,
                '_': int(time.time() * 1000),
            }

            resp = session.get(url, params=params, timeout=8)
            if resp.status_code != 200:
                return None

            data = resp.json()
            if not data.get('data'):
                return None

            d = data['data']

            # 解析买卖五档
            asks = []
            bids = []
            ask_prices = [d.get(f'f{i}') for i in (19, 20, 21, 22, 23)]
            ask_vols = [d.get(f'f{i}') for i in (39, 40, 41, 42, 43)]
            bid_prices = [d.get(f'f{i}') for i in (24, 25, 26, 27, 28)]
            bid_vols = [d.get(f'f{i}') for i in (44, 45, 46, 47, 48)]

            for i in range(5):
                ap = ask_prices[i]
                av = ask_vols[i]
                if ap and ap != '-':
                    asks.append({'price': float(ap), 'volume': int(float(av)) if av and av != '-' else 0})

                bp = bid_prices[i]
                bv = bid_vols[i]
                if bp and bp != '-':
                    bids.append({'price': float(bp), 'volume': int(float(bv)) if bv and bv != '-' else 0})

            if not asks and not bids:
                return None  # 停牌或无数据

            return {
                'code': code,
                'name': d.get('f14', ''),
                'price': d.get('f2', 0) or 0,
                'pre_close': d.get('f18', 0) or 0,
                'change_pct': d.get('f3', 0) or 0,
                'open': d.get('f17', 0) or 0,
                'high': d.get('f15', 0) or 0,
                'low': d.get('f16', 0) or 0,
                'asks': asks,
                'bids': bids,
                'time': d.get('f86', ''),
                'status': 'ok',
            }

        except Exception as e:
            logger.warning(f'盘口数据获取失败 {code}: {e}')
            return None

    # ============================================================
    # 日线行情（多数据源支持）
    # ============================================================

    def get_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        获取日线行情
        支持多个数据源，自动切换
        """
        import akshare as ak

        symbol = ts_code.split('.')[0]
        market = 'sh' if symbol.startswith('6') else 'sz'

        # 数据源1: 新浪源（stock_zh_a_daily）
        try:
            df = ak.stock_zh_a_daily(
                symbol=f"{market}{symbol}",
                start_date=start_date,
                end_date=end_date,
                adjust='qfq'
            )

            if not df.empty:
                df = df.rename(columns={
                    'date': 'trade_date',
                    'open': 'open',
                    'close': 'close',
                    'high': 'high',
                    'low': 'low',
                    'volume': 'vol',
                    'amount': 'amount',
                    'outstanding_share': 'circ_share',
                    'turnover': 'turnover_rate'
                })

                df['ts_code'] = ts_code
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.sort_values('trade_date').reset_index(drop=True)

                # 计算涨跌幅
                if 'pct_chg' not in df.columns:
                    df['pct_chg'] = df['close'].pct_change() * 100

                return df

        except Exception as e:
            logger.debug('新浪数据源失败，尝试东财源', extra={'data': {'ts_code': ts_code}})

        # 数据源2: 东财源（stock_zh_a_hist）
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period='daily',
                start_date=start_date,
                end_date=end_date,
                adjust='qfq'
            )

            if not df.empty:
                df = df.rename(columns={
                    '日期': 'trade_date',
                    '开盘': 'open',
                    '收盘': 'close',
                    '最高': 'high',
                    '最低': 'low',
                    '成交量': 'vol',
                    '成交额': 'amount',
                    '涨跌幅': 'pct_chg',
                    '涨跌额': 'change',
                    '换手率': 'turnover_rate'
                })

                df['ts_code'] = ts_code
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.sort_values('trade_date').reset_index(drop=True)

                return df

        except Exception as e:
            logger.debug('东财数据源失败，尝试腾讯源', extra={'data': {'ts_code': ts_code}})

        # 数据源3: 腾讯源
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=f"{market}{symbol}",
                start_date=start_date,
                end_date=end_date,
                adjust='qfq'
            )

            if not df.empty:
                df['ts_code'] = ts_code
                return df

        except Exception as e:
            logger.debug('腾讯数据源也失败，返回空DataFrame', extra={'data': {'ts_code': ts_code}})

        return pd.DataFrame()

    # ============================================================
    # 股票基本信息
    # ============================================================

    def get_stock_info(self, symbol: str) -> dict:
        """获取股票基本信息"""
        import akshare as ak

        # 数据源1: 东财
        try:
            df = ak.stock_individual_info_em(symbol=symbol)
            info = {}
            for _, row in df.iterrows():
                info[row['item']] = row['value']

            return {
                'name': info.get('股票简称', symbol),
                'industry': info.get('行业', '未知'),
                'market_cap': float(info.get('总市值', 0)) if info.get('总市值') else 0,
                'circ_market_cap': float(info.get('流通市值', 0)) if info.get('流通市值') else 0,
                'pe': float(info.get('市盈率(动态)', 0)) if info.get('市盈率(动态)') else 0,
                'pb': float(info.get('市净率', 0)) if info.get('市净率') else 0,
            }
        except Exception as e:
            pass

        # 数据源2: 从股票列表获取名称
        try:
            df = ak.stock_info_a_code_name()
            match = df[df['code'] == symbol]
            if not match.empty:
                return {
                    'name': match.iloc[0]['name'],
                    'industry': '未知',
                    'market_cap': 0,
                    'circ_market_cap': 0,
                    'pe': 0,
                    'pb': 0,
                }
        except Exception:
            pass

        return {
            'name': symbol,
            'industry': '未知',
            'market_cap': 0,
            'circ_market_cap': 0,
            'pe': 0,
            'pb': 0,
        }

    # ============================================================
    # 财务数据
    # ============================================================

    def get_financial_data(self, symbol: str) -> dict:
        """
        获取财务数据（基于 stock_financial_abstract）
        返回结构化的关键财务指标字典
        """
        import akshare as ak

        try:
            df = ak.stock_financial_abstract(symbol=symbol)
            if df.empty:
                return self._empty_financial()

            # stock_financial_abstract 返回: 列0=分类, 列1=指标名, 列2+=各季度数据
            # 直接用行号定位，避免中文编码匹配问题
            # AKShare stock_financial_abstract 的行号是固定的
            def row_val(row_idx):
                """获取指定行的最新季度值"""
                try:
                    v = df.iloc[row_idx, 2]  # 列2 = 最新季度
                    return float(v) if pd.notna(v) else None
                except (ValueError, TypeError, IndexError):
                    return None

            def row_yoy(row_idx):
                """同比增长率（最新期 vs 去年同期）"""
                current = row_val(row_idx)
                prev = None
                try:
                    v = df.iloc[row_idx, prev_year_col] if prev_year_col >= 2 else None
                    prev = float(v) if pd.notna(v) else None
                except (ValueError, TypeError, IndexError):
                    pass
                if current is not None and prev is not None and prev != 0:
                    return (current - prev) / abs(prev)
                return None

            # 找到去年同期列的索引
            prev_year_col = -1
            for j, col in enumerate(df.columns[2:], start=2):
                try:
                    y, rest = int(str(col)[:4]), str(col)[4:]
                    if str(y - 1) + rest == str(df.columns[2]):
                        prev_year_col = j
                        break
                except (ValueError, IndexError):
                    continue

            # Row 0: 归母净利润
            net_profit = row_val(0)
            # Row 1: 营业总收入
            revenue = row_val(1)
            # Row 3: 净利润
            net_income = row_val(3)
            # Row 5: 股东权益合计(净资产)
            net_assets = row_val(5)
            # Row 7: 经营现金流量净额
            ocf = row_val(7)
            # Row 11: 净资产收益率(ROE)
            roe = row_val(11)
            # Row 13: 毛利率
            gross_margin = row_val(13)
            # Row 54: 营业总收入增长率（百分比）
            revenue_growth_rate = row_val(54)
            # Row 55: 归属母公司净利润增长率（百分比）
            profit_growth_rate = row_val(55)

            # 如果增长率字段直接可用，优先使用；否则用 YoY 计算
            if revenue_growth_rate is not None and revenue_growth_rate != 0:
                revenue_growth = revenue_growth_rate / 100.0  # 百分比转小数
            else:
                rev_yoy = row_yoy(1)  # 营业总收入 YoY
                revenue_growth = rev_yoy if rev_yoy is not None else 0

            if profit_growth_rate is not None and profit_growth_rate != 0:
                profit_growth = profit_growth_rate / 100.0
            else:
                np_yoy = row_yoy(0)  # 归母净利润 YoY
                profit_growth = np_yoy if np_yoy is not None else 0

            # 应计比率 = (净利润 - 经营现金流) / 净资产
            if net_income and ocf and net_assets and net_assets > 0:
                accrual = (net_income - ocf) / net_assets
            else:
                accrual = 0

            # 安全转小数：百分比 / 100
            roe_val = roe / 100.0 if roe is not None else 0
            gm_val = gross_margin / 100.0 if gross_margin is not None else 0

            return {
                'roe': roe_val,
                'gross_margin': gm_val,
                'profit_growth': profit_growth,
                'revenue_growth': revenue_growth,
                'accrual_ratio': accrual if accrual is not None else 0,
                'net_assets': net_assets if net_assets is not None else 0,
                'revenue': revenue if revenue is not None else 0,
                'net_profit': net_profit if net_profit is not None else 0,
                'ocf': ocf if ocf is not None else 0,
            }

        except Exception as e:
            logger.warning('财务数据获取失败', extra={'data': {'symbol': symbol, 'error': str(e)}})
            return self._empty_financial()

    def _empty_financial(self) -> dict:
        """返回空的财务数据结构"""
        return {
            'roe': 0,
            'gross_margin': 0,
            'profit_growth': 0,
            'revenue_growth': 0,
            'accrual_ratio': 0,
            'net_assets': 0,
            'revenue': 0,
            'net_profit': 0,
            'ocf': 0,
        }

    # ============================================================
    # 资金流向
    # ============================================================

    def get_money_flow(self, symbol: str) -> pd.DataFrame:
        """获取个股资金流向"""
        import akshare as ak

        try:
            market = "sh" if symbol.startswith('6') else "sz"
            df = ak.stock_individual_fund_flow(stock=symbol, market=market)
            return df
        except Exception:
            return pd.DataFrame()

    def get_north_money_flow(self, days: int = 5) -> pd.DataFrame:
        """获取北向资金流向"""
        import akshare as ak

        try:
            df = ak.stock_hsgt_north_net_flow_in_em()
            return df.tail(days)
        except Exception:
            return pd.DataFrame()

    # ============================================================
    # 板块数据
    # ============================================================

    def get_industry_stocks(self, industry: str) -> list:
        """获取行业成分股"""
        import akshare as ak

        try:
            df = ak.stock_board_industry_cons_em(symbol=industry)
            return df['代码'].tolist()
        except Exception:
            return []

    # ============================================================
    # 分钟线行情
    # ============================================================

    @staticmethod
    def _parse_ts_code(ts_code: str) -> tuple:
        """Parse a ts_code into (symbol, market).

        Handles both '600519' and '600519.SH' / '000001.SZ' formats.
        Returns (symbol: str, market: str) where market is 'sh' or 'sz'.
        """
        symbol = str(ts_code).split('.')[0].strip()
        market = 'sh' if symbol.startswith('6') else 'sz'
        return symbol, market

    def get_minute_data(self, ts_code: str, period: str = '5',
                        start_time: str = None, end_time: str = None) -> pd.DataFrame:
        """Get historical minute-level bar data for a single stock.

        Tries the Sina source first, then falls back to Eastmoney.

        Args:
            ts_code:  Stock code in '600519' or '600519.SH' format.
            period:   Bar period in minutes ('1', '5', '15', '30', '60').
            start_time: Start datetime string, e.g. '2025-06-01 09:30:00'.
            end_time:   End datetime string.

        Returns:
            DataFrame with columns matching minute_bars table schema,
            or an empty DataFrame on failure.
        """
        import akshare as ak

        symbol, market = self._parse_ts_code(ts_code)
        df = pd.DataFrame()

        # ---- Source 1: Sina (stock_zh_a_minute) ----
        try:
            sina_symbol = f"{market}{symbol}"
            df = ak.stock_zh_a_minute(
                symbol=sina_symbol,
                period=period,
                adjust='qfq',
            )

            if not df.empty:
                # Sina returns columns: day, open, high, low, close, volume
                col_map = {}
                for col in df.columns:
                    cl = col.lower()
                    if cl in ('day', 'time', 'trade_time', 'datetime',
                              'date', 'trade_date'):
                        col_map[col] = 'trade_time'
                    elif cl in ('open',):
                        col_map[col] = 'open'
                    elif cl in ('high',):
                        col_map[col] = 'high'
                    elif cl in ('low',):
                        col_map[col] = 'low'
                    elif cl in ('close',):
                        col_map[col] = 'close'
                    elif cl in ('volume', 'vol'):
                        col_map[col] = 'volume'

                # Only rename columns we have mappings for
                rename_map = {k: v for k, v in col_map.items() if k in df.columns}
                if rename_map:
                    df = df.rename(columns=rename_map)
        except Exception:
            pass  # fall through to next source

        # ---- Source 2: Eastmoney (stock_zh_a_hist_min_em) ----
        if df.empty:
            try:
                df = ak.stock_zh_a_hist_min_em(
                    symbol=symbol,
                    period=period,
                    start_date=start_time,
                    end_date=end_time,
                    adjust='qfq',
                )

                if not df.empty:
                    # Eastmoney returns Chinese column names
                    col_map_cn = {
                        '时间': 'trade_time',
                        '开盘': 'open',
                        '最高': 'high',
                        '最低': 'low',
                        '收盘': 'close',
                        '成交量': 'volume',
                    }
                    # Also try lowercase English mappings in case AKShare normalises
                    col_map_en = {}
                    for col in df.columns:
                        cl = col.lower()
                        if cl in ('time', 'trade_time', 'datetime'):
                            col_map_en[col] = 'trade_time'
                        elif cl in ('open',):
                            col_map_en[col] = 'open'
                        elif cl in ('high',):
                            col_map_en[col] = 'high'
                        elif cl in ('low',):
                            col_map_en[col] = 'low'
                        elif cl in ('close',):
                            col_map_en[col] = 'close'
                        elif cl in ('volume', 'vol'):
                            col_map_en[col] = 'volume'

                    rename_map = {k: v for k, v in {**col_map_cn, **col_map_en}.items()
                                  if k in df.columns}
                    if rename_map:
                        df = df.rename(columns=rename_map)
            except Exception:
                pass

        if df.empty:
            return pd.DataFrame()

        # Add metadata columns
        df['ts_code'] = ts_code
        df['period'] = int(period) if period else 5

        # Ensure trade_time is a string
        if 'trade_time' in df.columns:
            df['trade_time'] = df['trade_time'].astype(str)

        return df

    def get_intraday_data(self, ts_code: str) -> pd.DataFrame:
        """Get the latest intraday tick/minute data via Eastmoney.

        Args:
            ts_code: Stock code in '600519' or '600519.SH' format.

        Returns:
            DataFrame with intraday data, or empty DataFrame on failure.
        """
        import akshare as ak

        symbol, _ = self._parse_ts_code(ts_code)
        df = pd.DataFrame()

        try:
            df = ak.stock_intraday_em(symbol=symbol)

            if not df.empty:
                # Map common column names
                col_map = {}
                for col in df.columns:
                    cl = col.lower()
                    if cl in ('time', 'trade_time', 'datetime'):
                        col_map[col] = 'trade_time'
                    elif cl in ('open',):
                        col_map[col] = 'open'
                    elif cl in ('high',):
                        col_map[col] = 'high'
                    elif cl in ('low',):
                        col_map[col] = 'low'
                    elif cl in ('close', 'price'):
                        col_map[col] = 'close'
                    elif cl in ('volume', 'vol'):
                        col_map[col] = 'volume'

                rename_map = {k: v for k, v in col_map.items() if k in df.columns}
                if rename_map:
                    df = df.rename(columns=rename_map)

                df['ts_code'] = ts_code
        except Exception:
            pass

        return df
