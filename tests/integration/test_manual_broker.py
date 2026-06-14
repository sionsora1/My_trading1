"""
Integration tests for ManualBroker

Tests the semi-automated broker flow: signal creation, confirmation,
rejection, buy-sell cycles, and state persistence.

Test cases:
    TC-MB01: Initial account state after connect()
    TC-MB02: Submit BUY → confirm → position exists, cash deducted
    TC-MB03: Submit order → reject → no position created
    TC-MB04: Buy → confirm → Sell → confirm → position gone
    TC-MB05: State persistence across restart (new broker instance)
"""

import os
import sys
import tempfile
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from broker.manual_broker import ManualBroker
from broker.base import (
    OrderRequest, OrderSide, OrderType, OrderStatus,
    AccountInfo, PositionInfo,
)


# ────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────

@pytest.fixture
def broker():
    """Create a ManualBroker with a temp data_dir, connect, yield, then clean up.

    Each test gets a fresh broker with isolated state — no cross-test leakage.
    """
    tmpdir = tempfile.mkdtemp(prefix='test_manual_broker_')
    mb = ManualBroker({
        'initial_capital': 100_000,
        'data_dir': tmpdir,
    })
    mb.connect()
    yield mb
    mb.disconnect()
    import shutil
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────
# Test class
# ────────────────────────────────────────────────────────────────

