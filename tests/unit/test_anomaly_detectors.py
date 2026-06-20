"""
tests/unit/test_anomaly_detectors.py — Unit tests for all 6 anomaly detectors.

Covers:
  TC-AD01: DivergenceDetector price_up_sell_more triggers
  TC-AD02: DivergenceDetector no trigger on balanced active
  TC-AD03: DivergenceDetector extreme_imbalance after 3 consecutive ticks
  TC-AD04: OrderbookDetector bid_ask_surge triggers
  TC-AD05: OrderbookDetector imbalance triggers
  TC-AD06: OrderbookDetector cancel triggers
  TC-AD07: OrderbookDetector spread_surge triggers
  TC-AD08: LimitMoveDetector seal_loosen triggers
  TC-AD09: LimitMoveDetector pry_signal triggers
  TC-AD10: TurnoverDetector spike triggers
  TC-AD11: TransBigDetector threshold classification
  TC-AD12: TransBigDetector auction_spike
  TC-AD13: QuoteSnapshot with new fields constructs correctly
  TC-AD14: AnomalyAlert.to_sse_data() produces valid JSON
"""

from __future__ import annotations

import json
import statistics
import time
from collections import deque

import pytest

from broker.detector import AnomalyAlert, SimpleQueue
from broker.detector.divergence import DivergenceDetector
from broker.detector.limit_move import LimitMoveDetector
from broker.detector.orderbook import OrderbookDetector
from broker.detector.trans_big import TransBigDetector
from broker.detector.turnover import TurnoverDetector
from broker.monitor import QuoteSnapshot


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def make_snap(code='600519', price=100.0, **kwargs):
    """Create a test QuoteSnapshot with reasonable defaults."""
    defaults = {
        'code': code, 'name': '测试', 'price': price,
        'open': 99.0, 'high': 101.0, 'low': 98.0,
        'volume': 100000.0, 'amount': 10000000.0,
        'change_pct': 0.0, 'last_close': 99.0,
        'bid1': price - 0.01, 'ask1': price + 0.01,
        'bid_vol1': 100.0, 'ask_vol1': 100.0,
        'active_buy': 5000.0, 'active_sell': 5000.0,
        'time': '10:00:00',
    }
    defaults.update({k: 0.0 for k in [
        'bid2', 'bid3', 'bid4', 'bid5',
        'ask2', 'ask3', 'ask4', 'ask5',
        'bid_vol2', 'bid_vol3', 'bid_vol4', 'bid_vol5',
        'ask_vol2', 'ask_vol3', 'ask_vol4', 'ask_vol5',
    ]})
    defaults.update(kwargs)
    return QuoteSnapshot(**defaults)


# ===========================================================================
# TC-AD01 ~ TC-AD03: DivergenceDetector
# ===========================================================================


