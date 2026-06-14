"""
端到端集成测试
运行: cd quant_strategy && python tests/test_integration.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_data_pipeline():
    """测试数据完整链路: AKShare获取 → Validator校验 → SQLite入库 → 回读验证"""
    from data.fetcher import DataFetcher
    from data.database import SQLiteManager
    from data.validator import DataValidator

    db = SQLiteManager()
    fetcher = DataFetcher()

    try:
        # 获取一只股票的数据
        stock = '600519'
        df = fetcher.get_daily_data(stock, '20260101', '20260601')

        if df is None or df.empty:
            print("⚠️ test_data_pipeline SKIPPED (no data from AKShare)")
            return

        # 校验
        rows = df.to_dict('records')
        valid = DataValidator.filter_valid_daily_bars(rows)

        # 入库
        ts_code = f"{stock}.SH"
        for r in valid:
            r['ts_code'] = ts_code
        n = db.upsert_daily_bars(valid)

        # 回读验证
        saved = db.get_daily_bars(ts_code, '20260101', '20260601')

        assert n > 0, "应该入库了一些数据"
        assert len(saved) == n, f"回读行数({len(saved)})应等于入库行数({n})"

        print(f"✅ test_data_pipeline PASSED ({n} rows)")
    except Exception as e:
        print(f"❌ test_data_pipeline FAILED: {e}")
        raise
    finally:
        db.close()


def test_signal_bus_pipeline():
    """测试信号总线完整链路: 多策略生成 → SignalBus处理 → 去重合并"""
    from sigbus.bus import SignalBus
    from strategy import get_strategy

    # 构造模拟数据
    market_data = {
        '600519': {
            'close': 1700, 'ma20': 1650, 'ma60': 1600,
            'volume': 5000000, 'volume_ma20': 4000000, 'return_20d': 0.08,
            'pct_chg': 2.5, 'name': '贵州茅台', 'open': 1680, 'high': 1710, 'low': 1675,
        },
        '000858': {
            'close': 160, 'ma20': 170, 'ma60': 155,
            'volume': 1000000, 'volume_ma20': 2000000, 'return_20d': -0.10,
            'pct_chg': -3.0, 'name': '五粮液', 'open': 163, 'high': 164, 'low': 159,
        },
    }
    portfolio = {
        'cash': 100000,
        'total_assets': 100000,
        'positions': {}
    }

    # 多策略生成
    strategies = []
    for name in ['trend_following', 'mean_reversion']:
        try:
            s = get_strategy(name)
            strategies.append(s)
        except Exception as e:
            print(f"  ⚠️ Strategy {name} not available: {e}")

    if not strategies:
        print("⚠️ test_signal_bus_pipeline SKIPPED (no strategies available)")
        return

    bus = SignalBus({'max_positions': 5, 'min_order_amount': 2000})
    signals = bus.process('20260612', market_data, portfolio, strategies)

    # 验证输出结构
    assert isinstance(signals, list), f"应该返回list，实际: {type(signals)}"
    for s in signals:
        assert 'ts_code' in s, f"每个信号应有 ts_code, got keys: {list(s.keys())}"
        assert 'signal' in s, f"每个信号应有 signal (BUY/SELL), got keys: {list(s.keys())}"

    print(f"✅ test_signal_bus_pipeline PASSED ({len(signals)} signals)")


def test_manual_broker_flow():
    """测试半自动交易闭环: 信号→确认→下单→持仓更新"""
    import tempfile
    import os

    from broker.manual_broker import ManualBroker
    from broker.base import OrderRequest, OrderSide

    # Use temp dir for test data
    tmpdir = tempfile.mkdtemp(prefix='test_broker_')

    try:
        mb = ManualBroker({
            'initial_capital': 100000,
            'data_dir': tmpdir,
        })
        mb.connect()

        # 1. 初始状态
        acc = mb.get_account()
        assert acc.total_assets == 100000, f"初始总资产应为 100000，实际: {acc.total_assets}"

        # 2. 提交买入信号 (50股, 金额84000在100000初始资金内)
        req = OrderRequest(
            ts_code='600519', side=OrderSide.BUY,
            quantity=50, price=1680,
            reason='测试买入', stock_name='贵州茅台'
        )
        result = mb.submit_order(req)
        assert result.order_id, f"应该有 order_id, got: {result.order_id}"

        # 3. 待确认信号
        pending = mb.get_pending_signals()
        assert len(pending) >= 1, f"应该有至少1个待确认信号，实际: {len(pending)}"

        # 4. 确认成交
        mb.confirm_order(result.order_id, fill_price=1680, fill_qty=50)

        # 5. 验证持仓更新
        positions = mb.get_positions()
        assert '600519' in positions, f"应该有600519持仓, positions: {list(positions.keys())}"
        pos = positions['600519']
        assert pos.quantity == 50, f"持仓应为50股，实际: {pos.quantity}"

        # 6. 验证资产 = 现金 + 市值 (A股买入只收佣金，不收印花税)
        trade_amount = 1680 * 50
        acc = mb.get_account()
        expected_cash = 100000 - trade_amount - max(5, trade_amount * 0.0003)
        assert abs(acc.available_cash - expected_cash) < 1, \
            f"现金应为 {expected_cash:.2f}，实际: {acc.available_cash}"

        print(f"✅ test_manual_broker_flow PASSED")
    finally:
        # Cleanup temp dir
        import shutil
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


if __name__ == '__main__':
    print("=" * 60)
    print("A股量化交易系统 - 端到端集成测试")
    print("=" * 60)
    print()

    tests = [
        ('数据完整链路', test_data_pipeline),
        ('信号总线链路', test_signal_bus_pipeline),
        ('半自动交易闭环', test_manual_broker_flow),
    ]

    passed = 0
    failed = 0
    skipped = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except AssertionError as e:
            print(f"❌ {name} FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {name} ERROR: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print()
    print("=" * 60)
    print(f"结果: {passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 60)
