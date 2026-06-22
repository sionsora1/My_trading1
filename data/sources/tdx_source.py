"""
通达信（TDX）数据源 — 基于 pytdx 直连行情服务器
"""
from typing import Dict, List, Optional
import pandas as pd
import time

from data.sources.base import BaseDataSource
from utils.logger import get_logger

logger = get_logger('data', 'data.log')

# 股票名称本地缓存（避免实时行情时反复查库）
_name_cache: Dict[str, str] = {}

def _load_name_cache():
    """从本地数据库加载股票名称到内存缓存"""
    global _name_cache
    if _name_cache:
        return
    try:
        from data.database import SQLiteManager
        db = SQLiteManager()
        cur = db._conn.execute('SELECT ts_code, name FROM stock_info')
        for row in cur.fetchall():
            code = row['ts_code'].replace('.SH', '').replace('.SZ', '')
            _name_cache[code] = row['name']
        db.close()
    except Exception:
        pass

# TDX 行情服务器地址列表（含备用）
TDX_SERVERS: List[tuple] = [
    ('60.191.117.167', 7709),
    ('120.76.152.2', 7709),
    ('121.14.104.72', 7709),
    ('121.14.104.70', 7709),
    ('114.80.80.210', 7709),
    ('180.153.18.17', 7709),
]