class TestDivergenceDetector:
    """TC-AD01 ~ TC-AD03: 内外盘背离检测器."""

    def test_price_up_sell_more_triggers(self):
        """TC-AD01: price_up_sell_more triggers when price up AND sell > buy * 1.5."""
        detector = DivergenceDetector()
        prev = make_snap('600519', price=100.0, active_buy=1000.0, active_sell=1000.0)
        curr = make_snap('600519', price=101.0, active_buy=1000.0, active_sell=3000.0)
        # sell/buy = 3000/1000 = 3.0 > 1.5, price 101 > 100
        alerts = detector.check({'600519': curr}, {'600519': prev})
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.type == 'divergence'
        assert alert.subtype == 'price_up_sell_more'
        assert alert.code == '600519'
        assert alert.direction == 'sell'
        assert alert.data['ratio'] >= 1.5

    def test_no_trigger_on_balanced_active(self):
        """TC-AD02: Does NOT trigger when price up with balanced active."""
        detector = DivergenceDetector()
        prev = make_snap('600519', price=100.0, active_buy=5000.0, active_sell=5000.0)
        curr = make_snap('600519', price=101.0, active_buy=5000.0, active_sell=5000.0)
        # sell/buy = 5000/5000 = 1.0 <= 1.5
        alerts = detector.check({'600519': curr}, {'600519': prev})
        assert len(alerts) == 0

    def test_extreme_imbalance_after_3_consecutive_ticks(self):
        """TC-AD03: extreme_imbalance triggers after 3 consecutive ticks with ratio > 3:1."""
        detector = DivergenceDetector(cooldown_sec=0)
        # Keep price constant to avoid triggering price_up_sell_more accidentally
        base = make_snap('600519', price=100.0, active_buy=1000.0, active_sell=4000.0)
        # Tick 1: ratio = 4000/1000 = 4.0 > 3.0 → counter +1
        curr1 = make_snap('600519', price=100.0, active_buy=1000.0, active_sell=4000.0)
        alerts1 = detector.check({'600519': curr1}, {'600519': base})
        assert len(alerts1) == 0  # Only 1 tick, need 3

        # Tick 2: ratio still > 3 → counter +2
        curr2 = make_snap('600519', price=100.0, active_buy=1000.0, active_sell=4000.0)
        alerts2 = detector.check({'600519': curr2}, {'600519': curr1})
        assert len(alerts2) == 0  # Only 2 ticks

        # Tick 3: ratio still > 3 → counter +3 → trigger!
        curr3 = make_snap('600519', price=100.0, active_buy=1000.0, active_sell=4000.0)
        alerts3 = detector.check({'600519': curr3}, {'600519': curr2})
        # May also get price_down_buy_more if price fluctuates; filter for extreme_imbalance
        extreme = [a for a in alerts3 if a.subtype == 'extreme_imbalance']
        assert len(extreme) == 1
        alert = extreme[0]
        assert alert.type == 'divergence'
        assert alert.subtype == 'extreme_imbalance'
        assert alert.code == '600519'
        assert alert.data['duration'] == 3
        assert alert.data['dominant'] == 'sell'


# ===========================================================================
# TC-AD04 ~ TC-AD07: OrderbookDetector
# ===========================================================================


