"""
数据同步服务
管理 TDX/AKShare 数据到 SQLite 的定时同步
"""

import time
from typing import List, Optional
import pandas as pd

from utils.logger import get_logger

logger = get_logger('data', 'data.log')

# 板块文件映射
_BLOCK_FILE_MAP = {
    'gn': 'block_gn.dat',
    'hy': 'block.dat',
    'zs': 'block_zs.dat',
}


class DataSyncService:
    """数据同步服务"""

    def __init__(self, db, source):
        """
        Args:
            db: SQLiteManager 实例
            source: BaseDataSource 实例（如 TDXDataSource）
        """
        self.db = db
        self.source = source

    # ------------------------------------------------------------------
    # 股票列表
    # ------------------------------------------------------------------

    def sync_stock_list(self) -> int:
        """全量同步股票列表 → stock_info 表

        Returns:
            同步的股票数量
        """
        df = self.source.get_stock_list()
        if df is None or df.empty:
            logger.warning('股票列表为空，跳过同步')
            return 0

        rows = []
        for _, row in df.iterrows():
            code = str(row.get('symbol', ''))
            market = 'SH' if code.startswith('6') else 'SZ'
            rows.append({
                'ts_code': str(row.get('ts_code', '')),
                'name': str(row.get('name', '')),
                'industry': str(row.get('industry', '')),
                'market': market,
                'list_date': str(row.get('list_date', '')),
                'delist_date': '',
                'pe': 0.0,
                'pb': 0.0,
                'market_cap': 0.0,
            })

        batch_size = 500
        for i in range(0, len(rows), batch_size):
            self.db.upsert_stock_info(rows[i:i + batch_size])

        logger.info(f'股票列表同步完成: {len(rows)}只')
        return len(rows)

    # ------------------------------------------------------------------
    # 日线数据
    # ------------------------------------------------------------------

    def sync_daily_bars(self, codes: List[str], start_date: str, end_date: str,
                        batch_size: int = 50) -> dict:
        """批量同步日线数据 → daily_bars 表（增量 upsert）

        Args:
            codes: 股票代码列表，如 ['000001.SZ', '600519.SH']
            start_date: 起始日期 'YYYYMMDD'
            end_date: 结束日期 'YYYYMMDD'
            batch_size: 并发/批次大小（保留参数，当前逐股请求）

        Returns:
            {'synced': N, 'failed': N, 'total_bars': N}
        """
        synced = 0
        failed = 0
        total_bars = 0
        total = len(codes)

        for i, code in enumerate(codes):
            try:
                df = self.source.get_daily_data(code, start_date, end_date)
                if df is not None and not df.empty:
                    rows = []
                    for _, row in df.iterrows():
                        trade_dt = row['trade_date']
                        if hasattr(trade_dt, 'strftime'):
                            date_str = trade_dt.strftime('%Y%m%d')
                        else:
                            date_str = str(trade_dt)

                        rows.append({
                            'ts_code': str(row.get('ts_code', code)),
                            'trade_date': date_str,
                            'open': float(row.get('open', 0) or 0),
                            'high': float(row.get('high', 0) or 0),
                            'low': float(row.get('low', 0) or 0),
                            'close': float(row.get('close', 0) or 0),
                            'volume': float(row.get('volume', 0) or 0),
                            'amount': float(row.get('amount', 0) or 0),
                            'turnover': float(row.get('turnover', 0) or 0),
                            'pct_chg': float(row.get('pct_chg', 0) or 0),
                        })
                    if rows:
                        self.db.upsert_daily_bars(rows)
                        total_bars += len(rows)
                synced += 1
            except Exception as e:
                logger.warning(f'日线同步失败 {code}: {e}')
                failed += 1

            if (i + 1) % 10 == 0:
                logger.info(f'日线同步进度: {i + 1}/{total}')

            time.sleep(0.1)

        result = {'synced': synced, 'failed': failed, 'total_bars': total_bars}
        logger.info(f'日线同步完成: {result}')
        return result

    # ------------------------------------------------------------------
    # 分钟线数据
    # ------------------------------------------------------------------

    def sync_minute_bars(self, codes: List[str], date: str,
                         period: str = '5') -> dict:
        """同步指定日期的分钟线 → minute_bars 表

        Args:
            codes: 股票代码列表
            date: 交易日期 'YYYYMMDD'
            period: K线周期（默认'5'，即5分钟）

        Returns:
            {'synced': N, 'failed': N, 'total_bars': N}
        """
        synced = 0
        failed = 0
        total_bars = 0

        for code in codes:
            try:
                df = self.source.get_minute_data(code, period, start_time=date)
                if df is not None and not df.empty:
                    rows = []
                    for _, row in df.iterrows():
                        price = float(row.get('price', 0) or 0)
                        vol = float(row.get('vol', 0) or 0)
                        rows.append({
                            'ts_code': str(row.get('ts_code', code)),
                            'trade_time': str(row.get('trade_time', '')),
                            'period': int(row.get('period', 5)),
                            'open': price,
                            'high': price,
                            'low': price,
                            'close': price,
                            'volume': vol,
                            'amount': round(price * vol, 2),
                        })
                    if rows:
                        self.db.upsert_minute_bars(rows)
                        total_bars += len(rows)
                synced += 1
            except Exception as e:
                logger.warning(f'分钟线同步失败 {code}: {e}')
                failed += 1

        result = {'synced': synced, 'failed': failed, 'total_bars': total_bars}
        logger.info(f'分钟线同步完成: {result}')
        return result

    # ------------------------------------------------------------------
    # 除权除息
    # ------------------------------------------------------------------

    def sync_xdxr(self, codes: List[str]) -> int:
        """全量同步除权除息 → xdxr 表

        Args:
            codes: 股票代码列表

        Returns:
            同步的除权除息记录数
        """
        count = 0
        for code in codes:
            try:
                data = self.source.get_xdxr_data(code)
                if not data:
                    continue

                rows = []
                for item in data:
                    year = str(item.get('year', '')).strip()
                    month = str(item.get('month', '')).strip().zfill(2)
                    day = str(item.get('day', '')).strip().zfill(2)
                    ex_date = f'{year}{month}{day}'

                    rows.append({
                        'ts_code': code,
                        'ex_date': ex_date,
                        'category': item.get('category'),
                        'name': str(item.get('name', '')),
                        'fenhong': item.get('fenhong'),
                        'songzhuangu': item.get('songzhuangu'),
                        'peigu': item.get('peigu'),
                        'peigujia': item.get('peigujia'),
                        'suogu': item.get('suogu'),
                        'qianzongguben': item.get('qianzongguben'),
                        'houzongguben': item.get('houzongguben'),
                        'fenshu': item.get('fenshu'),
                        'xingquanjia': item.get('xingquanjia'),
                    })

                if rows:
                    self.db.upsert_xdxr(rows)
                    count += len(rows)

            except Exception as e:
                logger.warning(f'除权除息同步失败 {code}: {e}')

        logger.info(f'除权除息同步完成: {count}条')
        return count

    # ------------------------------------------------------------------
    # 板块成分股
    # ------------------------------------------------------------------

    def sync_blocks(self, block_types: List[str] = None) -> dict:
        """全量同步板块成分股 → block_info 表

        使用 pytdx 直连读取板块定义文件（block_gn.dat / block.dat / block_zs.dat）。

        Args:
            block_types: 板块类型列表，如 ['gn', 'hy', 'zs']，为 None 则同步全部

        Returns:
            {'gn': N, 'hy': N, 'zs': N}  各类型同步的股票数量
        """
        from pytdx.hq import TdxHq_API

        if block_types is None:
            block_types = list(_BLOCK_FILE_MAP.keys())

        servers = getattr(self.source, '_servers', [
            ('60.191.117.167', 7709),
            ('120.76.152.2', 7709),
        ])

        results = {}
        api = TdxHq_API()

        for block_type in block_types:
            filename = _BLOCK_FILE_MAP.get(block_type)
            if not filename:
                logger.warning(f'未知板块类型: {block_type}，跳过')
                results[block_type] = 0
                continue

            # 全量同步前先清除该类型的旧数据
            self.db.clear_block_info(block_type)

            count = 0
            connected = False

            for ip, port in servers:
                try:
                    if api.connect(ip, port, time_out=5):
                        connected = True
                        blocks = api.get_and_parse_block_info(filename)
                        api.disconnect()

                        if not blocks:
                            continue

                        rows = []
                        for block in blocks:
                            block_name = str(block.get('blockname', ''))
                            block_code = f'{block_type}_{block_name}'
                            code_index = block.get('code_index', [])

                            # code_index 可能是 list of tuples 或逗号分隔的字符串
                            stocks = self._parse_code_index(code_index)

                            for stock_code, stock_name in stocks:
                                stock_code = str(stock_code).strip()
                                if not stock_code:
                                    continue
                                ts_code = (f'{stock_code}.SH'
                                           if stock_code.startswith('6')
                                           else f'{stock_code}.SZ')
                                rows.append({
                                    'block_code': block_code,
                                    'block_name': block_name,
                                    'block_type': block_type,
                                    'ts_code': ts_code,
                                    'stock_name': str(stock_name).strip(),
                                })

                        # 分批写入
                        batch_size = 1000
                        for i in range(0, len(rows), batch_size):
                            self.db.upsert_block_info(rows[i:i + batch_size])
                        count = len(rows)

                        logger.info(f'板块 {block_type} ({filename}): 解析出{len(blocks)}个板块, '
                                    f'{count}条成分股记录')
                        break  # 成功，跳出服务器重试循环

                except Exception as e:
                    if connected:
                        try:
                            api.disconnect()
                        except Exception:
                            pass
                        connected = False
                    logger.debug(f'板块同步 {ip}:{port} {filename} 失败: {e}')
                    continue

            if connected:
                try:
                    api.disconnect()
                except Exception:
                    pass

            results[block_type] = count

        logger.info(f'板块同步全部完成: {results}')
        return results

    @staticmethod
    def _parse_code_index(code_index):
        """解析板块成分股的 code_index 字段

        code_index 可能是:
          1. list of (code, name) tuples
          2. 逗号分隔的字符串 "code1,name1,code2,name2,..."
          3. list of strings ["code1", "name1", "code2", "name2", ...]

        Returns:
            list of (code, name) tuples
        """
        if not code_index:
            return []

        # 情况1: list of tuples
        if isinstance(code_index, list) and len(code_index) > 0:
            if isinstance(code_index[0], (list, tuple)):
                return [(str(c), str(n)) for c, n in code_index]
            # 情况3: list of strings (alternating code/name)
            if isinstance(code_index[0], str):
                pairs = []
                for j in range(0, len(code_index) - 1, 2):
                    pairs.append((code_index[j], code_index[j + 1]))
                return pairs

        # 情况2: 逗号分隔字符串
        if isinstance(code_index, str):
            parts = [p.strip() for p in code_index.split(',')]
            pairs = []
            for j in range(0, len(parts) - 1, 2):
                pairs.append((parts[j], parts[j + 1]))
            return pairs

        return []

    # ------------------------------------------------------------------
    # 扩展财务数据
    # ------------------------------------------------------------------

    def sync_finance_detail(self, codes: List[str]) -> int:
        """同步扩展财务数据 → finance_detail 表

        通过 pytdx 的 get_finance_info 接口获取。

        Args:
            codes: 股票代码列表

        Returns:
            同步的财务记录数
        """
        # 检查 source 类型，仅 TDX 支持
        source_name = getattr(self.source, 'name', '')
        if isinstance(source_name, property):
            try:
                source_name = self.source.name
            except Exception:
                source_name = ''

        if source_name != 'tdx':
            logger.info(f'当前数据源 ({source_name}) 不支持财务数据同步，跳过')
            return 0

        from pytdx.hq import TdxHq_API

        servers = getattr(self.source, '_servers', [
            ('60.191.117.167', 7709),
            ('120.76.152.2', 7709),
        ])

        # 获取 _parse_code 方法（优先使用 source 自带方法）
        parse_code = getattr(self.source, '_parse_code', None)
        if parse_code is None:
            # 回退：内联实现
            def _fallback_parse_code(ts_code):
                symbol = str(ts_code).split('.')[0].strip()
                market = 1 if symbol.startswith('6') else 0
                return market, symbol
            parse_code = _fallback_parse_code

        count = 0
        total = len(codes)

        for idx, code in enumerate(codes):
            market, symbol = parse_code(code)

            for ip, port in servers:
                api = None
                try:
                    api = TdxHq_API()
                    if not api.connect(ip, port, time_out=5):
                        continue

                    item = api.get_finance_info(market, symbol)
                    api.disconnect()
                    api = None

                    if not item or not isinstance(item, dict):
                        break

                    # TDX 返回单个 dict，直接用 updated_date 作为报告期
                    updated = str(item.get('updated_date', '')).strip()
                    if len(updated) == 8:
                        report_date = updated
                    else:
                        from datetime import datetime
                        report_date = datetime.now().strftime('%Y%m%d')

                    rows = [{
                        'ts_code': code,
                        'report_date': report_date,
                        'total_shares': item.get('zongguben'),
                        'float_shares': item.get('liutongguben'),
                        'state_shares': item.get('guojiagu'),
                        'legal_person_shares': item.get('farengu'),
                        'b_shares': item.get('bgu'),
                        'h_shares': item.get('hgu'),
                        'employee_shares': item.get('zhigonggu'),
                        'total_assets': item.get('zongzichan'),
                        'current_assets': item.get('liudongzichan'),
                        'fixed_assets': item.get('gudingzichan'),
                        'intangible_assets': item.get('wuxingzichan'),
                        'net_equity': item.get('jingzichan'),
                        'current_liabilities': item.get('liudongfuzhai'),
                        'long_term_liabilities': item.get('changqifuzhai'),
                        'operating_revenue': item.get('zhuyingshouru'),
                        'operating_profit': item.get('zhuyinglirun'),
                        'business_profit': item.get('yingyelirun'),
                        'net_profit_after_tax': item.get('jinglirun'),
                        'retained_earnings': item.get('weifenpeilirun'),
                        'operating_cf': item.get('jingyingxianjinliu'),
                        'total_cf': item.get('zongxianjinliu'),
                        'capital_reserve': item.get('zibengongjijin'),
                        'shareholder_count': item.get('gudongrenshu'),
                        'net_assets_ps': item.get('meigujingzichan'),
                        'investment_income': item.get('touzishouyu'),
                        'inventory': item.get('cunhuo'),
                        'receivables': item.get('yingshouzhangkuan'),
                        'ipo_date': str(item.get('ipo_date', '')),
                    }]

                    self.db.upsert_finance_detail(rows)
                    count += len(rows)

                    break  # 成功

                except Exception as e:
                    if api:
                        try:
                            api.disconnect()
                        except Exception:
                            pass
                    logger.debug(f'财务数据同步 {code} ({ip}:{port}): {e}')
                    continue

            if (idx + 1) % 20 == 0:
                logger.info(f'财务数据同步进度: {idx + 1}/{total}')

        logger.info(f'财务数据同步完成: {count}条记录')
        return count

    # ------------------------------------------------------------------
    # 缺口检查与补全
    # ------------------------------------------------------------------

    def check_and_fill_gaps(self, codes: List[str], days_back: int = 30) -> dict:
        """检查日线缺失日期，自动补全

        Args:
            codes: 股票代码列表
            days_back: 回溯天数

        Returns:
            {'codes_checked': N, 'gaps_filled': N, 'bars_added': N}
        """
        from datetime import datetime, timedelta

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y%m%d')

        codes_checked = 0
        gaps_filled = 0
        bars_added = 0

        for code in codes:
            try:
                # 获取已有日期
                existing_rows = self.db._conn.execute(
                    "SELECT trade_date FROM daily_bars WHERE ts_code = ? "
                    "AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
                    (code, start_date, end_date)
                ).fetchall()
                existing_dates = set(row['trade_date'] for row in existing_rows)

                if not existing_dates:
                    # 没有数据，全量拉取
                    df = self.source.get_daily_data(code, start_date, end_date)
                    if df is not None and not df.empty:
                        rows = self._df_to_daily_rows(df, code)
                        if rows:
                            self.db.upsert_daily_bars(rows)
                            bars_added += len(rows)
                        gaps_filled += 1
                    codes_checked += 1
                    continue

                codes_checked += 1

                # 生成期望的交易日列表（先查 calendar，没有则用简单启发式）
                expected_dates = self._get_expected_trade_dates(start_date, end_date)

                missing_dates = [d for d in expected_dates if d not in existing_dates]
                if not missing_dates:
                    continue

                # 对缺失日期逐段拉取
                missing_start = missing_dates[0]
                missing_end = missing_dates[-1]

                df = self.source.get_daily_data(code, missing_start, missing_end)
                if df is not None and not df.empty:
                    # 只写入确实缺失的日期
                    rows = self._df_to_daily_rows(df, code)
                    new_rows = [r for r in rows if r['trade_date'] in missing_dates]
                    if new_rows:
                        self.db.upsert_daily_bars(new_rows)
                        bars_added += len(new_rows)
                    gaps_filled += 1

            except Exception as e:
                logger.warning(f'缺口检查失败 {code}: {e}')

        result = {
            'codes_checked': codes_checked,
            'gaps_filled': gaps_filled,
            'bars_added': bars_added,
        }
        logger.info(f'缺口补全完成: {result}')
        return result

    def _get_expected_trade_dates(self, start_date: str, end_date: str) -> list:
        """获取期望的交易日列表

        优先查 trade_calendar 表，若无数据则使用简单工作日启发式。
        """
        # 先尝试从 calendar 表获取
        try:
            dates = self.db.get_calendar_dates(start_date, end_date)
            if dates:
                return dates
        except Exception:
            pass

        # 回退：周一至周五作为期望日期
        from datetime import datetime, timedelta

        start_dt = datetime.strptime(start_date, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d')

        dates = []
        current = start_dt
        while current <= end_dt:
            if current.weekday() < 5:  # 周一至周五
                dates.append(current.strftime('%Y%m%d'))
            current += timedelta(days=1)

        return dates

    @staticmethod
    def _df_to_daily_rows(df: pd.DataFrame, ts_code: str) -> list:
        """将 DataFrame 转为 daily_bars upsert 行列表"""
        rows = []
        for _, row in df.iterrows():
            trade_dt = row['trade_date']
            if hasattr(trade_dt, 'strftime'):
                date_str = trade_dt.strftime('%Y%m%d')
            else:
                date_str = str(trade_dt)

            rows.append({
                'ts_code': str(row.get('ts_code', ts_code)),
                'trade_date': date_str,
                'open': float(row.get('open', 0) or 0),
                'high': float(row.get('high', 0) or 0),
                'low': float(row.get('low', 0) or 0),
                'close': float(row.get('close', 0) or 0),
                'volume': float(row.get('volume', 0) or 0),
                'amount': float(row.get('amount', 0) or 0),
                'turnover': float(row.get('turnover', 0) or 0),
                'pct_chg': float(row.get('pct_chg', 0) or 0),
            })
        return rows

    # ------------------------------------------------------------------
    # 收盘一键同步
    # ------------------------------------------------------------------

    def daily_close_sync(self, codes: List[str], date: str) -> dict:
        """收盘一键同步：日线 + 分钟线 + 财务

        Args:
            codes: 股票代码列表
            date: 交易日期 'YYYYMMDD'

        Returns:
            {'daily': {...}, 'minute': {...}, 'finance': N}
        """
        logger.info(f'收盘同步开始: date={date}, codes={len(codes)}只')

        # 日线（当天区间）
        daily_result = self.sync_daily_bars(codes, date, date)

        # 分钟线
        minute_result = self.sync_minute_bars(codes, date)

        # 财务数据
        finance_count = self.sync_finance_detail(codes)

        result = {
            'date': date,
            'daily': daily_result,
            'minute': minute_result,
            'finance': finance_count,
        }
        logger.info(f'收盘同步完成: {result}')
        return result


# ======================================================================
# 快速验证（需要 TDX 网络环境）
# ======================================================================
if __name__ == '__main__':
    from data.database import SQLiteManager
    from data.sources import get_data_source

    db = SQLiteManager()
    src = get_data_source('tdx')
    svc = DataSyncService(db, src)

    print('=' * 60)
    print('DataSyncService 验证')
    print('=' * 60)

    # 1. 同步股票列表（少量验证）
    print('\n[1] 同步股票列表...')
    try:
        count = svc.sync_stock_list()
        print(f'    股票列表: {count}只')
    except Exception as e:
        print(f'    股票列表失败: {e}')

    # 2. 同步日线
    print('\n[2] 同步日线...')
    try:
        result = svc.sync_daily_bars(['000001.SZ', '600519.SH'], '20260610', '20260616')
        print(f'    日线: {result}')
    except Exception as e:
        print(f'    日线失败: {e}')

    # 3. 同步除权除息
    print('\n[3] 同步除权除息...')
    try:
        count = svc.sync_xdxr(['000001.SZ'])
        print(f'    除权除息: {count}条')
    except Exception as e:
        print(f'    除权除息失败: {e}')

    # 4. 同步板块（仅概念板块，数据量小）
    print('\n[4] 同步板块...')
    try:
        result = svc.sync_blocks(['gn'])
        print(f'    板块: {result}')
    except Exception as e:
        print(f'    板块失败: {e}')

    print('\n' + '=' * 60)
    print('验证完成')
    print('=' * 60)

    db.close()
