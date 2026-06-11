"""
数据获取模块
使用AKShare（完全免费，无需注册，无需token）
支持多个数据源，自动切换
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import os
import json
import random
import requests

from config.settings import DATA_CACHE_DIR


class DataFetcher:
    """A股数据获取器（基于AKShare，支持多数据源）"""

    def __init__(self):
        os.makedirs(DATA_CACHE_DIR, exist_ok=True)
        print("[DataFetcher] AKShare数据源初始化成功")

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

    def get_realtime_quotes(self, stock_pool: list) -> dict:
        """
        获取实时行情快照

        Args:
            stock_pool: 股票代码列表，如 ['600519', '002415']

        Returns:
            {ts_code: {name, close(现价), open, high, low, volume, amount, change_pct, bid1, ask1, ...}}
        """
        if not stock_pool:
            return {}

        # 非交易时段不请求实时行情（节约请求，避免空数据）
        now = datetime.now()
        h, m, w = now.hour, now.minute, now.weekday()
        if w >= 5 or not ((h == 9 and m >= 25) or (10 <= h <= 11) or (13 <= h <= 14) or (h == 15 and m <= 5)):
            return {}

        # 构造东方财富市场代码
        codes = []
        code_map = {}  # code -> code
        for code in stock_pool:
            code = str(code).split('.')[0]
            if code.startswith('6'):
                em_code = f'1.{code}'
            else:
                em_code = f'0.{code}'
            codes.append(em_code)
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

        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
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
                print(f"[DataFetcher] 实时行情请求失败 (batch {i}): {e}")
                continue

            # 随机间隔 0.5~1.5 秒，避免被识别为爬虫
            if i + batch_size < len(codes):
                time.sleep(0.5 + random.random())

        return results

    def build_realtime_market_data(self, stock_pool: list) -> dict:
        """
        构建实时行情数据，格式兼容 build_market_data_by_date

        Returns:
            {today_date: {ts_code: {close, name, open, high, low, volume, ...}}}
        """
        quotes = self.get_realtime_quotes(stock_pool)
        today = datetime.now().strftime('%Y%m%d')

        if not quotes:
            return {}

        return {today: quotes}

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
            pass  # 尝试下一个数据源

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
            pass  # 尝试下一个数据源

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
            pass

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
                return {'name': match.iloc[0]['name'], 'industry': '未知'}
        except:
            pass

        return {'name': symbol, 'industry': '未知'}

    # ============================================================
    # 财务数据
    # ============================================================

    def get_financial_data(self, symbol: str) -> pd.DataFrame:
        """获取财务数据"""
        import akshare as ak

        try:
            df = ak.stock_financial_analysis_indicator(symbol=symbol)
            if not df.empty:
                df = df.rename(columns={
                    '日期': 'end_date',
                    '净资产收益率': 'roe',
                    '销售毛利率': 'grossprofit_margin',
                })
                return df
        except:
            pass

        return pd.DataFrame()

    def calculate_growth(self, financial_df: pd.DataFrame) -> dict:
        """计算成长指标"""
        if financial_df.empty:
            return {'profit_growth': 0, 'revenue_growth': 0}
        return {'profit_growth': 0.1, 'revenue_growth': 0.08}

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
        except:
            return pd.DataFrame()

    def get_north_money_flow(self, days: int = 5) -> pd.DataFrame:
        """获取北向资金流向"""
        import akshare as ak

        try:
            df = ak.stock_hsgt_north_net_flow_in_em()
            return df.tail(days)
        except:
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
        except:
            return []

    # ============================================================
    # 技术指标计算
    # ============================================================

    def calculate_ma(self, df: pd.DataFrame, windows=None) -> pd.DataFrame:
        """计算均线"""
        if windows is None:
            windows = [5, 10, 20, 60]
        for w in windows:
            df[f'ma{w}'] = df['close'].rolling(window=w).mean()
        return df

    def calculate_volume_ma(self, df: pd.DataFrame, windows=None) -> pd.DataFrame:
        """计算成交量均线"""
        if windows is None:
            windows = [5, 10, 20]
        for w in windows:
            df[f'volume_ma{w}'] = df['vol'].rolling(window=w).mean()
        return df

    def calculate_returns(self, df: pd.DataFrame, periods=None) -> pd.DataFrame:
        """计算收益率"""
        if periods is None:
            periods = [1, 5, 10, 20, 60]
        for p in periods:
            df[f'return_{p}d'] = df['close'].pct_change(p)
        return df

    def calculate_volatility(self, df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
        """计算波动率"""
        df[f'volatility_{window}d'] = df['close'].pct_change().rolling(window=window).std()
        return df

    def calculate_price_percentile(self, df: pd.DataFrame, window: int = 250) -> pd.DataFrame:
        """计算价格在近N日的分位数"""
        def percentile_rank(x):
            if len(x) < 20:
                return 0.5
            current = x.iloc[-1]
            return (x < current).sum() / len(x)

        df['price_percentile_1y'] = df['close'].rolling(window=min(window, len(df))).apply(
            percentile_rank, raw=False
        ).fillna(0.5)
        return df

    # ============================================================
    # 数据整合
    # ============================================================

    def build_stock_data(self, ts_code: str, lookback_days: int = 300) -> dict:
        """构建单只股票的完整数据"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y%m%d')

        symbol = ts_code.split('.')[0]

        # 获取日线数据
        daily = self.get_daily_data(ts_code, start_date, end_date)
        if daily.empty:
            return {}

        # 计算技术指标
        daily = self.calculate_ma(daily)
        daily = self.calculate_volume_ma(daily)
        daily = self.calculate_returns(daily)
        daily = self.calculate_volatility(daily)
        daily = self.calculate_price_percentile(daily)

        latest = daily.iloc[-1]

        # 获取股票信息
        info = self.get_stock_info(symbol)

        # 获取财务数据
        financial = self.get_financial_data(symbol)
        growth = self.calculate_growth(financial)

        pe = info.get('pe', 20)
        pb = info.get('pb', 3)
        market_cap = info.get('market_cap', 1e10)
        turnover = latest.get('turnover_rate', 3)

        stock_data = {
            'ts_code': ts_code,
            'code': symbol,
            'close': latest['close'],
            'volume': latest['vol'],
            'market_cap': market_cap,
            'ma5': latest.get('ma5', latest['close']),
            'ma10': latest.get('ma10', latest['close']),
            'ma20': latest.get('ma20', latest['close']),
            'ma60': latest.get('ma60', latest['close']),
            'high_1y': daily['high'].max(),
            'low_1y': daily['low'].min(),
            'price_percentile_1y': latest.get('price_percentile_1y', 0.5),
            'volume_ma20': latest.get('volume_ma20', latest['vol']),
            'turnover': turnover,
            'pe': pe,
            'pb': pb,
            'pe_percentile_5y': 0.5,
            'roe': 0.15,
            'ep': 1 / pe if pe > 0 else 0.05,
            'profit_growth': growth['profit_growth'],
            'revenue_growth': growth['revenue_growth'],
            'gross_margin': 0.30,
            'accrual_ratio': 0.02,
            'pledge_ratio': 0.10,
            'return_1d': latest.get('return_1d', 0),
            'return_20d': latest.get('return_20d', 0),
            'return_60d': latest.get('return_60d', 0),
            'volatility': latest.get('volatility_20d', 0.25),
            'policy_benefit': False,
            'analyst_upgrade': False,
            'insider_buying': False,
            'buyback': False,
            'st_flag': False,
            'main_force_net_3d': 0,
            'northbound_net_3d': 0,
            'industry': info.get('industry', '未知'),
            'name': info.get('name', symbol),
        }

        return stock_data

    def build_market_data_by_date(self, stock_codes: list, start_date: str, end_date: str) -> dict:
        """
        构建按日期索引的市场数据（用于回测）

        关键：获取回测区间前120天的数据，用于计算ma60等技术指标
        """
        from datetime import datetime, timedelta

        market_data_by_date = {}
        stock_info_cache = {}

        # 计算扩展的开始日期（前120天，确保有足够数据计算ma60）
        start_dt = datetime.strptime(start_date, '%Y%m%d')
        extended_start = (start_dt - timedelta(days=180)).strftime('%Y%m%d')

        print(f"  获取 {len(stock_codes)} 只股票的历史数据...")
        print(f"  数据区间: {extended_start} ~ {end_date}（含前导数据）")

        for i, code in enumerate(stock_codes):
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

            try:
                # 获取扩展的日线数据（包含前导数据）
                daily = self.get_daily_data(ts_code, extended_start, end_date)
                if daily.empty:
                    print(f"  [{i+1}/{len(stock_codes)}] {code}: 无数据，跳过")
                    continue

                # 获取股票信息（缓存）
                if code not in stock_info_cache:
                    stock_info_cache[code] = self.get_stock_info(code)
                    time.sleep(0.3)

                info = stock_info_cache[code]
                name = info.get('name', code)
                industry = info.get('industry', '未知')

                # 计算技术指标
                daily = self.calculate_ma(daily)
                daily = self.calculate_volume_ma(daily)
                daily = self.calculate_returns(daily)
                daily = self.calculate_volatility(daily)
                daily = self.calculate_price_percentile(daily)

                # 计算滚动的1年最高/最低价
                daily['rolling_high_1y'] = daily['high'].rolling(window=min(250, len(daily)), min_periods=1).max()
                daily['rolling_low_1y'] = daily['low'].rolling(window=min(250, len(daily)), min_periods=1).min()

                # 只保留在回测区间内的数据
                daily_backtest = daily[daily['trade_date'] >= pd.to_datetime(start_date)]

                # 转换为回测格式
                for idx, row in daily_backtest.iterrows():
                    date = row['trade_date'].strftime('%Y%m%d')

                    if date not in market_data_by_date:
                        market_data_by_date[date] = {}

                    close = row['close']

                    # 处理NaN值
                    def safe_val(val, default):
                        if pd.isna(val):
                            return default
                        return val

                    market_data_by_date[date][code] = {
                        'ts_code': code,
                        'code': code,
                        'close': close,
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'volume': row['vol'],
                        'prev_close': close / (1 + row['pct_chg'] / 100) if 'pct_chg' in row and not pd.isna(row['pct_chg']) and row['pct_chg'] != -100 else close,
                        'trade_date': date,
                        'ma5': safe_val(row.get('ma5'), close),
                        'ma10': safe_val(row.get('ma10'), close),
                        'ma20': safe_val(row.get('ma20'), close),
                        'ma60': safe_val(row.get('ma60'), close),
                        'high_1y': safe_val(row.get('rolling_high_1y'), close),
                        'low_1y': safe_val(row.get('rolling_low_1y'), close),
                        'price_percentile_1y': safe_val(row.get('price_percentile_1y'), 0.5),
                        'pe_percentile_5y': 0.5,
                        'volume_ma20': safe_val(row.get('volume_ma20'), row['vol']),
                        'turnover': safe_val(row.get('turnover_rate'), 3),
                        'pe': info.get('pe', 20),
                        'pb': info.get('pb', 3),
                        'ep': 1 / info.get('pe', 20) if info.get('pe', 20) > 0 else 0.05,
                        'roe': 0.15,
                        'profit_growth': 0.10,
                        'revenue_growth': 0.08,
                        'gross_margin': 0.30,
                        'accrual_ratio': 0.02,
                        'pledge_ratio': 0.10,
                        'return_1d': safe_val(row.get('return_1d'), 0),
                        'return_20d': safe_val(row.get('return_20d'), 0),
                        'return_60d': safe_val(row.get('return_60d'), 0),
                        'volatility': safe_val(row.get('volatility_20d'), 0.25),
                        'market_cap': info.get('market_cap', 1e10),
                        'policy_benefit': False,
                        'analyst_upgrade': False,
                        'insider_buying': False,
                        'buyback': False,
                        'st_flag': False,
                        'main_force_net_3d': 0,
                        'northbound_net_3d': 0,
                        'industry': industry,
                        'name': name,
                    }

                print(f"  [{i+1}/{len(stock_codes)}] {code} {name}: {len(daily_backtest)}条数据")

                time.sleep(0.5)  # 限速

            except Exception as e:
                print(f"  [{i+1}/{len(stock_codes)}] {code}: 失败 - {e}")
                continue

        print(f"  共获取 {len(market_data_by_date)} 个交易日的数据")
        return market_data_by_date


class DataCache:
    """数据缓存管理"""

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or DATA_CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)

    def save_market_data(self, data, filename: str = 'market_data'):
        """保存市场数据"""
        filepath = f"{self.cache_dir}/{filename}.json"
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        print(f"数据已保存: {filepath}")

    def load_market_data(self, filename: str = 'market_data'):
        """加载市场数据"""
        filepath = f"{self.cache_dir}/{filename}.json"
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return None