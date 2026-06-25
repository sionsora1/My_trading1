"""
回放测试引擎 — 加载录制数据，以可控速度模拟实时行情驱动实盘模块。

用法:
    engine = ReplayEngine('20260623')
    engine.start(speed=10)          # 10x 速度
    engine.wait()                    # 阻塞直到回放完成
    engine.stop()
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.logger import get_logger

logger = get_logger('test', 'test.log')

TEST_DATA_DIR = os.path.join(_project_root, 'test_data')


class ReplayEngine:
    """回放引擎 — 模拟实时行情流驱动实盘模块。

    数据加载:
        - snapshots.jsonl → QuoteSnapshot 流
        - daily_bars.json  → 日线数据（注入 SQLite 内存库）
        - stock_info.json  → 股票信息
        - minute_bars.jsonl → 分钟K线

    回放模式:
        speed=1  → 实时速度
        speed=60 → 1分钟 = 1秒
        speed=0  → 最快（无延迟，逐步推进）
    """

    def __init__(self, session_date: str):
        """
        Args:
            session_date: 录制日期 'YYYYMMDD'
        """
        self._session_date = session_date
        self._session_dir = os.path.join(TEST_DATA_DIR, session_date)

        # 加载元数据
        meta_path = os.path.join(self._session_dir, 'metadata.json')
        if not os.path.isfile(meta_path):
            raise FileNotFoundError(f'录制数据不存在: {session_date}')
        with open(meta_path, 'r', encoding='utf-8') as f:
            self._meta = json.load(f)

        self._stock_pool: List[str] = self._meta.get('stock_pool', [])

        # 状态
        self._running = threading.Event()
        self._speed: float = 1.0
        self._current_time: float = 0.0
        self._snapshot_index: int = 0
        self._snapshots: List[dict] = []
        self._snapshot_times: List[float] = []

        # 测试组件
        self._test_db = None
        self._monitor_alerts: List[Any] = []
        self._live_signals: List[Any] = []
        self._test_live_server = None

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def progress(self) -> float:
        """回放进度 0.0~1.0。"""
        if not self._snapshots:
            return 0.0
        return self._snapshot_index / len(self._snapshots)

    @property
    def simulated_time(self) -> str:
        """当前模拟的墙上时间。"""
        if self._snapshot_index < len(self._snapshot_times):
            ts = self._snapshot_times[self._snapshot_index]
            return datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        return '--:--:--'

    @property
    def stock_pool(self) -> List[str]:
        return self._stock_pool

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        """加载录制数据到内存。"""
        # 1. snapshots
        snap_path = os.path.join(self._session_dir, 'snapshots.jsonl')
        if os.path.isfile(snap_path):
            with open(snap_path, 'r', encoding='utf-8') as f:
                self._snapshots = [json.loads(line) for line in f if line.strip()]
            self._snapshot_times = [s['ts'] for s in self._snapshots]

        # 2. daily_bars
        db_path = os.path.join(self._session_dir, 'daily_bars.json')
        self._daily_bars: Dict[str, list] = {}
        if os.path.isfile(db_path):
            with open(db_path, 'r', encoding='utf-8') as f:
                self._daily_bars = json.load(f)

        # 3. stock_info
        si_path = os.path.join(self._session_dir, 'stock_info.json')
        self._stock_info: Dict[str, dict] = {}
        if os.path.isfile(si_path):
            with open(si_path, 'r', encoding='utf-8') as f:
                self._stock_info = json.load(f)

        # 4. minute_bars
        mb_path = os.path.join(self._session_dir, 'minute_bars.jsonl')
        self._minute_bars: Dict[str, list] = defaultdict(list)
        if os.path.isfile(mb_path):
            with open(mb_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        code = r.pop('code', '')
                        self._minute_bars[code].append(r)

        logger.info(
            f'数据加载: {len(self._snapshots)} snapshots, '
            f'{len(self._daily_bars)} 日线, {len(self._stock_info)} 股票信息, '
            f'{len(self._minute_bars)} 只分钟线'
        )
        return {
            'snapshots': len(self._snapshots),
            'daily_bars_stocks': len(self._daily_bars),
            'stock_info': len(self._stock_info),
            'minute_stocks': len(self._minute_bars),
        }

    def _build_test_db(self):
        """构建测试用内存 SQLite（不落盘）。"""
        import sqlite3

        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row

        # stock_info
        db.execute('''CREATE TABLE stock_info (
            ts_code TEXT PRIMARY KEY, name TEXT, industry TEXT,
            market TEXT, list_date TEXT, pe REAL, pb REAL, market_cap REAL)''')
        for ts_code, info in self._stock_info.items():
            db.execute(
                'INSERT OR REPLACE INTO stock_info VALUES (?,?,?,?,?,?,?,?)',
                (ts_code, info.get('name', ''), info.get('industry', ''),
                 info.get('market', ''), info.get('list_date', ''),
                 info.get('pe'), info.get('pb'), info.get('market_cap')),
            )

        # daily_bars
        db.execute('''CREATE TABLE daily_bars (
            ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, turnover REAL, pct_chg REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma60 REAL, volume_ma20 REAL,
            return_1d REAL, return_5d REAL, return_20d REAL, return_60d REAL,
            volatility REAL, PRIMARY KEY(ts_code, trade_date))''')
        for ts_code, bars in self._daily_bars.items():
            for b in bars:
                try:
                    db.execute(
                        'INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (b.get('ts_code', ts_code), b.get('trade_date', ''),
                         b.get('open'), b.get('high'), b.get('low'), b.get('close'),
                         b.get('volume'), b.get('amount'), b.get('turnover'), b.get('pct_chg'),
                         b.get('ma5'), b.get('ma10'), b.get('ma20'), b.get('ma60'),
                         b.get('volume_ma20'), b.get('return_1d'), b.get('return_5d'),
                         b.get('return_20d'), b.get('return_60d'), b.get('volatility')),
                    )
                except Exception:
                    continue

        # minute_bars
        db.execute('''CREATE TABLE minute_bars (
            ts_code TEXT, trade_time TEXT, period INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
            PRIMARY KEY(ts_code, trade_time, period))''')

        db.commit()
        self._test_db = db
        logger.info('测试数据库已构建 (内存)')

    # ------------------------------------------------------------------
    # 回放控制
    # ------------------------------------------------------------------

    def start(self, speed: float = 1.0, mode: str = 'monitor') -> Dict[str, Any]:
        """启动回放。

        Args:
            speed: 速度倍率 (1=实时, 60=1分钟→1秒, 0=最快)
            mode: 'monitor' / 'live' / 'both'

        Returns:
            {'status': 'started', ...}
        """
        if self._running.is_set():
            return {'status': 'already_running'}

        self.load()
        self._build_test_db()
        self._speed = max(0.001, speed)
        self._mode = mode
        self._snapshot_index = 0
        self._monitor_alerts = []
        self._live_signals = []
        self._running.set()

        self._thread = threading.Thread(target=self._replay_loop, daemon=True, name='replay-engine')
        self._thread.start()

        logger.info(f'回放启动: speed={speed}x, mode={mode}')
        return {'status': 'started', 'speed': speed, 'mode': mode,
                'total_snapshots': len(self._snapshots),
                'stock_pool_size': len(self._stock_pool)}

    def stop(self) -> Dict[str, Any]:
        """停止回放。"""
        self._running.clear()
        result = {
            'status': 'stopped',
            'snapshots_played': self._snapshot_index,
            'monitor_alerts': len(self._monitor_alerts),
            'live_signals': len(self._live_signals),
        }
        logger.info(f'回放停止: {result}')
        return result

    def set_speed(self, speed: float) -> Dict[str, Any]:
        """运行时调整回放速度。"""
        self._speed = max(0.001, speed)
        return {'speed': self._speed}

    def wait(self, timeout: float = None) -> None:
        """阻塞等待回放完成。"""
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # ------------------------------------------------------------------
    # 回放主循环
    # ------------------------------------------------------------------

    def _replay_loop(self) -> None:
        """回放主循环 — 按时间戳顺序注入数据。"""
        snapshot_batches = self._group_snapshots()
        prev_ts = None

        for batch_ts, batch in snapshot_batches:
            if not self._running.is_set():
                break

            # 时间控制
            if prev_ts is not None and self._speed > 0:
                real_delay = (batch_ts - prev_ts) / self._speed
                if real_delay > 0:
                    time.sleep(min(real_delay, 5.0))  # 最多等5秒，防止卡死

            prev_ts = batch_ts
            self._snapshot_index += len(batch)
            self._current_time = batch_ts

            # 注入到 MonitorEngine
            if self._mode in ('monitor', 'both'):
                self._feed_monitor(batch)

            # 注入到 LiveTrading (每分钟一次)
            if self._mode in ('live', 'both'):
                self._feed_live(batch_ts, batch)

    def _group_snapshots(self) -> List[Tuple[float, List[dict]]]:
        """将 snapshots 按时间戳分组（同一轮的 snapshots 放在一个 batch）。"""
        if not self._snapshots:
            return []
        batches = []
        current_ts = None
        current_batch = []
        for s in self._snapshots:
            ts = s['ts']
            if current_ts is None:
                current_ts = ts
            if abs(ts - current_ts) > 1.5:  # 超过 1.5 秒视为新一批
                batches.append((current_ts, current_batch))
                current_ts = ts
                current_batch = []
            current_batch.append(s)
        if current_batch:
            batches.append((current_ts, current_batch))
        return batches

    # ------------------------------------------------------------------
    # 数据注入
    # ------------------------------------------------------------------

    def _feed_monitor(self, batch: List[dict]) -> None:
        """将 snapshot batch 注入 MonitorEngine 的检测管线。

        这里绕过 TDXQuotesPoller，直接构造 QuoteSnapshot 并喂入
        Detector + 异动检测器，模拟 MonitorEngine 内部逻辑。
        """
        try:
            from broker.monitor import QuoteSnapshot, Detector
            from broker.detector import AnomalyAlert
            from broker.detector.divergence import DivergenceDetector
            from broker.detector.turnover import TurnoverDetector
            from broker.detector.orderbook import OrderbookDetector

            # 构造 snapshots
            curr = {}
            for s in batch:
                code = s['code']
                curr[code] = QuoteSnapshot(
                    code=code, name=s.get('name', code),
                    price=s['price'], open=s['open'], high=s['high'], low=s['low'],
                    volume=s['volume'], amount=s['amount'],
                    change_pct=s.get('change_pct', 0), last_close=s.get('last_close', 0),
                    bid1=s.get('bid1', 0), bid2=s.get('bid2', 0), bid3=s.get('bid3', 0),
                    bid4=s.get('bid4', 0), bid5=s.get('bid5', 0),
                    ask1=s.get('ask1', 0), ask2=s.get('ask2', 0), ask3=s.get('ask3', 0),
                    ask4=s.get('ask4', 0), ask5=s.get('ask5', 0),
                    bid_vol1=s.get('bid_vol1', 0), bid_vol2=s.get('bid_vol2', 0),
                    bid_vol3=s.get('bid_vol3', 0), bid_vol4=s.get('bid_vol4', 0),
                    bid_vol5=s.get('bid_vol5', 0),
                    ask_vol1=s.get('ask_vol1', 0), ask_vol2=s.get('ask_vol2', 0),
                    ask_vol3=s.get('ask_vol3', 0), ask_vol4=s.get('ask_vol4', 0),
                    ask_vol5=s.get('ask_vol5', 0),
                    active_buy=s.get('active_buy', 0), active_sell=s.get('active_sell', 0),
                    time=s.get('time', ''),
                )
            if not hasattr(self, '_prev_snapshots'):
                self._prev_snapshots: Dict[str, QuoteSnapshot] = {}
            if not hasattr(self, '_detector'):
                self._detector = Detector()
            if not hasattr(self, '_divergence_detector'):
                self._divergence_detector = DivergenceDetector()
                self._turnover_detector = TurnoverDetector()
                self._orderbook_detector = OrderbookDetector()
                # 加载 liutong + turnover medians
                try:
                    from data.database import SQLiteManager
                    test_db_obj = SQLiteManager.__new__(SQLiteManager)
                    test_db_obj._conn = self._test_db
                    # 简化: 直接用录制时的日线数据计算 turnover medians
                    liutong = {}
                    for code in self._stock_pool:
                        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                        info = self._stock_info.get(ts_code, {})
                        mc = info.get('market_cap', 0)
                        bars = self._daily_bars.get(ts_code, [])
                        if mc > 0 and bars and bars[-1].get('close'):
                            total = mc / bars[-1]['close']
                            liutong[code] = total * 0.7
                    self._turnover_detector.set_liutong_cache(liutong)

                    import statistics
                    medians = {}
                    for code in self._stock_pool:
                        ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
                        lt = liutong.get(code)
                        if not lt:
                            continue
                        bars = self._daily_bars.get(ts_code, [])
                        daily_pcts = [(b['volume'] / lt) * 100 for b in bars if b.get('volume')]
                        if len(daily_pcts) >= 5:
                            m = statistics.median(daily_pcts)
                            medians[code] = {'daily': round(m, 4), '5min': round(m / 48, 6)}
                    self._turnover_detector.set_hist_medians(medians)
                    self._orderbook_detector.set_liutong_cache(liutong)
                except Exception:
                    pass

            # 喂入 Detector
            prev = self._prev_snapshots
            for code, snap in curr.items():
                p = prev.get(code) if prev else None
                if p is None or p.volume <= 0:
                    continue
                da = snap.amount - p.amount
                dv = snap.volume - p.volume
                db_val = (snap.active_buy or 0) - (p.active_buy or 0)
                ds = (snap.active_sell or 0) - (p.active_sell or 0)
                if dv <= 0 or da <= 0:
                    continue
                self._detector.feed(code, da, dv, db_val, ds)
                result = self._detector.check(code, da, dv, db_val, ds)
                if result:
                    level, mad = result
                    self._monitor_alerts.append({
                        'ts': self._current_time, 'type': 'mad',
                        'code': code, 'name': snap.name,
                        'level': level, 'mad_multiple': mad,
                        'price': snap.price, 'amount': da,
                    })

            # 异动检测器
            try:
                anomaly_alerts = self._divergence_detector.check(curr, prev)
                anomaly_alerts += self._turnover_detector.check(curr)
                anomaly_alerts += self._orderbook_detector.check(curr)
                for a in anomaly_alerts:
                    self._monitor_alerts.append({
                        'ts': self._current_time, 'type': f'{a.type}/{a.subtype}',
                        'code': a.code, 'name': a.name,
                        'data': a.data if hasattr(a, 'data') else {},
                    })
            except Exception:
                pass

            self._prev_snapshots = curr

        except Exception as e:
            logger.error(f'Monitor feed 异常: {e}', exc_info=True)

    def _feed_live(self, batch_ts: float, batch: List[dict]) -> None:
        """注入 LiveTrading 所需的数据（每分钟触发一次策略扫描）。

        这里简化实现：记录触发的时机但不实际执行交易。
        完整实现需要启动 LiveTradingServer 并替换其 DataFetcher。
        """
        # 每分钟触发一次
        if not hasattr(self, '_last_live_ts'):
            self._last_live_ts = 0
        if batch_ts - self._last_live_ts < 60:
            return
        self._last_live_ts = batch_ts

        # 构造 market_data dict
        now_str = datetime.fromtimestamp(batch_ts).strftime('%H:%M:%S')
        market_data = {}
        for s in batch:
            code = s['code']
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            info = self._stock_info.get(ts_code, {})
            bars = self._daily_bars.get(ts_code, [])
            latest_bar = bars[-1] if bars else {}
            market_data[code] = {
                'close': s['price'], 'open': s['open'], 'high': s['high'],
                'low': s['low'], 'volume': s['volume'], 'amount': s['amount'],
                'change_pct': s.get('change_pct', 0),
                'ma5': latest_bar.get('ma5'), 'ma10': latest_bar.get('ma10'),
                'ma20': latest_bar.get('ma20'), 'ma60': latest_bar.get('ma60'),
                'volume_ma20': latest_bar.get('volume_ma20'),
                'return_1d': latest_bar.get('return_1d'), 'return_5d': latest_bar.get('return_5d'),
                'return_20d': latest_bar.get('return_20d'), 'return_60d': latest_bar.get('return_60d'),
                'volatility': latest_bar.get('volatility'),
                'pe': info.get('pe', 0), 'pb': info.get('pb', 0),
                'ep': 1 / info.get('pe', 1) if info.get('pe', 0) > 0 else 0,
                'roe': 0, 'name': info.get('name', code), 'industry': info.get('industry', ''),
            }
        self._live_signals.append({
            'ts': batch_ts, 'time': now_str,
            'stocks': len(market_data), 'market_data_snapshot': market_data,
        })

    # ------------------------------------------------------------------
    # 测试结果查询
    # ------------------------------------------------------------------

    def get_alerts(self, limit: int = 200) -> List[dict]:
        """获取回放期间的告警。"""
        return self._monitor_alerts[-limit:]

    def get_alert_summary(self) -> Dict[str, int]:
        """告警类型汇总。"""
        from collections import Counter
        return dict(Counter(a['type'] for a in self._monitor_alerts))

    def get_progress(self) -> Dict[str, Any]:
        """回放进度。"""
        return {
            'running': self.is_running,
            'speed': self._speed,
            'progress': round(self.progress * 100, 1),
            'snapshot_index': self._snapshot_index,
            'total_snapshots': len(self._snapshots),
            'simulated_time': self.simulated_time,
            'monitor_alerts': len(self._monitor_alerts),
            'live_signals': len(self._live_signals),
        }
