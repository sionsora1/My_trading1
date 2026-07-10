"""
数据获取模块 — 门面层
支持多数据源（TDX / AKShare），自动回退
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
import os
import json
import random
import requests

from config.settings import DATA_CACHE_DIR, DATA_SOURCE_CONFIG
from data.sources import get_data_source
from utils.logger import get_logger

logger = get_logger('data', 'data.log')


class DataFetcher:
    """A股数据获取器（门面模式 — 支持多数据源自动回退）"""

    @staticmethod
    def _parse_ts_code(ts_code: str) -> tuple:
        """Parse a stock code into (symbol, market).

        Args:
            ts_code: Stock code such as '600519', '600519.SH', or
                '000001.SZ'.

        Returns:
            Tuple of (six-digit symbol, market), where market is 'sh' or 'sz'.
        """
        symbol = str(ts_code).strip().upper().split('.')[0]
        if not symbol:
            raise ValueError('ts_code cannot be empty')

        market = 'sh' if symbol.startswith(('6', '9')) else 'sz'
        return symbol, market

    @staticmethod
    def _volume_series(df: pd.DataFrame) -> pd.Series:
        """Return the volume column regardless of source naming."""
        if 'vol' in df.columns:
            return df['vol']
        if 'volume' in df.columns:
            return df['volume']
        raise KeyError("DataFrame must contain 'vol' or 'volume'")

    def __init__(self, primary: str = None, fallback: str = None):
        """
        Args:
            primary: 主数据源名称 ('tdx' / 'akshare')，默认读取配置
            fallback: 备用数据源名称，默认读取配置
        """
        os.makedirs(DATA_CACHE_DIR, exist_ok=True)

        cfg = DATA_SOURCE_CONFIG
        self._primary_name = primary or cfg.get('primary', 'akshare')
        self._fallback_name = fallback or cfg.get('fallback', 'akshare')

        # 初始化主数据源
        self._primary = get_data_source(self._primary_name,
                                        **cfg.get(self._primary_name, {}))
        # 初始化备用数据源（如果与主数据源不同）
        if self._fallback_name != self._primary_name:
            self._fallback = get_data_source(self._fallback_name,
                                             **cfg.get(self._fallback_name, {}))
        else:
            self._fallback = None

        active_info = f'主: {self._primary_name}'
        if self._fallback:
            active_info += f', 备用: {self._fallback_name}'
        logger.info(f'数据源初始化成功 [{active_info}]',
                    extra={'data': {
                        'primary': self._primary_name,
                        'fallback': self._fallback_name,
                    }})

    def _fetch(self, method: str, *args, **kwargs):
        """从主数据源获取数据，失败时自动回退到备用数据源

        Args:
            method: 方法名（字符串）
            *args, **kwargs: 传递给数据源方法的参数

        Returns:
            数据源方法的返回值
        """
        try:
            result = getattr(self._primary, method)(*args, **kwargs)
            # 判断结果是否为空
            is_empty = False
            if isinstance(result, pd.DataFrame):
                is_empty = result.empty
            elif isinstance(result, dict):
                is_empty = not result
            elif isinstance(result, (list, tuple)):
                is_empty = len(result) == 0
            elif result is None:
                is_empty = True

            if is_empty and self._fallback:
                logger.debug(f'{method}: 主数据源返回空，尝试备用数据源')
                return getattr(self._fallback, method)(*args, **kwargs)
            return result
        except Exception as e:
            if self._fallback:
                logger.warning(f'{method}: 主数据源异常，回退到备用数据源: {e}')
                try:
                    return getattr(self._fallback, method)(*args, **kwargs)
                except Exception as e2:
                    logger.error(f'{method}: 备用数据源也失败: {e2}')
                    raise
            raise

    # ============================================================
    # 简单数据获取（委托给数据源）
    # ============================================================

    def get_stock_list(self) -> pd.DataFrame:
        """获取A股股票列表"""
        return self._fetch('get_stock_list')

    def get_realtime_quotes(self, stock_pool: list) -> dict:
        """获取实时行情快照"""
        return self._fetch('get_realtime_quotes', stock_pool)

    def get_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日线行情"""
        return self._fetch('get_daily_data', ts_code, start_date, end_date)

    def get_stock_info(self, symbol: str) -> dict:
        """获取股票基本信息"""
        return self._fetch('get_stock_info', symbol)

    def get_financial_data(self, symbol: str) -> dict:
        """获取财务数据"""
        return self._fetch('get_financial_data', symbol)

    def get_minute_data(self, ts_code: str, period: str = '5',
                        start_time: str = None, end_time: str = None) -> pd.DataFrame:
        """获取分钟K线"""
        return self._fetch('get_minute_data', ts_code, period,
                           start_time, end_time)

    def get_intraday_data(self, ts_code: str) -> pd.DataFrame:
        """获取日内分时数据"""
        return self._fetch('get_intraday_data', ts_code)

    # ============================================================
    # 扩展方法（代理到主数据源的特殊能力）
    # ============================================================

    def get_money_flow(self, symbol: str) -> pd.DataFrame:
        """获取个股资金流向"""
        return self._fetch('get_money_flow', symbol)

    def get_north_money_flow(self, days: int = 5) -> pd.DataFrame:
        """获取北向资金流向"""
        return self._fetch('get_north_money_flow', days)

    def get_industry_stocks(self, industry: str) -> list:
        """获取行业成分股"""
        return self._fetch('get_industry_stocks', industry)

    # ============================================================
    # 复合数据构建方法
    # ============================================================

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

    def calculate_growth(self, financial: dict) -> dict:
        """
        计算成长指标
        现在 financial 已经是结构化的 dict，直接返回即可
        保留此方法以兼容旧调用方式
        """
        if isinstance(financial, dict):
            return {
                'profit_growth': financial.get('profit_growth', 0),
                'revenue_growth': financial.get('revenue_growth', 0),
            }
        # 兼容旧的 DataFrame 调用
        return {'profit_growth': 0, 'revenue_growth': 0}

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

        # 获取财务数据（现在是结构化的 dict）
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
            'volume': latest.get('volume', latest.get('vol', 0)),
            'market_cap': market_cap,
            'ma5': latest.get('ma5', latest['close']),
            'ma10': latest.get('ma10', latest['close']),
            'ma20': latest.get('ma20', latest['close']),
            'ma60': latest.get('ma60', latest['close']),
            'ma28': latest.get('ma28', latest['close']),
            'high_1y': daily['high'].max(),
            'low_1y': daily['low'].min(),
            'price_percentile_1y': latest.get('price_percentile_1y', 0.5),
            'volume_ma20': latest.get('volume_ma20', latest.get('volume', latest.get('vol', 0))),
            'turnover': turnover,
            'pe': pe,
            'pb': pb,
            'pe_percentile_5y': 0.5,
            'roe': financial.get('roe', 0),
            'ep': 1 / pe if pe > 0 else 0.05,
            'profit_growth': growth['profit_growth'],
            'revenue_growth': growth['revenue_growth'],
            'gross_margin': financial.get('gross_margin', 0),
            'accrual_ratio': financial.get('accrual_ratio', 0),
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
        market_data_by_date = {}
        stock_info_cache = {}
        stock_financial_cache = {}

        # 计算扩展的开始日期（前120天，确保有足够数据计算ma60）
        start_dt = datetime.strptime(start_date, '%Y%m%d')
        extended_start = (start_dt - timedelta(days=180)).strftime('%Y%m%d')

        logger.info('开始获取历史数据', extra={'data': {
            'stock_count': len(stock_codes),
            'date_range': f'{extended_start}~{end_date}',
        }})

        for i, code in enumerate(stock_codes):
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"

            try:
                # 获取扩展的日线数据（包含前导数据）
                daily = self.get_daily_data(ts_code, extended_start, end_date)
                if daily.empty:
                    logger.warning(f'{code}: 无数据，跳过')
                    continue

                # 获取股票信息（缓存）
                if code not in stock_info_cache:
                    stock_info_cache[code] = self.get_stock_info(code)
                    time.sleep(0.3)

                # 获取财务数据（缓存）
                if code not in stock_financial_cache:
                    stock_financial_cache[code] = self.get_financial_data(code)
                    time.sleep(0.3)

                info = stock_info_cache[code]
                financial = stock_financial_cache[code]
                growth = self.calculate_growth(financial)
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

                    # ── Point-in-time financial data (eliminates look-ahead bias) ──
                    # Try database for the correct vintage of fundamental data.
                    # Falls back to the current snapshot if the DB has no data for this date.
                    ptl_pe = info.get('pe', 20)
                    ptl_pb = info.get('pb', 3)
                    ptl_roe = financial.get('roe', 0)
                    ptl_gross_margin = financial.get('gross_margin', 0)
                    ptl_accrual = financial.get('accrual_ratio', 0)
                    ptl_profit_growth = growth['profit_growth']
                    ptl_revenue_growth = growth['revenue_growth']
                    ptl_mcap = info.get('market_cap', 1e10)

                    try:
                        from data.database import SQLiteManager as _DB
                        _db = _DB()
                        # Point-in-time fundamentals lookup
                        fin_ptl = _db.get_financial_for_date(ts_code, date)
                        if fin_ptl:
                            ptl_roe = fin_ptl.get('roe', ptl_roe) or ptl_roe
                            ptl_gross_margin = fin_ptl.get('gross_margin', ptl_gross_margin) or ptl_gross_margin
                            ptl_accrual = fin_ptl.get('accrual_ratio', ptl_accrual) or ptl_accrual
                            ptl_profit_growth = fin_ptl.get('profit_growth', ptl_profit_growth) or ptl_profit_growth
                            ptl_revenue_growth = fin_ptl.get('revenue_growth', ptl_revenue_growth) or ptl_revenue_growth
                        # Point-in-time stock info (PE/PB may change over time)
                        info_ptl = _db.get_stock_info(ts_code)
                        if info_ptl:
                            ptl_pe = info_ptl.get('pe', ptl_pe) or ptl_pe
                            ptl_pb = info_ptl.get('pb', ptl_pb) or ptl_pb
                            ptl_mcap = info_ptl.get('market_cap', ptl_mcap) or ptl_mcap
                        _db.close()
                    except Exception:
                        pass  # DB unavailable: use snapshot values (acceptable fallback)

                    market_data_by_date[date][code] = {
                        'ts_code': code,
                        'code': code,
                        'close': close,
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'volume': row.get('volume', row.get('vol', 0)),
                        'prev_close': close / (1 + row['pct_chg'] / 100) if 'pct_chg' in row and not pd.isna(row['pct_chg']) and row['pct_chg'] != -100 else close,
                        'trade_date': date,
                        'ma5': safe_val(row.get('ma5'), close),
                        'ma10': safe_val(row.get('ma10'), close),
                        'ma20': safe_val(row.get('ma20'), close),
                        'ma60': safe_val(row.get('ma60'), close),
                        'ma28': safe_val(row.get('ma28'), close),
                        'high_1y': safe_val(row.get('rolling_high_1y'), close),
                        'low_1y': safe_val(row.get('rolling_low_1y'), close),
                        'price_percentile_1y': safe_val(row.get('price_percentile_1y'), 0.5),
                        'pe_percentile_5y': 0.5,
                        'volume_ma20': safe_val(row.get('volume_ma20'), row.get('volume', row.get('vol', 0))),
                        'turnover': safe_val(row.get('turnover_rate'), 3),
                        'pe': ptl_pe,
                        'pb': ptl_pb,
                        'ep': 1 / ptl_pe if ptl_pe > 0 else 0.05,
                        'roe': ptl_roe,
                        'profit_growth': ptl_profit_growth,
                        'revenue_growth': ptl_revenue_growth,
                        'gross_margin': ptl_gross_margin,
                        'accrual_ratio': ptl_accrual,
                        'pledge_ratio': 0.10,
                        'return_1d': safe_val(row.get('return_1d'), 0),
                        'return_20d': safe_val(row.get('return_20d'), 0),
                        'return_60d': safe_val(row.get('return_60d'), 0),
                        'volatility': safe_val(row.get('volatility_20d'), 0.25),
                        'market_cap': ptl_mcap,
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

                logger.debug(f'{code} {name}: {len(daily_backtest)}条数据')

                time.sleep(0.5)  # 限速

            except Exception as e:
                logger.error(f'{code}: 获取失败', extra={'data': {'code': code, 'error': str(e)}})
                continue

        logger.info('历史数据获取完成', extra={'data': {'trading_days': len(market_data_by_date)}})
        return market_data_by_date

    def fetch_and_store_minute_bars(self, stock_pool: list, db,
                                    period: str = '5') -> int:
        """Fetch minute bars for a pool of stocks and persist them via *db*.

        Each bar row is validated through DataValidator.validate_minute_bar
        before storage.  Progress is printed every 10 stocks.

        Args:
            stock_pool: List of stock codes (e.g. ['600519', '000001']).
            db:         SQLiteManager instance (must have upsert_minute_bars).
            period:     Bar period in minutes ('1', '5', '15', '30', '60').

        Returns:
            Total number of rows stored.
        """
        # Lazy import to avoid circular dependency at module level
        from data.validator import DataValidator

        total_stored = 0
        total_stocks = len(stock_pool)

        for i, code in enumerate(stock_pool):
            try:
                df = self.get_minute_data(ts_code=code, period=period)
                if df.empty:
                    logger.debug(f'{code}: 无分钟线数据')
                    continue

                rows = df.to_dict(orient='records')
                valid_rows = []
                rejected = 0
                for row in rows:
                    ok, _ = DataValidator.validate_minute_bar(row)
                    if ok:
                        valid_rows.append(row)
                    else:
                        rejected += 1

                if rejected > 0:
                    logger.warning(f'{code}: {rejected}行被数据验证拒绝')

                if valid_rows:
                    db.upsert_minute_bars(valid_rows)
                    total_stored += len(valid_rows)

                # Progress reporting every 10 stocks
                if (i + 1) % 10 == 0:
                    logger.info(f'分钟线进度: {i+1}/{total_stocks}, {total_stored}行已存储')

            except Exception as e:
                logger.error(f'{code}: 分钟线获取失败', extra={'data': {'code': code, 'error': str(e)}})

            # Rate limiting between stocks
            time.sleep(0.3)

        logger.info('分钟线批量获取完成', extra={'data': {
            'total_stored': total_stored, 'total_stocks': total_stocks,
        }})
        return total_stored

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
        volume = self._volume_series(df)
        for w in windows:
            df[f'volume_ma{w}'] = volume.rolling(window=w).mean()
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
        logger.info('数据已缓存', extra={'data': {'filepath': filepath}})

    def load_market_data(self, filename: str = 'market_data'):
        """加载市场数据"""
        filepath = f"{self.cache_dir}/{filename}.json"
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return None


# ======================================================================
# Quick manual verification (run: python data/fetcher.py)
# ======================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("DataFetcher facade — 验证")
    print("=" * 60)

    fetcher = DataFetcher()

    # --- 1. 数据源信息 ----------------------------------------------------------
    print(f"\n[1] 数据源: 主={fetcher._primary_name}, "
          f"备用={fetcher._fallback_name}")

    # --- 2. get_stock_list ------------------------------------------------------
    print("\n[2] get_stock_list")
    try:
        df_list = fetcher.get_stock_list()
        print(f"    股票数量: {len(df_list)}")
        if not df_list.empty:
            print(f"    前5只:\n{df_list.head().to_string()}")
    except Exception as e:
        print(f"    SKIPPED (error: {e})")

    # --- 3. get_daily_data ------------------------------------------------------
    print("\n[3] get_daily_data (000001.SZ)")
    try:
        df_daily = fetcher.get_daily_data('000001.SZ', '20260610', '20260616')
        print(f"    K线条数: {len(df_daily)}")
        if not df_daily.empty:
            print(f"    Columns: {list(df_daily.columns)}")
            print(f"    Head:\n{df_daily.head().to_string()}")
    except Exception as e:
        print(f"    SKIPPED (error: {e})")

    # --- 4. 数据源实例测试 ------------------------------------------------------
    print("\n[4] 直接数据源测试")
    try:
        tdx = get_data_source('tdx')
        print(f"    TDX source name: {tdx.name}")
        ak_src = get_data_source('akshare')
        print(f"    AKShare source name: {ak_src.name}")
    except Exception as e:
        print(f"    SKIPPED (error: {e})")

    # --- Final summary ---
    print("\n" + "=" * 60)
    print("VALIDATION COMPLETE")
    print("=" * 60)
