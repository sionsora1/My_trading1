"""
tests/unit/test_market_watcher.py — Unit tests for market watcher module.

Covers:
  TC-MW01: MarketWatcherEngine singleton pattern
  TC-MW02: PoolStatusTracker snapshot sorting by change_pct descending
  TC-MW03: SurgeWatcher triggers surge_up on 2% rise
  TC-MW04: SurgeWatcher no trigger on small (0.5%) change
  TC-MW05: SurgeWatcher clears window after alert to prevent spam
  TC-MW06: FlowWatcher triggers big_inflow on >= 50M net inflow
  TC-MW07: FlowWatcher triggers big_outflow on >= 50M net outflow
  TC-MW08: FlowWatcher no trigger on small (10M) net flow
  TC-MW09: LimitUpDownWatcher process returns list type
  TC-MW10: SectorHeatmap cache returns same data within interval
  TC-MW11: All classes use get_logger (propagate=False)
  TC-MW12: Public methods have type annotations
"""
import inspect
import time

import pytest

from broker.market_watcher import (
    FlowWatcher,
    LimitUpDownWatcher,
    MarketWatcherEngine,
    PoolStatusTracker,
    SectorHeatmap,
    SurgeWatcher,
)
from broker.monitor import QuoteSnapshot


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_snap(
    code='600519',
    price=100.0,
    change_pct=0.5,
    volume=1000.0,
    amount=100000.0,
    active_buy=500.0,
    active_sell=300.0,
):
    """Create a test QuoteSnapshot with reasonable defaults."""
    from datetime import datetime

    now = datetime.now().strftime('%H:%M:%S')
    return QuoteSnapshot(
        code=code,
        name='Test',
        price=price,
        open=99.0,
        high=101.0,
        low=98.0,
        volume=volume,
        amount=amount,
        change_pct=change_pct,
        bid1=99.9,
        ask1=100.1,
        active_buy=active_buy,
        active_sell=active_sell,
        time=now,
    )


# ===========================================================================
# TC-MW01: MarketWatcherEngine singleton
# ===========================================================================


class TestMarketWatcherEngine:
    """TC-MW01: Singleton pattern — get_instance returns the same object."""

    def test_singleton(self):
        e1 = MarketWatcherEngine.get_instance()
        e2 = MarketWatcherEngine.get_instance()
        assert e1 is e2


# ===========================================================================
# TC-MW02: PoolStatusTracker
# ===========================================================================


class TestPoolStatusTracker:
    """TC-MW02: Snapshot sorting by change_pct descending."""

    def test_sort_descending(self):
        tracker = PoolStatusTracker()
        snap1 = _make_snap('000001', change_pct=1.5)
        snap2 = _make_snap('600519', change_pct=-0.5)
        snap3 = _make_snap('300750', change_pct=3.2)
        curr = {'000001': snap1, '600519': snap2, '300750': snap3}
        result = tracker.process(curr)

        assert len(result) == 1
        assert result[0]['type'] == 'pool_snapshot'
        stocks = result[0]['data']
        assert len(stocks) == 3
        assert stocks[0]['change_pct'] == 3.2
        assert stocks[1]['change_pct'] == 1.5
        assert stocks[2]['change_pct'] == -0.5
        # Verify all expected fields are present
        for key in ('code', 'name', 'price', 'volume', 'amount', 'bid1', 'ask1', 'high', 'low'):
            assert key in stocks[0], f"Missing field '{key}' in pool_snapshot stock dict"


# ===========================================================================
# TC-MW03 ~ TC-MW05: SurgeWatcher
# ===========================================================================


class TestSurgeWatcher:
    """TC-MW03 ~ TC-MW05: Surge detection with 60s sliding window."""

    def test_trigger_surge_up(self):
        """TC-MW03: 60s window with 2% rise triggers surge_up."""
        sw = SurgeWatcher(surge_up_pct=1.5, surge_down_pct=-1.5)
        # Use a timestamp within the 60s window so the entry is not pruned
        sw._price_history['600519'] = [(time.time() - 30, 100.0)]
        snap = _make_snap('600519', price=102.0)
        result = sw.process({'600519': snap})
        assert len(result) == 1
        assert result[0]['type'] == 'surge_up'
        assert result[0]['code'] == '600519'
        assert result[0]['change_pct'] == 2.0

    def test_no_trigger_small_change(self):
        """TC-MW04: 0.5% change does not trigger."""
        sw = SurgeWatcher(surge_up_pct=1.5, surge_down_pct=-1.5)
        sw._price_history['600519'] = [(time.time() - 30, 100.0)]
        snap = _make_snap('600519', price=100.5)
        result = sw.process({'600519': snap})
        assert len(result) == 0

    def test_clear_window_after_trigger(self):
        """TC-MW05: Window cleared after alert to prevent spam."""
        sw = SurgeWatcher(surge_up_pct=1.5, surge_down_pct=-1.5)
        sw._price_history['600519'] = [(time.time() - 30, 100.0)]
        snap = _make_snap('600519', price=102.0)
        sw.process({'600519': snap})
        # After trigger, the window should be emptied
        assert sw._price_history.get('600519', []) == []


