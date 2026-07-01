"""
回放测试录制器 — 截获实盘数据流并保存到 test_data/

录制数据源:
    1. TDX get_security_quotes → QuoteSnapshot (5秒/轮)
    2. TDX get_minute_data → 分钟K线 (60秒/轮)
    3. 东方财富资金流向 → fund_flow (60秒/轮)
    4. daily_bars 快照 → 策略计算所需的日线数据
    5. stock_info 快照 → 股票基本信息

输出目录: test_data/YYYYMMDD/
    snapshots.jsonl     — 每行一个 {ts, code, price, ..., bid1-5, ask1-5, active_buy, active_sell}
    minute_bars.jsonl   — 每行一个 {trade_time, ts_code, open, high, low, close, volume, period}
    fund_flow.jsonl     — 每行一个 {ts, code, super_large, large, medium, small}
    stock_info.json     — {ts_code: {name, industry, pe, pb, market_cap}}
    metadata.json       — {date, stock_pool, start_time, end_time, snapshot_count, ...}
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger('test', 'test.log')

TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'test_data')


class RecordSession:
    """录制会话 — 线程安全，同时录制 QuoteSnapshot / 分钟线 / 资金流向。

    用法:
        session = RecordSession.start(stock_pool)

        # 在 MonitorEngine poll 循环中:
        session.record_snapshots(snapshots)

        # 在 LiveTrading 扫描中:
        session.record_minute_bars(code, bars_df)
        session.record_fund_flow(code, flow_data)
    """

    _instance = None  # 全局单例

    @classmethod
    def get_instance(cls) -> 'RecordSession':
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._lock = threading.Lock()
        self._date: str = ''
        self._stock_pool: List[str] = []
        self._started_at: str = ''
        self._snapshot_count: int = 0
        self._minute_count: int = 0
        self._fund_count: int = 0
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop: threading.Event = threading.Event()

        # File handles
        self._snap_fh = None
        self._minute_fh = None
        self._fund_fh = None

    @property
    def is_recording(self) -> bool:
        return self._snap_fh is not None

    @property
    def date(self) -> str:
        return self._date

    @property
    def snapshot_count(self) -> int:
        return self._snapshot_count

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------

    def start(self, stock_pool: List[str], date: str = '') -> Dict[str, Any]:
        """开始录制。

        Args:
            stock_pool: 监控股票池
            date: 交易日期 (YYYYMMDD，默认今天)

        Returns:
            {'status': 'started', 'dir': str}
        """
        with self._lock:
            if self.is_recording:
                return {'status': 'already_recording', 'dir': self._session_dir}

            self._date = date or datetime.now().strftime('%Y%m%d')
            self._stock_pool = list(stock_pool)
            self._started_at = datetime.now().isoformat()
            self._snapshot_count = 0
            self._minute_count = 0
            self._fund_count = 0

            self._session_dir = os.path.join(TEST_DATA_DIR, self._date)
            os.makedirs(self._session_dir, exist_ok=True)

            # 打开追加写入的 JSONL 文件
            self._snap_fh = open(os.path.join(self._session_dir, 'snapshots.jsonl'), 'a', encoding='utf-8')
            self._minute_fh = open(os.path.join(self._session_dir, 'minute_bars.jsonl'), 'a', encoding='utf-8')
            self._fund_fh = open(os.path.join(self._session_dir, 'fund_flow.jsonl'), 'a', encoding='utf-8')

            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_snapshots, daemon=True, name='record-poll')
            self._poll_thread.start()

            logger.info(f'录制开始: {self._session_dir}, {len(stock_pool)} 只股票')
            return {'status': 'started', 'dir': self._session_dir}

    def stop(self) -> Dict[str, Any]:
        """停止录制并保存元数据。"""
        # 先停轮询线程
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5.0)

        with self._lock:
            if not self.is_recording:
                return {'status': 'not_recording'}

            self._snap_fh.close()
            self._minute_fh.close()
            self._fund_fh.close()
            self._snap_fh = None
            self._minute_fh = None
            self._fund_fh = None

            # 保存元数据 + stock_info 快照
            metadata = {
                'date': self._date,
                'stock_pool': self._stock_pool,
                'started_at': self._started_at,
                'ended_at': datetime.now().isoformat(),
                'snapshot_count': self._snapshot_count,
                'minute_count': self._minute_count,
                'fund_count': self._fund_count,
            }
            with open(os.path.join(self._session_dir, 'metadata.json'), 'w', encoding='utf-8') as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            self._save_stock_info_snapshot()

            logger.info(f'录制停止: {self._snapshot_count} snapshots, {self._minute_count} minute bars')
            return {'status': 'stopped', 'metadata': metadata}

    # ------------------------------------------------------------------
    # 独立轮询线程（不依赖 MonitorEngine）
    # ------------------------------------------------------------------

    def _poll_snapshots(self) -> None:
        """独立 daemon 线程：每 3 秒拉 TDX 快照 → 录制 + 检测器告警 + SSE 推送。"""
        from broker.monitor import TDXQuotesPoller, QuoteSnapshot, Detector
        poller = TDXQuotesPoller()
        logger.info(f'[录制轮询] 启动: {len(self._stock_pool)} 只股票')

        # 预加载名称映射（TDX 批量查询不返回名称，从 stock_info 补）
        name_map: Dict[str, str] = {}
        try:
            info_path = os.path.join(self._session_dir, 'stock_info.json')
            if os.path.isfile(info_path):
                with open(info_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
                for ts_code, v in info.items():
                    code = v.get('code', '') or ts_code.split('.')[0]
                    name = v.get('name', '')
                    if code and name:
                        name_map[code] = name
                logger.info(f'[录制轮询] 加载 {len(name_map)} 个名称映射')
        except Exception:
            pass

        # 初始化检测器（复用回放引擎的管线）
        detector = Detector()
        prev_snapshots: Dict[str, Any] = {}

        poll_round = 0
        while not self._poll_stop.is_set():
            if not TDXQuotesPoller.is_trading_time():
                time.sleep(30)
                continue

            try:
                curr = poller.poll(self._stock_pool)
            except Exception as e:
                logger.debug(f'[录制轮询] TDX 异常: {e}')
                time.sleep(10)
                continue

            if not curr:
                time.sleep(3)
                continue

            # 补名称
            for code, snap in list(curr.items()):
                if hasattr(snap, 'name') and not snap.name and code in name_map:
                    curr[code] = dataclasses.replace(snap, name=name_map[code])

            # 录制快照
            try:
                self.record_snapshots(curr)
            except Exception as e:
                logger.debug(f'[录制轮询] 写入异常: {e}')

            # 大单检测 + SSE 推送
            try:
                self._detect_and_push(curr, prev_snapshots, detector)
            except Exception as e:
                logger.debug(f'[录制轮询] 检测异常: {e}')

            prev_snapshots = dict(curr)

            # 每 60 秒拉一次分钟 K 线
            poll_round += 1
            if poll_round % 20 == 0:
                self._fetch_minute_bars(poller)

            # 每天拉一次资金流向（日线数据，首次轮询立即拉）
            if poll_round == 1:
                self._fetch_fund_flow()

            time.sleep(3)

        logger.info(f'[录制轮询] 停止: 共 {self._snapshot_count} 条快照')

    def _detect_and_push(self, curr: dict, prev: dict, detector) -> None:
        """跑大单检测器并把告警推到 MonitorEngine 的 SSE 通道。"""
        try:
            alerts = []
            for code, snap in curr.items():
                p = prev.get(code)
                if p is None or getattr(p, 'volume', 0) <= 0:
                    continue
                da = snap.amount - p.amount
                dv = snap.volume - p.volume
                db_val = (getattr(snap, 'active_buy', 0) or 0) - (getattr(p, 'active_buy', 0) or 0)
                ds = (getattr(snap, 'active_sell', 0) or 0) - (getattr(p, 'active_sell', 0) or 0)
                if dv <= 0 or da <= 0:
                    continue
                detector.feed(code, da, dv, db_val, ds)
                result = detector.check(code, da, dv, db_val, ds)
                if result:
                    level, mad = result
                    direction = 'buy' if db_val > ds else ('sell' if ds > db_val else 'neutral')
                    alerts.append({
                        'code': code, 'name': getattr(snap, 'name', code),
                        'direction': direction, 'level': level,
                        'volume': int(dv), 'hands': int(dv // 100),
                        'amount': round(da, 2), 'price': snap.price,
                        'mad_multiple': round(mad, 2),
                        'time': getattr(snap, 'time', ''),
                        'timestamp': datetime.now().isoformat(),
                    })

            if alerts:
                # 通过 MonitorEngine 的 SSE 推送到前端
                self._push_alerts_to_monitor(alerts)
        except Exception as e:
            logger.debug(f'[检测] 异常: {e}')

    @staticmethod
    def _push_alerts_to_monitor(alerts: list) -> None:
        """把告警通过 MonitorEngine SSE 推送到前端大单监控面板。"""
        try:
            from broker.monitor import MonitorEngine
            engine = MonitorEngine.get_instance()
            loop = getattr(engine, '_event_loop', None)
            if loop is None or not loop.is_running():
                return
            sse = getattr(engine, '_sse', None)
            if sse is None:
                return
            import asyncio
            for alert_data in alerts:
                # push dict, SSE stream handler 检查 isinstance(dict) 后序列化
                asyncio.run_coroutine_threadsafe(sse.push(alert_data), loop)
        except Exception:
            pass

    def _fetch_minute_bars(self, poller) -> None:
        """拉取今日分钟 K 线并写入录制文件（每 60 秒调用一次）。"""
        if not self.is_recording:
            return
        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API(auto_retry=True, raise_exception=False)
            market_map = {'6': 1, '0': 0, '3': 0}
            today_str = datetime.now().strftime('%Y%m%d')
            count = 0

            # 复用录制线程的 TDX 服务器配置
            for ip, port in poller._servers[:1]:  # 只用一个服务器
                try:
                    if api.connect(ip, port, time_out=5.0):
                        break
                except Exception:
                    continue

            for code in self._stock_pool:
                if self._poll_stop.is_set():
                    break
                market = market_map.get(code[0], 0)
                try:
                    bars = api.get_security_bars(9, market, code, 0, 240)
                    if bars:
                        normalized = []
                        for b in bars:
                            bar_time = str(b.get('datetime', ''))
                            # bar_time 格式: '2026-07-01 15:00', today_str: '20260701'
                            bar_date = bar_time[:10].replace('-', '')
                            if bar_date != today_str:
                                continue
                            normalized.append({
                                'trade_time': bar_time,
                                'open': float(b.get('open', 0) or 0),
                                'high': float(b.get('high', 0) or 0),
                                'low': float(b.get('low', 0) or 0),
                                'close': float(b.get('close', 0) or 0),
                                'volume': float(b.get('vol', 0) or 0),
                                'amount': float(b.get('amount', 0) or 0),
                            })
                        if normalized:
                            self.record_minute_bars(code, normalized)
                            count += 1
                except Exception:
                    pass
                time.sleep(0.05)

            try:
                api.disconnect()
            except Exception:
                pass
            if count > 0:
                logger.info(f'[录制轮询] 分钟线: {count}/{len(self._stock_pool)} 只, 累计 {self._minute_count} 条')
        except Exception as e:
            logger.warning(f'[录制轮询] 分钟线拉取异常: {e}')

    def _fetch_fund_flow(self) -> None:
        """拉取资金流向（每天一次，限制 TOP 10 避免 API 过载）。"""
        if not self.is_recording:
            return
        try:
            from data.fetcher import DataFetcher
            fetcher = DataFetcher()
            fund_ok = 0
            for code in self._stock_pool[:10]:
                try:
                    df = fetcher.get_money_flow(code)
                    if df is not None and not df.empty:
                        latest = df.iloc[-1].to_dict()
                        self.record_fund_flow(code, latest)
                        fund_ok += 1
                except Exception:
                    pass
            if fund_ok > 0:
                logger.info(f'[录制轮询] 资金流向: {fund_ok}/10 只')
        except Exception as e:
            logger.debug(f'[录制轮询] 资金流向异常: {e}')

    # ------------------------------------------------------------------
    # 录制方法（供 MonitorEngine 等外部调用，兼容旧逻辑）
    # ------------------------------------------------------------------

    def record_snapshots(self, snapshots: Dict[str, Any]) -> None:
        """录制一轮 QuoteSnapshot。

        Args:
            snapshots: {code: QuoteSnapshot} 或 {code: dict}
        """
        if not self.is_recording:
            return
        now_ts = time.time()
        with self._lock:
            for code, snap in snapshots.items():
                if hasattr(snap, '__dataclass_fields__'):
                    d = {
                        'ts': now_ts,
                        'code': snap.code, 'name': snap.name,
                        'price': snap.price, 'open': snap.open,
                        'high': snap.high, 'low': snap.low,
                        'volume': snap.volume, 'amount': snap.amount,
                        'change_pct': snap.change_pct,
                        'last_close': snap.last_close,
                        'bid1': snap.bid1, 'ask1': snap.ask1,
                        'bid_vol1': snap.bid_vol1, 'ask_vol1': snap.ask_vol1,
                        'bid2': snap.bid2, 'ask2': snap.ask2,
                        'bid_vol2': snap.bid_vol2, 'ask_vol2': snap.ask_vol2,
                        'bid3': snap.bid3, 'ask3': snap.ask3,
                        'bid_vol3': snap.bid_vol3, 'ask_vol3': snap.ask_vol3,
                        'bid4': snap.bid4, 'ask4': snap.ask4,
                        'bid_vol4': snap.bid_vol4, 'ask_vol4': snap.ask_vol4,
                        'bid5': snap.bid5, 'ask5': snap.ask5,
                        'bid_vol5': snap.bid_vol5, 'ask_vol5': snap.ask_vol5,
                        'active_buy': snap.active_buy if hasattr(snap, 'active_buy') else 0,
                        'active_sell': snap.active_sell if hasattr(snap, 'active_sell') else 0,
                        'time': getattr(snap, 'time', ''),
                    }
                else:
                    d = {'ts': now_ts, **{k: v for k, v in snap.items()}}
                self._snap_fh.write(json.dumps(d, ensure_ascii=False) + '\n')
            self._snapshot_count += len(snapshots)

    def record_minute_bars(self, code: str, bars) -> None:
        """录制单只股票的分钟K线。

        Args:
            code: 股票代码
            bars: DataFrame 或 list[dict]
        """
        if not self.is_recording or bars is None:
            return
        with self._lock:
            if hasattr(bars, 'to_dict'):
                records = bars.to_dict(orient='records')
            else:
                records = list(bars) if isinstance(bars, list) else []
            for r in records:
                d = {'ts': time.time(), 'code': code}
                if hasattr(r, 'items'):
                    d.update({k: v for k, v in r.items() if k != 'ts_code'})
                else:
                    d['data'] = str(r)
                # 处理不可序列化的值
                for k, v in list(d.items()):
                    if isinstance(v, float) and (v != v):  # NaN
                        d[k] = None
                try:
                    line = json.dumps(d, ensure_ascii=False, default=str)
                    self._minute_fh.write(line + '\n')
                except Exception:
                    pass
            self._minute_count += 1

    def record_fund_flow(self, code: str, flow: dict) -> None:
        """录制单只股票的资金流向。

        Args:
            code: 股票代码
            flow: EastMoney fund flow dict
        """
        if not self.is_recording:
            return
        with self._lock:
            d = {'ts': time.time(), 'code': code}
            if flow:
                d.update({k: v for k, v in flow.items()})
            try:
                self._fund_fh.write(json.dumps(d, ensure_ascii=False, default=str) + '\n')
                self._fund_count += 1
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _save_stock_info_snapshot(self) -> None:
        """保存 stock_info + finance_detail 快照（回放时需要）。"""
        try:
            from data.database import SQLiteManager
            db = SQLiteManager()

            # stock_info
            rows = db._conn.execute('SELECT * FROM stock_info').fetchall()
            stock_info = {}
            for r in rows:
                d = dict(r)
                code = d.pop('ts_code')
                stock_info[code] = d
            with open(os.path.join(self._session_dir, 'stock_info.json'), 'w', encoding='utf-8') as f:
                json.dump(stock_info, f, ensure_ascii=False, default=str)

            # daily_bars for stock_pool (recent 120 days for strategies)
            daily_bars = {}
            for c in self._stock_pool:
                ts_code = f"{c}.SH" if c.startswith('6') else f"{c}.SZ"
                rows = db._conn.execute(
                    "SELECT * FROM daily_bars WHERE ts_code=? AND trade_date >= ? ORDER BY trade_date",
                    (ts_code, (datetime.now().replace(year=datetime.now().year - 1)).strftime('%Y%m%d')),
                ).fetchall()
                daily_bars[ts_code] = [dict(r) for r in rows]
            with open(os.path.join(self._session_dir, 'daily_bars.json'), 'w', encoding='utf-8') as f:
                json.dump(daily_bars, f, ensure_ascii=False, default=str)

            db.close()
            logger.info('stock_info + daily_bars 快照已保存')
        except Exception as e:
            logger.warning(f'快照保存失败: {e}')

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def list_sessions() -> List[Dict]:
        """列出所有录制会话。"""
        if not os.path.isdir(TEST_DATA_DIR):
            return []
        sessions = []
        for d in sorted(os.listdir(TEST_DATA_DIR), reverse=True):
            path = os.path.join(TEST_DATA_DIR, d)
            meta_path = os.path.join(path, 'metadata.json')
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        meta = json.load(f)
                    meta['dir'] = d
                    # 检查文件完整性
                    meta['has_snapshots'] = os.path.isfile(os.path.join(path, 'snapshots.jsonl'))
                    meta['has_minute'] = os.path.isfile(os.path.join(path, 'minute_bars.jsonl'))
                    sessions.append(meta)
                except Exception:
                    pass
        return sessions

    @staticmethod
    def cleanup_old(max_days: int = 5) -> int:
        """清理超过 max_days 天的录制数据。"""
        if not os.path.isdir(TEST_DATA_DIR):
            return 0
        cutoff = datetime.now().strftime('%Y%m%d')
        removed = 0
        for d in sorted(os.listdir(TEST_DATA_DIR)):
            if d < cutoff:
                path = os.path.join(TEST_DATA_DIR, d)
                if os.path.isdir(path):
                    import shutil
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1
        return removed
