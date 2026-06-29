"""
回放测试引擎 — 多日流式加载 + 策略回测 + 检测器告警

用法:
    engine = ReplayEngine(['20260623', '20260624'])
    engine.run(strategies=['momentum', 'value'])
    print(engine.get_results())
"""

from __future__ import annotations

import json
import os
import sys
import statistics
import threading
import time
from collections import defaultdict, Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from utils.logger import get_logger

logger = get_logger('test', 'test.log')

TEST_DATA_DIR = os.path.join(_project_root, 'test_data')


class ReplayEngine:
    """多日回放引擎 — 流式加载 + 策略回测 + 检测器告警。

    工作流程:
        1. 按日流式加载 snapshots → 聚合为 daily OHLCV market_data
        2. 构建 market_data_by_date dict
        3. 对每个选中策略，用 BacktestEngine (strict_mode) 跑回测
        4. 同时运行检测器（MAD/背离/盘口/换手）产出告警
        5. 返回每策略的 trades + 告警汇总
    """

    def __init__(self, session_dates: List[str]):
        """
        Args:
            session_dates: 录制日期列表，按日期排序 ['20260623', '20260624', ...]
        """
        self._session_dates = sorted(session_dates)
        self._sessions: List[_SessionData] = []
        self._stock_pool: List[str] = []
        self._stock_info: Dict[str, dict] = {}
        self._daily_bars: Dict[str, List[dict]] = {}

        # 状态
        self._running = False
        self._stop_event = threading.Event()
        self._speed: float = 1.0
        self._progress: float = 0.0
        self._current_day: int = 0
        self._total_days: int = 0
        self._current_time_str: str = ''

        # 结果
        self._monitor_alerts: List[dict] = []
        self._strategy_results: Dict[str, dict] = {}  # {strategy_name: result}
        self._market_data_by_date: Dict[str, dict] = {}

        # 检测器实例（复用）
        self._detector = None
        self._divergence_detector = None
        self._turnover_detector = None
        self._orderbook_detector = None
        self._prev_snapshots: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and not self._stop_event.is_set()

    @property
    def progress(self) -> float:
        return self._progress

    @property
    def simulated_time(self) -> str:
        return self._current_time_str

    # ------------------------------------------------------------------
    # 核心: 执行
    # ------------------------------------------------------------------

    def run(
        self,
        strategies: List[str],
        speed: float = 1.0,
        initial_capital: float = 1_000_000,
        commission_rate: float = 0.0003,
        slippage_rate: float = 0.002,
        stop_loss_rate: float = -0.08,
        min_commission: float = 5.0,
        max_positions: int = 10,
    ) -> Dict[str, Any]:
        """执行多日回放 + 策略回测 + 检测器告警。

        Args:
            strategies: 策略名称列表 ['momentum', 'eight_factor', ...]
            speed: 回放速度倍率
            initial_capital: 初始资金
            commission_rate: 佣金率
            slippage_rate: 滑点率
            stop_loss_rate: 止损线（负数）
            min_commission: 最低佣金
            max_positions: 最大持仓数

        Returns:
            {
                'status': 'completed',
                'strategies': {name: {trades, metrics, ...}},
                'alerts': [...],
                'alert_summary': {...},
            }
        """
        self._running = True
        self._stop_event.clear()
        self._speed = max(0.001, speed)
        self._monitor_alerts = []
        self._strategy_results = {}
        self._market_data_by_date = {}
        self._prev_snapshots = {}

        try:
            # 1. 验证 + 加载元数据
            self._load_sessions()

            # 2. 构建 market_data_by_date（流式：逐日加载快照 → 聚合）
            self._build_market_data()

            # 3. 先跑策略回测（快，秒级），让交易结果立即可见
            for name in strategies:
                if self._stop_event.is_set():
                    break
                self._run_strategy_backtest(
                    name, initial_capital, commission_rate,
                    slippage_rate, stop_loss_rate, min_commission, max_positions,
                )
            self._progress = 0.5  # 策略完成 = 50%

            # 4. 跑检测器告警（慢，逐快照处理）
            self._run_detectors()

            self._progress = 1.0
            return self.get_results()

        except Exception as e:
            logger.error(f'回放执行异常: {e}', exc_info=True)
            return {'status': 'error', 'error': str(e)}
        finally:
            self._running = False

    def stop(self) -> Dict[str, Any]:
        """停止回放。"""
        self._stop_event.set()
        self._running = False
        return self.get_results()

    def set_speed(self, speed: float) -> Dict[str, Any]:
        """调整回放速度。"""
        self._speed = max(0.001, speed)
        return {'speed': self._speed}

    # ------------------------------------------------------------------
    # 会话加载
    # ------------------------------------------------------------------

    def _load_sessions(self) -> None:
        """验证并加载所有会话元数据。"""
        self._sessions = []
        for date in self._session_dates:
            session_dir = os.path.join(TEST_DATA_DIR, date)
            meta_path = os.path.join(session_dir, 'metadata.json')
            if not os.path.isfile(meta_path):
                raise FileNotFoundError(f'录制数据不存在: {date}')

            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)

            session = _SessionData(
                date=date,
                dir=session_dir,
                meta=meta,
                stock_pool=meta.get('stock_pool', []),
            )
            self._sessions.append(session)

            # 合并 stock_pool
            for code in session.stock_pool:
                if code not in self._stock_pool:
                    self._stock_pool.append(code)

        self._total_days = len(self._sessions)
        logger.info(f'加载 {self._total_days} 个交易日: {self._session_dates}')

        # 加载共享数据（stock_info + daily_bars）
        self._load_shared_data()

    def _load_shared_data(self) -> None:
        """加载股票信息和日线（跨会话共享）。"""
        # 构建 code → name 映射（录制快照可能缺名称，从 stock_info 补）
        self._stock_names: Dict[str, str] = {}
        for session in self._sessions:
            # stock_info
            si_path = os.path.join(session.dir, 'stock_info.json')
            if os.path.isfile(si_path):
                with open(si_path, 'r', encoding='utf-8') as f:
                    info = json.load(f)
                    for ts_code, v in info.items():
                        if ts_code not in self._stock_info:
                            self._stock_info[ts_code] = v
                        code = v.get('code', '') or ts_code.split('.')[0]
                        name = v.get('name', '')
                        if code and name and code not in self._stock_names:
                            self._stock_names[code] = name

            # daily_bars（合并，取最新）
            db_path = os.path.join(session.dir, 'daily_bars.json')
            if os.path.isfile(db_path):
                with open(db_path, 'r', encoding='utf-8') as f:
                    bars = json.load(f)
                    for ts_code, v in bars.items():
                        if ts_code not in self._daily_bars:
                            self._daily_bars[ts_code] = []
                        # 去重合并
                        existing_dates = {b.get('trade_date', '') for b in self._daily_bars[ts_code]}
                        for bar in v:
                            if bar.get('trade_date', '') not in existing_dates:
                                self._daily_bars[ts_code].append(bar)
                        self._daily_bars[ts_code].sort(key=lambda x: x.get('trade_date', ''))

    # ------------------------------------------------------------------
    # 构建 market_data_by_date
    # ------------------------------------------------------------------

    def _build_market_data(self) -> None:
        """逐日加载快照 → 聚合为日线 OHLCV → 构建 market_data_by_date。

        同时追加每日 bar 到 daily_bars 缓存（保证 D2+ 指标正确）。
        """
        logger.info('构建 market_data...')

        for day_idx, session in enumerate(self._sessions):
            if self._stop_event.is_set():
                break

            self._current_day = day_idx + 1
            self._progress = day_idx / max(self._total_days, 1) * 0.5  # 0~50%: 构建市场数据

            # 加载当天快照
            snapshots = self._load_snapshots_streaming(session)
            if not snapshots:
                logger.warning(f'{session.date}: 无快照数据，跳过')
                continue

            # 聚合为日线
            daily_data = self._aggregate_daily(snapshots, session)
            if not daily_data:
                logger.warning(f'{session.date}: 聚合后无数据，跳过')
                continue

            # 追加当天 bar 到日线缓存
            self._append_daily_bar(daily_data, session.date)

            # 构建 market_data（含技术指标）
            market_data = self._build_market_data_for_date(daily_data, session.date)
            self._market_data_by_date[session.date] = market_data

            logger.info(f'{session.date}: {len(market_data)} 只股票 → market_data')

    def _load_snapshots_streaming(self, session: '_SessionData') -> Dict[str, List[dict]]:
        """流式加载单日快照，按 code 分组。"""
        snap_path = os.path.join(session.dir, 'snapshots.jsonl')
        if not os.path.isfile(snap_path):
            return {}

        by_code: Dict[str, List[dict]] = defaultdict(list)
        with open(snap_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    s = json.loads(line)
                    code = s.get('code', '')
                    if code:
                        by_code[code].append(s)
                except json.JSONDecodeError:
                    continue

        return by_code

    def _aggregate_daily(
        self, snapshots_by_code: Dict[str, List[dict]], session: '_SessionData',
    ) -> Dict[str, dict]:
        """将 3 秒快照聚合为日线 OHLCV。

        Returns:
            {code: {open, high, low, close, volume, amount, change_pct, name}}
        """
        daily: Dict[str, dict] = {}
        for code, snaps in snapshots_by_code.items():
            if not snaps:
                continue
            prices = [s['price'] for s in snaps if s.get('price', 0) > 0]
            if not prices:
                continue
            volumes = [s.get('volume', 0) for s in snaps]
            amounts = [s.get('amount', 0) for s in snaps]
            name = snaps[0].get('name', '') or self._stock_names.get(code, code)
            daily[code] = {
                'open': snaps[0].get('open', prices[0]),
                'high': max(s.get('high', p) for s, p in zip(snaps, prices)),
                'low': min(s.get('low', p) for s, p in zip(snaps, prices)),
                'close': prices[-1],
                'volume': volumes[-1] if volumes else 0,
                'amount': amounts[-1] if amounts else 0,
                'change_pct': snaps[-1].get('change_pct', 0) if snaps else 0,
                'name': name,
            }
        return daily

    def _append_daily_bar(self, daily_data: Dict[str, dict], date: str) -> None:
        """将聚合后的日线追加到 daily_bars 缓存。"""
        for code, d in daily_data.items():
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            if ts_code not in self._daily_bars:
                self._daily_bars[ts_code] = []
            # 检查是否已存在
            exists = any(b.get('trade_date') == date for b in self._daily_bars[ts_code])
            if not exists:
                self._daily_bars[ts_code].append({
                    'trade_date': date,
                    'open': d['open'],
                    'high': d['high'],
                    'low': d['low'],
                    'close': d['close'],
                    'volume': d['volume'],
                    'amount': d['amount'],
                })
                self._daily_bars[ts_code].sort(key=lambda x: x.get('trade_date', ''))

    def _build_market_data_for_date(self, daily_data: Dict[str, dict], date: str) -> Dict[str, dict]:
        """基于当天日线 + 累积日线缓存构建 market_data dict。

        格式与 BacktestEngine / strategy 期望的完全一致:
            {ts_code: {close, open, high, low, volume, amount, ma5, ma10, ...}}
        """
        market_data: Dict[str, dict] = {}

        for code, d in daily_data.items():
            ts_code = f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            bars = self._daily_bars.get(ts_code, [])
            info = self._stock_info.get(ts_code, {})

            # 技术指标
            closes = [b['close'] for b in bars if b.get('close', 0) > 0]
            volumes = [b['volume'] for b in bars if b.get('volume', 0) > 0]

            # MA
            ma5 = round(sum(closes[-5:]) / min(5, len(closes[-5:])), 2) if closes[-5:] else d['close']
            ma10 = round(sum(closes[-10:]) / min(10, len(closes[-10:])), 2) if closes[-10:] else d['close']
            ma20 = round(sum(closes[-20:]) / min(20, len(closes[-20:])), 2) if closes[-20:] else d['close']
            ma60 = round(sum(closes[-60:]) / min(60, len(closes[-60:])), 2) if closes[-60:] else d['close']

            # 收益率
            ret_1d = (closes[-1] / closes[-2] - 1) if len(closes) >= 2 else 0
            ret_5d = (closes[-1] / closes[-6] - 1) if len(closes) >= 6 else 0
            ret_20d = (closes[-1] / closes[-21] - 1) if len(closes) >= 21 else 0

            # 波动率
            if len(closes) >= 21:
                rets = [(closes[i] / closes[i - 1] - 1) for i in range(-20, 0)]
                vol = statistics.stdev(rets) * (252 ** 0.5) if len(rets) >= 5 else 0.25
            else:
                vol = 0.25

            # 市值
            mc = info.get('market_cap', 0)
            pe = info.get('pe', 0)
            pb = info.get('pb', 0)

            market_data[ts_code] = {
                'close': d['close'],
                'open': d['open'],
                'high': d['high'],
                'low': d['low'],
                'volume': d['volume'],
                'amount': d['amount'],
                'change_pct': d.get('change_pct', 0),
                'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
                'volume_ma20': round(sum(volumes[-20:]) / max(20, len(volumes[-20:])), 0) if volumes else 0,
                'return_1d': round(ret_1d, 4),
                'return_5d': round(ret_5d, 4),
                'return_20d': round(ret_20d, 4),
                'return_60d': 0,
                'volatility': round(vol, 4),
                'pe': pe, 'pb': pb,
                'ep': round(1 / pe, 4) if pe and pe > 0 else 0,
                'roe': info.get('roe', 0),
                'profit_growth': info.get('profit_growth', 0),
                'revenue_growth': info.get('revenue_growth', 0),
                'gross_margin': info.get('gross_margin', 0),
                'pledge_ratio': info.get('pledge_ratio', 0),
                'market_cap': mc,
                'industry': info.get('industry', ''),
                'name': info.get('name', d.get('name', code)),
                'policy_benefit': False,
                'analyst_upgrade': False,
                'insider_buying': False,
                'buyback': False,
                'st_flag': False,
                'main_force_net_3d': 0,
                'northbound_net_3d': 0,
            }

        return market_data

    # ------------------------------------------------------------------
    # 检测器告警
    # ------------------------------------------------------------------

    def _run_detectors(self) -> None:
        """逐日加载快照，跑检测器（MAD + 背离 + 盘口 + 换手）。"""
        if not self._sessions:
            return

        from broker.monitor import QuoteSnapshot, Detector
        from broker.detector import AnomalyAlert
        from broker.detector.divergence import DivergenceDetector
        from broker.detector.turnover import TurnoverDetector
        from broker.detector.orderbook import OrderbookDetector

        self._detector = Detector()
        self._divergence_detector = DivergenceDetector()
        self._turnover_detector = TurnoverDetector()
        self._orderbook_detector = OrderbookDetector()
        self._prev_snapshots = {}

        # 加载 liutong + turnover medians
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

        # 逐日推进
        for day_idx, session in enumerate(self._sessions):
            if self._stop_event.is_set():
                break

            snap_path = os.path.join(session.dir, 'snapshots.jsonl')
            if not os.path.isfile(snap_path):
                continue

            # 先统计行数
            total_lines = 0
            with open(snap_path, 'r', encoding='utf-8') as f:
                for _ in f:
                    total_lines += 1

            # 流式读取，每批 200 条推进，同步更新进度 + 模拟真实速度
            batch: List[dict] = []
            processed = 0
            last_batch_ts: float = 0
            with open(snap_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        s = json.loads(line)
                        batch.append(s)
                        processed += 1
                        if len(batch) >= 200:
                            self._feed_detector_batch(batch, session.date)
                            # 取批次中最后一条的时间作为模拟时钟
                            last_time = batch[-1].get('time', '') if batch else ''
                            if last_time:
                                self._current_time_str = f'{session.date} {last_time}'
                            # 模拟真实速度：根据时间间隔 / speed 延迟
                            batch_ts = batch[-1].get('ts', 0) if batch else 0
                            if last_batch_ts > 0 and batch_ts > last_batch_ts and self._speed > 0:
                                real_gap = batch_ts - last_batch_ts
                                sim_delay = real_gap / self._speed
                                if sim_delay > 0:
                                    time.sleep(min(sim_delay, 2.0))  # 单次最多睡2秒, 避免卡太久
                            last_batch_ts = batch_ts
                            batch = []
                            if total_lines > 0:
                                day_progress = processed / total_lines
                                self._progress = 0.5 + (day_idx + day_progress) / max(self._total_days, 1) * 0.5
                            if self._stop_event.is_set():
                                break
                    except json.JSONDecodeError:
                        continue
                if batch:
                    self._feed_detector_batch(batch, session.date)

    def _feed_detector_batch(self, batch: List[dict], date: str) -> None:
        """将快照批次喂入检测器。"""
        try:
            from broker.monitor import QuoteSnapshot

            curr = {}
            for s in batch:
                code = s['code']
                name = s.get('name', '') or self._stock_names.get(code, code)
                curr[code] = QuoteSnapshot(
                    code=code, name=name,
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

            prev = self._prev_snapshots

            # MAD 检测
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
                        'date': date,
                        'time': snap.time,
                        'type': 'mad',
                        'subtype': level,
                        'code': code,
                        'name': name,
                        'direction': 'buy' if db_val > ds else ('sell' if ds > db_val else 'neutral'),
                        'data': {
                            'price': snap.price,
                            'amount': round(da, 2),
                            'mad_multiple': round(mad, 2),
                            'level': level,
                        },
                    })

            # 异动检测器
            for detector_name, detector in [
                ('divergence', self._divergence_detector),
                ('turnover', self._turnover_detector),
                ('orderbook', self._orderbook_detector),
            ]:
                try:
                    anomaly_alerts = detector.check(curr, prev)
                    for a in anomaly_alerts:
                        self._monitor_alerts.append({
                            'date': date,
                            'time': getattr(a, 'time', ''),
                            'type': f'{a.type}/{a.subtype}',
                            'subtype': a.subtype if hasattr(a, 'subtype') else '',
                            'code': a.code,
                            'name': a.name,
                            'direction': getattr(a, 'direction', 'neutral'),
                            'data': a.data if hasattr(a, 'data') else {},
                        })
                except Exception as e:
                    logger.debug(f'{detector_name} 检测器异常: {e}')

            self._prev_snapshots = curr

        except Exception as e:
            logger.error(f'检测器批次异常: {e}', exc_info=True)

    # ------------------------------------------------------------------
    # 策略回测
    # ------------------------------------------------------------------

    def _run_strategy_backtest(
        self,
        strategy_name: str,
        initial_capital: float,
        commission_rate: float,
        slippage_rate: float,
        stop_loss_rate: float,
        min_commission: float,
        max_positions: int,
    ) -> None:
        """对单个策略跑 BacktestEngine。"""
        try:
            from backtest.engine import BacktestEngine, BacktestConfig
            from strategy import get_strategy

            strategy = get_strategy(strategy_name)
            if strategy is None:
                self._strategy_results[strategy_name] = {
                    'status': 'error', 'error': f'策略不存在: {strategy_name}',
                }
                return

            config = BacktestConfig(
                initial_capital=initial_capital,
                commission_rate=commission_rate,
                slippage_rate=slippage_rate,
                stop_loss_rate=stop_loss_rate,
                min_commission=min_commission,
                max_position_num=max_positions,
                strict_mode=True,
            )

            engine = BacktestEngine(config)
            result = engine.run(self._market_data_by_date, strategy, print_report=False)

            self._strategy_results[strategy_name] = {
                'status': 'completed',
                'trades': [self._serialize_trade(r) for r in engine.trade_records],
                'signals': engine.daily_operations if hasattr(engine, 'daily_operations') else [],
                'metrics': result.get('metrics', {}),
                'positions': result.get('final_portfolio', {}),
                'daily_nav': result.get('daily_nav', []),
            }

            logger.info(f'{strategy_name}: {len(engine.trade_records)} 笔交易')

        except Exception as e:
            logger.error(f'{strategy_name} 回测异常: {e}', exc_info=True)
            self._strategy_results[strategy_name] = {
                'status': 'error', 'error': str(e),
            }

    def _serialize_trade(self, record) -> dict:
        """序列化 TradeRecord 为 JSON 安全的 dict。"""
        ts_code = getattr(record, 'ts_code', '')
        side = getattr(record, 'side', '')
        # 从 stock_info 映射补名称（TradeRecord 不含 name）
        code = ts_code.split('.')[0] if '.' in ts_code else ts_code
        stock_name = self._stock_names.get(code, '')
        trade_date = getattr(record, 'trade_date', '')
        # 日频策略，交易时间为当日 15:00（收盘执行）
        trade_time = f'{trade_date} 09:30:00' if trade_date else ''
        return {
            'ts_code': ts_code,
            'name': stock_name,
            'direction': side,  # 'BUY' / 'SELL'
            'price': getattr(record, 'price', 0),
            'quantity': getattr(record, 'quantity', 0),
            'amount': getattr(record, 'amount', 0),
            'commission': getattr(record, 'commission', 0),
            'slippage': getattr(record, 'slippage', 0),
            'date': trade_date,
            'time': trade_time,
            'reason': getattr(record, 'reason', ''),
        }

    # ------------------------------------------------------------------
    # 查询结果
    # ------------------------------------------------------------------

    def get_results(self) -> Dict[str, Any]:
        """获取完整回放结果。"""
        return {
            'status': 'completed' if self._progress >= 1.0 else ('running' if self._running else 'stopped'),
            'sessions': self._session_dates,
            'current_day': self._current_day,
            'total_days': self._total_days,
            'progress': round(self._progress * 100, 1),
            'alert_summary': self.get_alert_summary(),
            'alerts': self.get_alerts(),
            'strategies': self._sanitize_results(self._strategy_results),
        }

    @staticmethod
    def _sanitize_results(results: dict) -> dict:
        """递归替换 NaN/Infinity 为 JSON 安全值。"""
        import math
        def _clean(v):
            if isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    return 0.0
                return v
            if isinstance(v, dict):
                return {k: _clean(vv) for k, vv in v.items()}
            if isinstance(v, list):
                return [_clean(vv) for vv in v]
            return v
        return _clean(results)

    def get_alerts(self, limit: int = 500) -> List[dict]:
        """获取检测器告警。"""
        return self._monitor_alerts[-limit:]

    def get_alert_summary(self) -> Dict[str, int]:
        """告警类型汇总。"""
        return dict(Counter(
            a['type'] for a in self._monitor_alerts
        ))

    def get_progress(self) -> Dict[str, Any]:
        """回放进度。"""
        # 计算模拟结束时间
        end_time = ''
        if self._sessions:
            last_date = self._sessions[-1].date
            end_time = f'{last_date} 15:00:00'
        return {
            'running': self.is_running,
            'speed': self._speed,
            'progress': round(self._progress * 100, 1),
            'current_day': self._current_day,
            'total_days': self._total_days,
            'current_time': self._current_time_str,
            'end_time': end_time,
            'days': [s.date for s in self._sessions],
            'alerts': len(self._monitor_alerts),
            'strategies': {
                name: {
                    'status': r.get('status', ''),
                    'trades': len(r.get('trades', [])),
                }
                for name, r in self._strategy_results.items()
            },
        }


# ======================================================================
# 辅助 dataclass
# ======================================================================

class _SessionData:
    """录制会话内部表示。"""

    __slots__ = ('date', 'dir', 'meta', 'stock_pool')

    def __init__(self, date: str, dir: str, meta: dict, stock_pool: List[str]):
        self.date = date
        self.dir = dir
        self.meta = meta
        self.stock_pool = stock_pool