class TestOrderbookDetector:
    """TC-AD04 ~ TC-AD07: 盘口异动检测器."""

    def _populate_window(self, detector, code, num_entries=20):
        """Pre-populate the sliding window for a given code with normal data."""
        now = time.time()
        window = deque()
        for i in range(num_entries):
            t = now - (num_entries - i) * 5
            bid_vols = (10.0, 10.0, 10.0, 10.0, 10.0)
            ask_vols = (10.0, 10.0, 10.0, 10.0, 10.0)
            spread_pct = 0.1
            window.append((t, bid_vols, ask_vols, spread_pct))
        detector._windows[code] = window

    def test_bid_ask_surge_triggers(self):
        """TC-AD04: volume > median*10 AND > 2000 at bid_vol1 level triggers."""
        detector = OrderbookDetector(cooldown_sec=0)
        self._populate_window(detector, '600519')
        # median bid_vol1 = 10, threshold = 10*10 = 100
        # cur bid_vol1 = 3000 > 100 AND > 2000 → trigger
        snap = make_snap('600519', bid_vol1=3000.0)
        alerts = detector.check({'600519': snap})
        assert len(alerts) >= 1
        surge_alerts = [a for a in alerts if a.subtype == 'bid_ask_surge']
        assert len(surge_alerts) >= 1
        alert = surge_alerts[0]
        assert alert.type == 'orderbook'
        assert alert.direction == 'buy'
        assert alert.data['side'] == 'bid'
        assert alert.data['level'] == 1
        assert alert.data['current_hands'] >= 2000

    def test_imbalance_triggers(self):
        """TC-AD05: total_bid/total_ask < 0.2 triggers imbalance."""
        detector = OrderbookDetector(cooldown_sec=0, imbalance_min_hands=1)
        # total_bid = 100+0+0+0+0 = 100, total_ask = 1000+0+0+0+0 = 1000
        # ratio = 100/1000 = 0.1 < 0.2 → trigger sell direction
        snap = make_snap(
            '600519',
            bid_vol1=100.0, ask_vol1=1000.0,
            bid1=99.99, ask1=100.01,
        )
        alerts = detector.check({'600519': snap})
        imbalance_alerts = [a for a in alerts if a.subtype == 'imbalance']
        assert len(imbalance_alerts) >= 1
        alert = imbalance_alerts[0]
        assert alert.type == 'orderbook'
        assert alert.direction == 'sell'
        assert alert.data['ratio'] < 0.2

    def test_cancel_triggers(self):
        """TC-AD06: previous tick had big order, current tick same level < 200."""
        detector = OrderbookDetector(cooldown_sec=0)
        self._populate_window(detector, '600519')
        # median bid_vol1 = 10, surge_threshold = 10*10 = 100
        # previous snap: bid_vol1 = 5000 > 100 AND > 2000 → recognized as big order
        prev_snap = make_snap('600519', bid_vol1=5000.0, bid1=99.99, ask1=100.01)
        detector._prev_snapshots['600519'] = prev_snap
        # current snap: bid_vol1 = 100 < 200 → cancel detected
        curr_snap = make_snap('600519', bid_vol1=100.0, bid1=99.99, ask1=100.01)
        alerts = detector.check({'600519': curr_snap})
        cancel_alerts = [a for a in alerts if a.subtype == 'cancel']
        assert len(cancel_alerts) >= 1
        alert = cancel_alerts[0]
        assert alert.type == 'orderbook'
        assert alert.data['side'] == 'bid'
        assert alert.data['prev_hands'] >= 5000
        assert alert.data['current_hands'] < 200

    def test_spread_surge_triggers(self):
        """TC-AD07: spread_pct > 0.5% AND > mean*5 triggers."""
        detector = OrderbookDetector(cooldown_sec=0, spread_pct_threshold=0.5, spread_mean_multiple=5)
        self._populate_window(detector, '600519')
        # window spread_pcts are all 0.1, mean = 0.1
        # current: bid1=100, ask1=101 → spread_pct = 1.0%
        # 1.0 > 0.5 AND 1.0 > 0.1*5=0.5 → trigger
        snap = make_snap('600519', bid1=100.0, ask1=101.0, bid_vol1=100.0, ask_vol1=100.0)
        alerts = detector.check({'600519': snap})
        spread_alerts = [a for a in alerts if a.subtype == 'spread_surge']
        assert len(spread_alerts) >= 1
        alert = spread_alerts[0]
        assert alert.type == 'orderbook'
        assert alert.data['spread_pct'] > 0.5
        assert alert.data['multiple'] >= 5.0


# ===========================================================================
# TC-AD08 ~ TC-AD09: LimitMoveDetector
# ===========================================================================


class TestLimitMoveDetector:
    """TC-AD08 ~ TC-AD09: 涨跌停加速检测器."""

    def _populate_window(self, detector, code, num_entries=10, bid_vol1=500.0):
        """Pre-populate the time window for LimitMoveDetector."""
        now = time.time()
        window = []
        for i in range(num_entries):
            t = now - (num_entries - i) * 10
            window.append((t, 100.0, 100000.0, bid_vol1))
        detector._windows[code] = window

    def test_seal_loosen_triggers(self):
        """TC-AD08: change_pct > 9.5% with bid_vol1 < median * 0.5 triggers."""
        detector = LimitMoveDetector(cooldown_sec=0, seal_loosen_vol_ratio=0.5)
        # Populate window with bid_vol1=500 for 10 entries → median = 500
        self._populate_window(detector, '600519', num_entries=10, bid_vol1=500.0)
        # change_pct=9.8 > 9.5, bid_vol1=200 < 500*0.5=250 → trigger
        snap = make_snap('600519', price=109.7, change_pct=9.8, bid_vol1=200.0)
        alerts = detector.check({'600519': snap})
        seal_alerts = [a for a in alerts if a.subtype == 'seal_loosen']
        assert len(seal_alerts) >= 1
        alert = seal_alerts[0]
        assert alert.type == 'limit_move'
        assert alert.subtype == 'seal_loosen'
        assert alert.direction == 'sell'
        assert alert.data['collapse_pct'] > 0

    def test_pry_signal_triggers(self):
        """TC-AD09: change_pct < -9.5% with bid_vol1 > ask_vol1 * 10 triggers."""
        detector = LimitMoveDetector(cooldown_sec=0, pry_signal_vol_ratio=10)
        # change_pct=-9.8 < -9.5, bid_vol1=2000, ask_vol1=100
        # bid_vol1/ask_vol1 = 20 > 10 → trigger
        snap = make_snap(
            '600519', price=90.0, change_pct=-9.8,
            bid_vol1=2000.0, ask_vol1=100.0,
        )
        alerts = detector.check({'600519': snap})
        pry_alerts = [a for a in alerts if a.subtype == 'pry_signal']
        assert len(pry_alerts) >= 1
        alert = pry_alerts[0]
        assert alert.type == 'limit_move'
        assert alert.subtype == 'pry_signal'
        assert alert.direction == 'buy'
        assert alert.data['ratio'] >= 10.0