class TDXDataSource(BaseDataSource):
    """通达信数据源 — 基于 pytdx 直连行情服务器"""

    def __init__(self, servers: List[tuple] = None, max_workers: int = 5,
                 connect_timeout: int = 5):
        """
        Args:
            servers: TDX服务器地址列表，格式 [(ip, port), ...]
            max_workers: 并发请求数上限
            connect_timeout: 连接超时秒数
        """
        self._servers = servers or TDX_SERVERS
        self._max_workers = max_workers
        self._connect_timeout = connect_timeout
        self._api = None
        self._connected = False
        logger.info('TDXDataSource 初始化',
                    extra={'data': {'servers': len(self._servers)}})

    @property
    def name(self) -> str:
        return 'tdx'

    def _connect(self) -> bool:
        """连接TDX行情服务器（自动重试多个服务器）"""
        if self._connected and self._api is not None:
            return True

        from pytdx.hq import TdxHq_API

        self._api = TdxHq_API(auto_retry=True, raise_exception=False)

        for ip, port in self._servers:
            try:
                if self._api.connect(ip, port, time_out=self._connect_timeout):
                    self._connected = True
                    logger.info(f'TDX连接成功: {ip}:{port}')
                    return True
            except Exception as e:
                logger.debug(f'TDX服务器 {ip}:{port} 连接失败: {e}')
                continue

        logger.warning('所有TDX服务器连接失败')
        self._api = None
        self._connected = False
        return False

    def _disconnect(self):
        """断开TDX连接"""
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception:
                pass
            self._api = None
        self._connected = False

    @staticmethod
    def _safe_decode(text: str) -> str:
        """修复 pytdx 返回的 GBK 编码字符串被 Latin-1 误解的问题。

        pytdx 底层用 GBK 解码 TDX 服务器返回的字节，但在某些环境下
        Python 将其解释为 Latin-1，导致中文变成乱码。
        此方法将乱码字符串恢复为正确的 Unicode。
        """
        if not isinstance(text, str) or not text:
            return text
        try:
            return text.encode('latin-1').decode('gbk')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text

    @staticmethod
    def _parse_code(ts_code: str) -> tuple:
        """解析股票代码 → (market, code)

        Args:
            ts_code: 如 '600519.SH' 或 '000001.SZ'

        Returns:
            (market, code): market=1 上海, market=0 深圳
        """
        symbol = str(ts_code).split('.')[0].strip()
        market = 1 if symbol.startswith('6') else 0
        return market, symbol

    # ============================================================
    # 股票列表
    # ============================================================

    def get_stock_info(self, symbol: str) -> dict:
        """获取单只股票基本信息（名称/行业等）

        通过 get_security_list 分页查找目标股票，
        并对名称做 _safe_decode 修复 GBK 编码。
        """
        if not self._connect():
            return {}

        symbol = str(symbol).strip()
        market = 1 if symbol.startswith('6') else 0

        start = 0
        while True:
            try:
                result = self._api.get_security_list(market, start)
                if not result:
                    break
                for item in result:
                    if str(item.get('code', '')) == symbol:
                        self._disconnect()
                        return {
                            'name': self._safe_decode(item.get('name', symbol)),
                            'industry': '未知',
                            'market_cap': 0,
                            'circ_market_cap': 0,
                            'pe': 0,
                            'pb': 0,
                        }
                if len(result) < 1000:
                    break
                start += 1000
            except Exception as e:
                logger.warning(f'TDX股票信息查找失败 {symbol}: {e}')
                break

        self._disconnect()
        return {}

    def get_stock_list(self) -> pd.DataFrame:
        """获取A股股票列表（沪深两市，过滤ST/退市/科创/北交）"""
        if not self._connect():
            return pd.DataFrame()

        all_stocks = []
        for market in [0, 1]:
            start = 0
            while True:
                try:
                    result = self._api.get_security_list(market, start)
                    if not result:
                        break
                    all_stocks.extend(result)
                    if len(result) < 1000:
                        break
                    start += 1000
                except Exception as e:
                    logger.warning(f'获取股票列表失败 market={market} start={start}: {e}')
                    break

        self._disconnect()

        if not all_stocks:
            return pd.DataFrame()

        df = pd.DataFrame(all_stocks)
        # 列: code, name, volunit, decimal_point, pre_close
        df = df.rename(columns={'code': 'symbol'})
        df['symbol'] = df['symbol'].astype(str).str.strip()

        # 生成 ts_code
        df['ts_code'] = df['symbol'].apply(
            lambda x: f"{x}.SH" if x.startswith('6') else f"{x}.SZ"
        )

        # 过滤ST和退市
        df = df[~df['name'].str.contains('ST|退市', na=False)]

        # 过滤科创板和北交所
        df = df[~df['symbol'].str.startswith(('688', '8', '4'))]

        # 修复 GBK 编码乱码
        df['name'] = df['name'].apply(self._safe_decode)
        df['industry'] = '未知'
        df['area'] = '中国'
        df['market'] = 'A股'
        df['list_date'] = ''

        logger.info(f'TDX股票列表: {len(df)}只')
        return df[['ts_code', 'symbol', 'name', 'area', 'industry', 'market', 'list_date']]

    # ============================================================
    # 日线数据
    # ============================================================

    def get_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日K线数据（前复权）

        Args:
            ts_code: 股票代码如 '600519.SH'
            start_date: 起始日期 'YYYYMMDD'
            end_date: 结束日期 'YYYYMMDD'

        Returns:
            DataFrame with columns: trade_date, open, high, low, close, vol, amount, pct_chg, ts_code
        """
        if not self._connect():
            return pd.DataFrame()

        market, code = self._parse_code(ts_code)

        try:
            # category=9: 日线（前复权）
            bars = self._api.get_security_bars(9, market, code, 0, 800)
            self._disconnect()

            if not bars:
                return pd.DataFrame()

            df = pd.DataFrame(bars)
            # pytdx 返回字段: year, month, day, hour, minute, open, high, low, close, amount, vol
            # Note: pytdx day bars have hour=minute=0
            df['trade_date'] = pd.to_datetime(
                df['year'].astype(str).str.zfill(4) +
                df['month'].astype(str).str.zfill(2) +
                df['day'].astype(str).str.zfill(2),
                format='%Y%m%d'
            )

            df = df.rename(columns={'vol': 'volume'})

            # 计算涨跌幅
            df = df.sort_values('trade_date')
            df['pct_chg'] = df['close'].pct_change() * 100

            df['ts_code'] = ts_code
            df['amount'] = df.get('amount', 0)

            # 按日期范围过滤
            start_dt = pd.to_datetime(start_date, format='%Y%m%d')
            end_dt = pd.to_datetime(end_date, format='%Y%m%d')
            df = df[(df['trade_date'] >= start_dt) & (df['trade_date'] <= end_dt)]

            result = df[['trade_date', 'open', 'high', 'low', 'close',
                         'volume', 'amount', 'pct_chg', 'ts_code']].reset_index(drop=True)

            logger.debug(f'TDX日线: {ts_code} {len(result)}条')
            return result

        except Exception as e:
            self._disconnect()
            logger.warning(f'TDX日线获取失败 {ts_code}: {e}')
            return pd.DataFrame()

    # ============================================================
    # 实时行情
    # ============================================================

    def get_realtime_quotes(self, codes: List[str]) -> Dict[str, dict]:
        """获取实时行情快照（含5档盘口）

        Args:
            codes: 股票代码列表，如 ['600519', '000001']

        Returns:
            {code: {close, open, high, low, volume, amount, bid1-5, ask1-5, ...}}
        """
        if not codes:
            return {}

        if not self._connect():
            return {}

        _load_name_cache()
        results = {}
        for ts_code in codes:
            market, code = self._parse_code(ts_code)
            try:
                quotes = self._api.get_security_quotes([(market, code)])
                if not quotes:
                    continue

                q = quotes[0]
                # Map fields from pytdx:
                # price=最新价, open=开盘价, high=最高价, low=最低价,
                # vol=成交量, amount=成交额
                # bid1-5=买1-5价, ask1-5=卖1-5价
                # bid_vol1-5=买1-5量, ask_vol1-5=卖1-5量
                # b_vol=外盘(主动买), s_vol=内盘(主动卖)
                clean_code = code
                name = q.get('name', '')
                if not name:
                    name = _name_cache.get(clean_code, clean_code)
                else:
                    name = self._safe_decode(name)
                results[clean_code] = {
                    'ts_code': clean_code,
                    'name': name,
                    'close': float(q.get('price', 0) or 0),
                    'open': float(q.get('open', 0) or 0),
                    'high': float(q.get('high', 0) or 0),
                    'low': float(q.get('low', 0) or 0),
                    'volume': float(q.get('vol', 0) or 0),
                    'amount': float(q.get('amount', 0) or 0),
                    'bid1': float(q.get('bid1', 0) or 0),
                    'bid2': float(q.get('bid2', 0) or 0),
                    'bid3': float(q.get('bid3', 0) or 0),
                    'bid4': float(q.get('bid4', 0) or 0),
                    'bid5': float(q.get('bid5', 0) or 0),
                    'bid_vol1': float(q.get('bid_vol1', 0) or 0),
                    'bid_vol2': float(q.get('bid_vol2', 0) or 0),
                    'bid_vol3': float(q.get('bid_vol3', 0) or 0),
                    'bid_vol4': float(q.get('bid_vol4', 0) or 0),
                    'bid_vol5': float(q.get('bid_vol5', 0) or 0),
                    'ask1': float(q.get('ask1', 0) or 0),
                    'ask2': float(q.get('ask2', 0) or 0),
                    'ask3': float(q.get('ask3', 0) or 0),
                    'ask4': float(q.get('ask4', 0) or 0),
                    'ask5': float(q.get('ask5', 0) or 0),
                    'ask_vol1': float(q.get('ask_vol1', 0) or 0),
                    'ask_vol2': float(q.get('ask_vol2', 0) or 0),
                    'ask_vol3': float(q.get('ask_vol3', 0) or 0),
                    'ask_vol4': float(q.get('ask_vol4', 0) or 0),
                    'ask_vol5': float(q.get('ask_vol5', 0) or 0),
                    'active_buy': float(q.get('b_vol', 0) or 0),   # 外盘(主动买)
                    'active_sell': float(q.get('s_vol', 0) or 0),  # 内盘(主动卖)
                }
            except Exception as e:
                logger.debug(f'TDX实时行情失败 {code}: {e}')
                continue

        self._disconnect()
        return results

    # ============================================================
    # 分钟K线
    # ============================================================

    def get_minute_data(self, ts_code: str, period: str = '5',
                        start_time: str = None, end_time: str = None) -> pd.DataFrame:
        """获取分钟K线数据

        Args:
            ts_code: 股票代码
            period: K线周期（默认'5'，TDX目前只支持5分钟线）
            start_time: 起始时间 'YYYYMMDD' 格式（日期部分用于确定查询日）
            end_time: 结束时间

        Returns:
            DataFrame with columns: trade_time, price, vol
        """
        if not self._connect():
            return pd.DataFrame()

        market, code = self._parse_code(ts_code)

        # 确定查询日期（整数格式如 20260616）
        if start_time:
            date_str = str(start_time)[:8].replace('-', '')
        else:
            from datetime import datetime
            date_str = datetime.now().strftime('%Y%m%d')

        try:
            date_int = int(date_str)
        except (ValueError, TypeError):
            date_int = 20260616

        try:
            bars = self._api.get_history_minute_time_data(market, code, date_int)
            self._disconnect()

            if not bars:
                return pd.DataFrame()

            df = pd.DataFrame(bars)
            # pytdx returns: price, vol per minute (240 bars/day max)

            df['ts_code'] = ts_code
            df['period'] = int(period) if period else 5

            # 生成 trade_time 列
            if 'hour' in df.columns and 'minute' in df.columns:
                df['trade_time'] = (
                    date_str[:4] + '-' + date_str[4:6] + '-' + date_str[6:8] + ' ' +
                    df['hour'].astype(str).str.zfill(2) + ':' +
                    df['minute'].astype(str).str.zfill(2) + ':00'
                )

            return df

        except Exception as e:
            self._disconnect()
            logger.warning(f'TDX分钟线获取失败 {ts_code}: {e}')
            return pd.DataFrame()

    # ============================================================
    # 除权除息
    # ============================================================

    def get_xdxr_data(self, ts_code: str) -> list:
        """获取除权除息数据"""
        if not self._connect():
            return []

        market, code = self._parse_code(ts_code)

        try:
            result = self._api.get_xdxr_info(market, code)
            self._disconnect()
            return result if result else []
        except Exception as e:
            self._disconnect()
            logger.debug(f'TDX除权除息失败 {ts_code}: {e}')
            return []

    # ============================================================
    # 分笔成交
    # ============================================================

    def get_transaction_data(self, ts_code: str, count: int = 10) -> list:
        """获取分笔成交数据"""
        if not self._connect():
            return []

        market, code = self._parse_code(ts_code)

        try:
            result = self._api.get_transaction_data(market, code, 0, count)
            self._disconnect()
            return result if result else []
        except Exception as e:
            self._disconnect()
            logger.debug(f'TDX分笔成交失败 {ts_code}: {e}')
            return []

    # ============================================================
    # 板块成分股（通达信特有）
    # ============================================================

    def get_block_stocks(self, block_name: str) -> list:
        """获取板块成分股

        Args:
            block_name: 板块名称，如 '金融行业'、'上海' 等

        Returns:
            股票代码列表
        """
        if not self._connect():
            return []

        try:
            # 遍历板块文件
            block_meta = self._api.get_block_info_meta()
            if not block_meta:
                self._disconnect()
                return []

            # 匹配板块名称
            target_block = None
            for bm in block_meta:
                if bm.get('blockname') == block_name:
                    target_block = bm
                    break

            if target_block is None:
                # 模糊匹配
                for bm in block_meta:
                    if block_name in str(bm.get('blockname', '')):
                        target_block = bm
                        break

            if target_block is None:
                self._disconnect()
                logger.debug(f'未找到板块: {block_name}')
                return []

            # 获取板块文件内容
            block_info = self._api.get_block_info(target_block['blockname'])
            self._disconnect()

            if block_info:
                # 解析板块成分股
                stocks = []
                for item in block_info:
                    if 'code' in item:
                        stocks.append(item['code'])
                return stocks

            return []

        except Exception as e:
            self._disconnect()
            logger.warning(f'TDX板块获取失败 {block_name}: {e}')
            return []