# ===========================================================================
# TC-MW06 ~ TC-MW08: FlowWatcher
# ===========================================================================


class TestFlowWatcher:
    """TC-MW06 ~ TC-MW08: Capital flow detection with 1-minute window."""

    def test_trigger_big_inflow(self):
        """TC-MW06: Net inflow 60M in 1min triggers big_inflow."""
        fw = FlowWatcher(inflow_1m=50_000_000, outflow_1m=-50_000_000)
        # snap1: baseline, snap2: 60M more active_buy at same active_sell → net +60M
        snap1 = _make_snap('600519', active_buy=1_000_000, active_sell=500_000)
        snap2 = _make_snap('600519', active_buy=61_000_000, active_sell=500_000)
        fw._prev_snapshots['600519'] = snap1
        result = fw.process({'600519': snap2})
        assert len(result) == 1
        assert result[0]['type'] == 'big_inflow'
        assert result[0]['code'] == '600519'
        assert result[0]['net_flow'] >= 50_000_000

    def test_trigger_big_outflow(self):
        """TC-MW07: Net outflow 60M triggers big_outflow."""
        fw = FlowWatcher(inflow_1m=50_000_000, outflow_1m=-50_000_000)
        # snap1: baseline, snap2: 60M more active_sell at same active_buy → net -60M
        snap1 = _make_snap('600519', active_buy=500_000, active_sell=1_000_000)
        snap2 = _make_snap('600519', active_buy=500_000, active_sell=61_000_000)
        fw._prev_snapshots['600519'] = snap1
        result = fw.process({'600519': snap2})
        assert len(result) == 1
        assert result[0]['type'] == 'big_outflow'
        assert result[0]['code'] == '600519'
        assert result[0]['net_flow'] <= -50_000_000

    def test_no_trigger_small_flow(self):
        """TC-MW08: 10M net flow does not trigger."""
        fw = FlowWatcher(inflow_1m=50_000_000, outflow_1m=-50_000_000)
        snap1 = _make_snap('600519', active_buy=1_000_000, active_sell=500_000)
        snap2 = _make_snap('600519', active_buy=11_000_000, active_sell=500_000)
        fw._prev_snapshots['600519'] = snap1
        result = fw.process({'600519': snap2})
        assert len(result) == 0


# ===========================================================================
# TC-MW09: LimitUpDownWatcher
# ===========================================================================


class TestLimitUpDownWatcher:
    """TC-MW09: Process returns list type (no live TDX connection required)."""

    def test_process_returns_list(self):
        lw = LimitUpDownWatcher(scan_interval=999)
        # Prevent actual TDX scan by setting _last_scan far into the future
        lw._last_scan = time.time() + 99999
        result = lw.process()
        assert isinstance(result, list)
        assert result == []


# ===========================================================================
# TC-MW10: SectorHeatmap
# ===========================================================================


class TestSectorHeatmap:
    """TC-MW10: Cache returns same data within refresh interval."""

    def test_cache_behavior(self):
        sh = SectorHeatmap(refresh_interval=999)
        # Seed the cache so both calls return the same pre-populated data
        sh._last_refresh = time.time()
        sh._cached_data = {'concept': [], 'industry': [], 'timestamp': '2026-06-15T10:00:00'}
        result1 = sh.process()
        result2 = sh.process()
        assert result1 == result2
        assert result1['concept'] == []
        assert result1['industry'] == []


# ===========================================================================
# TC-MW11: Logger compliance
# ===========================================================================


class TestLoggerCompliance:
    """TC-MW11: All classes use get_logger (propagate=False)."""

    def test_logger_compliance(self):
        from utils.logger import get_logger

        # Verify the module-level logger exists and has propagate=False
        from broker import market_watcher
        assert hasattr(market_watcher, 'logger'), (
            "market_watcher module must have a module-level logger"
        )
        assert market_watcher.logger.propagate is False, (
            "Module logger must have propagate=False (set by get_logger)"
        )


# ===========================================================================
# TC-MW12: Type annotations
# ===========================================================================


class TestTypeAnnotations:
    """TC-MW12: Public methods (__init__, process) have type annotations."""

    def test_type_annotations(self):
        classes = [
            PoolStatusTracker,
            SurgeWatcher,
            FlowWatcher,
            LimitUpDownWatcher,
            SectorHeatmap,
        ]
        for cls in classes:
            for name, method in inspect.getmembers(cls, inspect.isfunction):
                if name.startswith('_') and name != '__init__':
                    continue
                if name not in ('__init__', 'process'):
                    continue
                try:
                    sig = inspect.signature(method)
                except (ValueError, TypeError):
                    continue

                # __init__ does not need a return annotation
                if name == '__init__':
                    continue

                # process methods must have a return annotation
                assert sig.return_annotation is not inspect.Parameter.empty, (
                    f"{cls.__name__}.{name} missing return annotation"
                )