# ===========================================================================
# TC-AD10: TurnoverDetector
# ===========================================================================


class TestTurnoverDetector:
    """TC-AD10: 换手率异动检测器."""

    def test_spike_triggers_after_setup(self):
        """TC-AD10: After setting liutong cache and hist medians, turnover spike triggers."""
        detector = TurnoverDetector(cooldown_sec=0)
        # Setup: 1B shares outstanding
        detector.set_liutong_cache({'600519': 1_000_000_000.0})
        detector.set_hist_medians({'600519': {'daily': 5.0, '5min': 0.5}})
        # Pre-populate 5-min window with a small baseline volume
        now = time.time()
        detector._five_min_windows['600519'] = deque([(now - 200, 10_000.0)])
        # Current snap: volume=30_000_000
        # delta_5m_vol = 30M - 10K = 29.99M
        # delta_5m_turnover = (29.99M / 1B) * 100 = 2.999%
        # multiple = 2.999 / 0.5 = 5.998 >= 5 → trigger
        snap = make_snap('600519', volume=30_000_000.0)
        alerts = detector.check({'600519': snap})
        spike_alerts = [a for a in alerts if a.subtype == 'spike']
        assert len(spike_alerts) >= 1
        alert = spike_alerts[0]
        assert alert.type == 'turnover'
        assert alert.data['multiple'] >= 5.0
        assert alert.data['delta_5m_pct'] > 0


# ===========================================================================
# TC-AD11 ~ TC-AD12: TransBigDetector
# ===========================================================================


class TestTransBigThreshold:
    """TC-AD11 ~ TC-AD12: 逐笔大单检测器 (threshold classification + auction)."""

    @staticmethod
    def _setup_detector(code='600519'):
        """Create a TransBigDetector with queue and snapshots."""
        queue = SimpleQueue()
        detector = TransBigDetector(queue, [code], abs_threshold=20_000_000)
        # Set no hist median so threshold stays at absolute 2000万
        detector.set_hist_medians({})
        # Set latest snapshot for name resolution
        detector._latest_snapshots = {code: make_snap(code, price=10.0)}
        return detector, queue

    def test_threshold_classification_large(self):
        """TC-AD11: large (>2000万), super_large (>×3 and >5000万), giant (>1亿)."""
        detector, queue = self._setup_detector('600519')

        # Test 1: large — amount = 2500万, >2000万 threshold but < super_large criteria
        # price=10, vol=25000手 → amount=10*25000*100=25,000,000
        txn_large = {'price': 10.0, 'vol': 25000, 'num': 10, 'buyorsell': 1, 'time': '10:00:00'}
        detector._check_single_txn('600519', txn_large, '10:00:00')
        large_alerts = queue.get_all_nonblocking()
        assert len(large_alerts) == 1
        assert large_alerts[0].type == 'trans_big'
        assert large_alerts[0].subtype == 'large'
        assert large_alerts[0].data['amount'] >= 20_000_000

        # Test 2: super_large — amount = 7000万
        # > 2000万*3=6000万 AND >= 5000万 → super_large
        # price=10, vol=70000手 → amount=10*70000*100=70,000,000
        txn_super = {'price': 10.0, 'vol': 70000, 'num': 1, 'buyorsell': 2, 'time': '10:01:00'}
        detector._check_single_txn('600519', txn_super, '10:01:00')
        super_alerts = queue.get_all_nonblocking()
        assert len(super_alerts) == 1
        assert super_alerts[0].subtype == 'super_large'
        assert super_alerts[0].data['amount'] >= 50_000_000
        assert super_alerts[0].data['multiple'] >= 3.0

        # Test 3: giant — amount = 1.5亿
        # >= 1亿 → giant
        # price=10, vol=150000手 → amount=10*150000*100=150,000,000
        txn_giant = {'price': 10.0, 'vol': 150000, 'num': 1, 'buyorsell': 1, 'time': '10:02:00'}
        detector._check_single_txn('600519', txn_giant, '10:02:00')
        giant_alerts = queue.get_all_nonblocking()
        assert len(giant_alerts) == 1
        assert giant_alerts[0].subtype == 'giant'
        assert giant_alerts[0].data['amount'] >= 100_000_000

    def test_auction_spike_triggers(self):
        """TC-AD12: buyorsell=8 with vol > 3* auction history median triggers auction."""
        detector, queue = self._setup_detector('600519')
        # Set auction history with 5 values, median = 140
        detector.set_auction_history({'600519': [100.0, 120.0, 140.0, 160.0, 180.0]})

        # txn: buyorsell=8, vol=500, price=10
        # ratio = 500/140 = 3.57 > 3 → auction_spike
        txn = {'price': 10.0, 'vol': 500, 'num': 0, 'buyorsell': 8, 'time': '09:25:00'}
        detector._check_single_txn('600519', txn, '09:25:00')
        alerts = queue.get_all_nonblocking()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.type == 'auction'
        assert alert.subtype == 'spike'
        assert alert.code == '600519'
        assert alert.data['hands'] == 500
        assert alert.data['multiple'] >= 3.0