class TestManualBrokerFlow:
    """Integration tests covering the full ManualBroker signal-to-trade lifecycle."""

    # ── TC-MB01 ─────────────────────────────────────────────────

    def test_initial_account_state(self, broker):
        """After connect(), get_account() returns correct initial values.

        Expected: total_assets=100000, available_cash=100000,
                  market_value=0, position_count=0.
        """
        account = broker.get_account()

        assert account.total_assets == 100_000, \
            f"total_assets: expected 100000, got {account.total_assets}"
        assert account.available_cash == 100_000, \
            f"available_cash: expected 100000, got {account.available_cash}"
        assert account.market_value == 0, \
            f"market_value: expected 0, got {account.market_value}"
        assert account.position_count == 0, \
            f"position_count: expected 0, got {account.position_count}"

    # ── TC-MB02 ─────────────────────────────────────────────────

    def test_submit_confirm_position_update(self, broker):
        """Submit a BUY order, confirm it, verify position created and cash deducted.

        1. Submit BUY for 50 shares of 600519.SH at 1800.00
        2. Confirm order with same fill price/qty
        3. Verify position exists with quantity=50
        4. Verify cash deducted by amount + commission (no stamp tax on buy)
        5. Verify order fields (commission, stamp_tax, amount)
        """
        req = OrderRequest(
            ts_code='600519.SH',
            side=OrderSide.BUY,
            quantity=50,
            price=1800.00,
            order_type=OrderType.LIMIT,
            reason='白酒龙头|贵州茅台',
            stock_name='贵州茅台',
        )
        result = broker.submit_order(req)
        assert result.status == OrderStatus.PENDING
        assert result.order_id.startswith('MANUAL_')

        # Confirm
        confirmed = broker.confirm_order(result.order_id, fill_price=1800.00, fill_qty=50)
        assert confirmed is not None, "confirm_order should return OrderResult"
        assert confirmed.status == OrderStatus.FILLED
        assert confirmed.filled_quantity == 50
        assert confirmed.filled_price == 1800.00

        # Position
        positions = broker.get_positions()
        assert '600519.SH' in positions, \
            f"Position 600519.SH should exist, got: {list(positions.keys())}"
        pos = positions['600519.SH']
        assert pos.quantity == 50, f"Position quantity: expected 50, got {pos.quantity}"

        # Cash: 100000 - (1800*50) - commission
        trade_amount = 1800.00 * 50  # 90,000
        expected_commission = max(trade_amount * 0.0003, 5.0)  # 27
        expected_total_cost = trade_amount + expected_commission  # 90,027
        expected_cash = 100_000 - expected_total_cost  # 9,973

        account = broker.get_account()
        assert abs(account.available_cash - expected_cash) < 0.01, \
            f"Cash: expected {expected_cash}, got {account.available_cash}"

        # Order-level fields
        assert confirmed.commission == expected_commission, \
            f"Commission: expected {expected_commission}, got {confirmed.commission}"
        assert confirmed.stamp_tax == 0.0, \
            f"Buy stamp_tax: expected 0.0, got {confirmed.stamp_tax}"
        assert confirmed.amount == trade_amount, \
            f"Amount: expected {trade_amount}, got {confirmed.amount}"

    # ── TC-MB03 ─────────────────────────────────────────────────

    def test_reject_signal_no_position(self, broker):
        """Submit an order, reject it, verify no position and cash unchanged.

        1. Submit BUY for 000858.SZ
        2. Reject the order with a reason
        3. Verify status is REJECTED with the reason
        4. Verify no position exists for the symbol
        5. Verify cash is still 100,000
        """
        req = OrderRequest(
            ts_code='000858.SZ',
            side=OrderSide.BUY,
            quantity=200,
            price=150.00,
            order_type=OrderType.LIMIT,
            reason='测试拒绝',
            stock_name='五粮液',
        )
        result = broker.submit_order(req)
        assert result.status == OrderStatus.PENDING

        rejected = broker.reject_order(result.order_id, reason='风险过高，不跟单')
        assert rejected is not None, "reject_order should return OrderResult"
        assert rejected.status == OrderStatus.REJECTED
        assert '风险过高' in rejected.error_message, \
            f"Rejection reason not found in: {rejected.error_message}"

        # No position
        positions = broker.get_positions()
        assert '000858.SZ' not in positions, \
            "Rejected order must not create a position"

        # Cash unchanged
        account = broker.get_account()
        assert account.available_cash == 100_000, \
            f"Cash should be 100000 after rejection, got {account.available_cash}"

    # ── TC-MB04 ─────────────────────────────────────────────────

    def test_buy_sell_complete_cycle(self, broker):
        """Buy all shares → confirm → Sell all shares → confirm → position gone.

        1. Buy 50 shares of 600519.SH at 1800, confirm
        2. Verify position exists with 50 shares
        3. Sell all 50 shares at 1850, confirm
        4. Verify position is removed (quantity 0)
        5. Verify sell order has stamp tax (sell-only in A-shares)
        """
        # --- BUY ---
        buy_req = OrderRequest(
            ts_code='600519.SH',
            side=OrderSide.BUY,
            quantity=50,
            price=1800.00,
            order_type=OrderType.LIMIT,
            reason='白酒龙头|贵州茅台',
            stock_name='贵州茅台',
        )
        buy_result = broker.submit_order(buy_req)
        broker.confirm_order(buy_result.order_id, fill_price=1800.00, fill_qty=50)

        positions = broker.get_positions()
        assert '600519.SH' in positions
        assert positions['600519.SH'].quantity == 50, "Should have 50 shares after buy"

        # --- SELL ---
        sell_req = OrderRequest(
            ts_code='600519.SH',
            side=OrderSide.SELL,
            quantity=50,
            price=1850.00,
            order_type=OrderType.LIMIT,
            reason='止盈卖出',
            stock_name='贵州茅台',
        )
        sell_result = broker.submit_order(sell_req)
        confirmed_sell = broker.confirm_order(
            sell_result.order_id, fill_price=1850.00, fill_qty=50,
        )

        assert confirmed_sell is not None
        assert confirmed_sell.status == OrderStatus.FILLED
        assert confirmed_sell.filled_quantity == 50
        # Sell must have stamp tax (A-share rule: 0.05% on sell only)
        assert confirmed_sell.stamp_tax > 0, \
            f"Sell stamp_tax should be > 0, got {confirmed_sell.stamp_tax}"

        # Position gone (quantity dropped to 0 → key removed from dict)
        positions = broker.get_positions()
        assert '600519.SH' not in positions, \
            f"Position should be removed after selling all shares, " \
            f"got: {list(positions.keys())}"

        # Account position_count should be 0
        account = broker.get_account()
        assert account.position_count == 0, \
            f"position_count: expected 0, got {account.position_count}"

    # ── TC-MB05 ─────────────────────────────────────────────────

    def test_state_persistence_across_restart(self):
        """Create broker → trade → new broker instance → verify positions restored.

        ManualBroker persists state to JSON on every mutation.  A fresh
        instance pointed at the same data_dir must pick up the saved
        positions and cash balance.
        """
        import shutil
        tmpdir = tempfile.mkdtemp(prefix='test_persist_')

        try:
            config = {
                'initial_capital': 100_000,
                'data_dir': tmpdir,
            }

            # --- First broker: make a trade ---
            broker1 = ManualBroker(config)
            broker1.connect()

            req = OrderRequest(
                ts_code='600519.SH',
                side=OrderSide.BUY,
                quantity=50,
                price=1800.00,
                order_type=OrderType.LIMIT,
                reason='持久化测试|贵州茅台',
                stock_name='贵州茅台',
            )
            result = broker1.submit_order(req)
            broker1.confirm_order(result.order_id, fill_price=1800.00, fill_qty=50)

            pos1 = broker1.get_positions()
            assert '600519.SH' in pos1
            assert pos1['600519.SH'].quantity == 50, \
                f"Broker1 position quantity: expected 50, got {pos1['600519.SH'].quantity}"
            cash1 = broker1._cash
            broker1.disconnect()

            # --- Second broker: should auto-load saved state ---
            broker2 = ManualBroker(config)
            broker2.connect()

            pos2 = broker2.get_positions()
            assert '600519.SH' in pos2, \
                f"Restored positions missing 600519.SH, got: {list(pos2.keys())}"
            assert pos2['600519.SH'].quantity == 50, \
                f"Restored quantity: expected 50, got {pos2['600519.SH'].quantity}"

            assert broker2._cash == cash1, \
                f"Restored cash: expected {cash1}, got {broker2._cash}"

            broker2.disconnect()

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