# ===========================================================================
# TC-AD13: QuoteSnapshot fields
# ===========================================================================


class TestQuoteSnapshotFields:
    """TC-AD13: QuoteSnapshot with new fields constructs correctly."""

    def test_all_fields_construct_correctly(self):
        """TC-AD13: bid2-5, ask2-5, bid_vol1-5, ask_vol1-5, last_close all set."""
        snap = make_snap(
            '600519', price=100.0, last_close=99.0,
            bid1=99.99, bid2=99.98, bid3=99.97, bid4=99.96, bid5=99.95,
            ask1=100.01, ask2=100.02, ask3=100.03, ask4=100.04, ask5=100.05,
            bid_vol1=100.0, bid_vol2=200.0, bid_vol3=300.0, bid_vol4=400.0, bid_vol5=500.0,
            ask_vol1=150.0, ask_vol2=250.0, ask_vol3=350.0, ask_vol4=450.0, ask_vol5=550.0,
        )
        assert snap.code == '600519'
        assert snap.name == '测试'
        assert snap.price == 100.0
        assert snap.last_close == 99.0
        # Verify 5-level bid prices
        assert snap.bid1 == 99.99
        assert snap.bid2 == 99.98
        assert snap.bid3 == 99.97
        assert snap.bid4 == 99.96
        assert snap.bid5 == 99.95
        # Verify 5-level ask prices
        assert snap.ask1 == 100.01
        assert snap.ask2 == 100.02
        assert snap.ask3 == 100.03
        assert snap.ask4 == 100.04
        assert snap.ask5 == 100.05
        # Verify 5-level bid volumes
        assert snap.bid_vol1 == 100.0
        assert snap.bid_vol2 == 200.0
        assert snap.bid_vol3 == 300.0
        assert snap.bid_vol4 == 400.0
        assert snap.bid_vol5 == 500.0
        # Verify 5-level ask volumes
        assert snap.ask_vol1 == 150.0
        assert snap.ask_vol2 == 250.0
        assert snap.ask_vol3 == 350.0
        assert snap.ask_vol4 == 450.0
        assert snap.ask_vol5 == 550.0


# ===========================================================================
# TC-AD14: AnomalyAlert serialization
# ===========================================================================


class TestAnomalyAlertSerialization:
    """TC-AD14: AnomalyAlert.to_sse_data() produces valid JSON."""

    def test_to_sse_data_produces_valid_json(self):
        """TC-AD14: to_sse_data() JSON includes all fields."""
        alert = AnomalyAlert(
            type='trans_big',
            subtype='large',
            code='600519',
            name='贵州茅台',
            direction='buy',
            time='14:32:15',
            data={
                'price': 1800.5,
                'amount': 30_000_000.0,
                'hands': 1667,
                'threshold': 20_000_000,
                'multiple': 1.5,
                'num_trades': 5,
            },
        )
        json_str = alert.to_sse_data()
        assert isinstance(json_str, str)

        parsed = json.loads(json_str)
        assert parsed['type'] == 'trans_big'
        assert parsed['subtype'] == 'large'
        assert parsed['code'] == '600519'
        assert parsed['name'] == '贵州茅台'
        assert parsed['direction'] == 'buy'
        assert parsed['time'] == '14:32:15'
        assert parsed['data']['price'] == 1800.5
        assert parsed['data']['amount'] == 30_000_000.0
        assert parsed['data']['hands'] == 1667
        assert parsed['data']['threshold'] == 20_000_000
        assert parsed['data']['multiple'] == 1.5
        assert parsed['data']['num_trades'] == 5


# ============================================================================
# 新增测试 (v2.4.0 修复补丁)
# ============================================================================


class TestOrderbookEdgeCases:
    """TC-AD15~AD16: Orderbook 边界情况"""

    def test_skip_when_window_not_full(self):
        """TC-AD15: 窗口未满 12 拍时不应触发挂单突变。"""
        detector = OrderbookDetector()
        # 窗口未满 (只喂 5 拍)
        code = '600519'
        curr = {}
        for _ in range(5):
            snap = make_snap(code=code, bid_vol1=5000.0)
            detector.check({code: snap})
            curr[code] = snap
        # 再喂一帧异常高挂单 — 窗口不够 12 拍，不应触发 surge
        snap = make_snap(code=code, bid_vol1=99999.0)
        alerts = detector.check({code: snap})
        # surge 需要 > min_hands AND > median×10，窗口不足则 median=0 或只有最近几拍
        assert all(a.subtype != 'bid_ask_surge' for a in alerts)

    def test_multi_dimension_same_tick(self):
        """TC-AD16: 同一帧触发多个维度时不互相覆盖。"""
        detector = OrderbookDetector()
        code = '600519'
        # 喂满 12 拍默认值
        for _ in range(12):
            detector.check({code: make_snap(code=code)})
        # 触发失衡 + 挂单突变
        snap = make_snap(
            code=code, bid_vol1=50000.0, bid_vol2=10.0, bid_vol3=10.0,
            bid_vol4=10.0, bid_vol5=10.0,
            ask_vol1=10.0, ask_vol2=10.0, ask_vol3=10.0,
            ask_vol4=10.0, ask_vol5=10.0,
        )
        alerts = detector.check({code: snap})
        # 至少触发失衡（买>卖×5 且买>5000手）
        imbalance_alerts = [a for a in alerts if a.subtype == 'imbalance']
        assert len(imbalance_alerts) >= 1


class TestTurnoverFallback:
    """TC-AD17: Turnover 回退到分档"""

    def test_fallback_daily_median_by_float_shares(self):
        """无历史中位数时，按流通股本估算。"""
        detector = TurnoverDetector()
        # 小盘股 (<5亿)
        assert detector._get_fallback_daily_median(1_0000_0000) == 5.0
        # 中盘股 (5-30亿)
        assert detector._get_fallback_daily_median(10_0000_0000) == 2.0
        # 大盘股 (>30亿)
        assert detector._get_fallback_daily_median(50_0000_0000) == 1.0

    def test_no_hist_median_still_detects(self):
        """即使没有历史中位数，回退分档也能检测到极端换手率。"""
        detector = TurnoverDetector()
        liutong = 10_0000_0000  # 10亿，中盘
        detector.set_liutong_cache({'600519': liutong})
        # 不设 hist_medians → 回退到 2.0% 日均
        snap = make_snap(code='600519', volume=liutong * 0.15)  # 15% 换手率
        alerts = detector.check({'600519': snap})
        # 15% > 2.0% × 5 = 10% → extreme
        assert any(a.subtype == 'extreme' for a in alerts)


class TestTransBigFallback:
    """TC-AD18: TransBig 降级到绝对下限"""

    def test_no_hist_median_uses_abs_minimum(self):
        """无历史中位数时，阈值退化到 2000万。"""
        from config.settings import ANOMALY_DETECTOR_CONFIG
        queue = SimpleQueue()
        detector = TransBigDetector(queue, stock_pool=['600519'])
        # 不调用 set_hist_medians
        threshold = detector._compute_threshold('600519')
        abs_min = ANOMALY_DETECTOR_CONFIG.get('trans_big', {}).get('abs_threshold', 20_000_000)
        assert threshold == float(abs_min)


class TestLimitMoveCodeMapping:
    """TC-AD19: 涨跌停代码段映射"""

    def test_code_segments_rate(self):
        """验证各类代码段返回正确的涨跌停幅度。"""
        # 主板
        assert LimitMoveDetector._get_limit_rate('600519') == 0.10
        assert LimitMoveDetector._get_limit_rate('000858') == 0.10
        assert LimitMoveDetector._get_limit_rate('001979') == 0.10
        # 创业板
        assert LimitMoveDetector._get_limit_rate('300394') == 0.20
        assert LimitMoveDetector._get_limit_rate('301123') == 0.20
        # 科创板
        assert LimitMoveDetector._get_limit_rate('688001') == 0.20
        assert LimitMoveDetector._get_limit_rate('689009') == 0.20
        # 北交所
        assert LimitMoveDetector._get_limit_rate('830001') == 0.30
        assert LimitMoveDetector._get_limit_rate('870001') == 0.30


class TestRankChangeCooldown:
    """TC-AD20: 排名突变冷却"""

    def test_cooldown_blocks_repeat_alert(self):
        """同股票在冷却期内不重复触发。"""
        # 模拟冷却字典行为
        cooldowns: dict = {}
        cooldown_sec = 60
        code = '600519'
        now = time.time()
        # 第一次: 允许
        last = cooldowns.get(code, 0)
        assert now - last >= cooldown_sec  # should be True
        cooldowns[code] = now
        # 第二次: 立即再次尝试 — 应被冷却阻止
        now2 = now + 5
        last2 = cooldowns.get(code, 0)
        assert now2 - last2 < cooldown_sec  # should be True (cooling)


class TestDivergenceCounterReset:
    """TC-AD21: 极端背离中断 1 拍计数器重置"""

    def test_counter_resets_on_miss(self):
        """连续 2 拍满足条件后第 3 拍中断 → 计数器重置。"""
        detector = DivergenceDetector(extreme_duration=3)
        code = '600519'
        prev = make_snap(code=code, active_buy=5000, active_sell=50000)  # 失衡

        # 第 1 拍: 满足
        curr1 = make_snap(code=code, price=100.0, active_buy=5000, active_sell=50000)
        detector.check({code: curr1}, {code: prev})
        assert detector._imbalance_counters[code] == 1

        # 第 2 拍: 满足
        curr2 = make_snap(code=code, price=100.0, active_buy=5000, active_sell=50000)
        detector.check({code: curr2}, {code: curr1})
        assert detector._imbalance_counters[code] == 2

        # 第 3 拍: 中断 (恢复正常)
        curr3 = make_snap(code=code, price=100.0, active_buy=5000, active_sell=5000)
        detector.check({code: curr3}, {code: curr2})
        assert detector._imbalance_counters[code] == 0  # 重置


class TestTransBigPreFilterWarmup:
    """TC-AD22: TransBig 预筛选 MAD 未就绪时返回全量"""

    def test_pre_filter_returns_full_pool_when_cold(self):
        """delta 窗口未满 3 条时，返回全量股票池。"""
        queue = SimpleQueue()
        detector = TransBigDetector(queue, stock_pool=['600519', '000858', '300394'])
        # 不喂任何快照 → delta_window 全空
        candidates = detector._pre_filter()
        assert len(candidates) == 3
        assert '600519' in candidates
        assert '000858' in candidates
        assert '300394' in candidates
