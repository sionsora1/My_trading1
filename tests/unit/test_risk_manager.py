"""
tests/unit/test_risk_manager.py — Unit tests for RiskManager

Covers:
  TC-RM01: Position cap control
  TC-RM02: Single-stock position limit
  TC-RM03: Drawdown control
  TC-RM04: Risk rejection (blacklist / signal)
  TC-RM05: Risk pass-through (valid orders within limits)
"""

import os
import tempfile

import pytest

from broker.risk_manager import RiskManager, RiskCheckResult, RiskState
from broker.base import (
    DailyRiskLimit,
    OrderRequest,
    OrderSide,
    AccountInfo,
    PositionInfo,
    Signal,
)


class TestRiskManager:
    """Unit tests for risk_manager.RiskManager."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _make_manager(**kwargs) -> RiskManager:
        """Create a RiskManager with given DailyRiskLimit overrides.

        The state_file is pointed at a temp location so the real
        data_cache/risk_state.json is never touched by tests.
        """
        config = DailyRiskLimit(**kwargs)
        rm = RiskManager(config)
        rm.state_file = os.path.join(
            tempfile.gettempdir(), "test_risk_state.json"
        )
        return rm

    @staticmethod
    def _make_account(**kwargs) -> AccountInfo:
        """Create an AccountInfo with sensible trading defaults."""
        defaults: dict = {
            "total_assets": 100_000,
            "available_cash": 100_000,
        }
        defaults.update(kwargs)
        return AccountInfo(**defaults)

    @staticmethod
    def _make_position(**kwargs) -> PositionInfo:
        """Create a PositionInfo with sensible defaults."""
        defaults: dict = {
            "ts_code": "000001",
            "quantity": 1_000,
            "available_quantity": 1_000,
            "cost_price": 10,
            "current_price": 10,
            "market_value": 10_000,
        }
        defaults.update(kwargs)
        return PositionInfo(**defaults)

    # ==================================================================
    # TC-RM01: Position cap control
    # ==================================================================
    def test_position_cap_control(self):
        """Total number of positions must not exceed configured max.

        When the limit is reached a BUY order for a **new** stock is
        rejected, but adding to an already-held position is still
        permitted.
        """
        rm = self._make_manager(max_total_positions=3)
        account = self._make_account()

        positions: dict = {
            "000001": self._make_position(ts_code="000001"),
            "000002": self._make_position(ts_code="000002"),
            "000003": self._make_position(ts_code="000003"),
        }

        # --- New stock at limit → blocked ---
        order_new = OrderRequest(
            ts_code="000004",
            side=OrderSide.BUY,
            quantity=100,
            price=10,
        )
        result = rm.check_order(order_new, account, positions)
        assert result.passed is False, (
            f"Expected block, got passed. Reason: {result.reason}"
        )
        assert "持仓数已达上限" in result.reason
        assert result.severity == "BLOCK"

        # --- Add to existing holding at limit → allowed ---
        order_existing = OrderRequest(
            ts_code="000001",
            side=OrderSide.BUY,
            quantity=100,
            price=10,
            reason="add to existing position",
        )
        result_existing = rm.check_order(order_existing, account, positions)
        assert result_existing.passed is True, (
            f"Expected pass for existing stock, got: {result_existing.reason}"
        )

    # ==================================================================
    # TC-RM02: Single-stock position limit
    # ==================================================================
    def test_single_stock_position_limit(self):
        """No single stock may exceed the configured max allocation weight.

        A BUY order that pushes the holding weight past the cap is
        blocked; a smaller top-up that stays within the cap is allowed.
        """
        rm = self._make_manager(max_single_position_weight=0.10)
        account = self._make_account(total_assets=100_000)

        positions: dict = {
            "000001": self._make_position(
                ts_code="000001",
                market_value=8_000,  # 8 % of 100k
            ),
        }

        # --- Order would bring weight to 11 % (8k + 3k) → blocked ---
        order_big = OrderRequest(
            ts_code="000001",
            side=OrderSide.BUY,
            quantity=300,
            price=10,  # +3000 market value
        )
        result = rm.check_order(order_big, account, positions)
        assert result.passed is False, (
            f"Expected block for overweight, got: {result.reason}"
        )
        assert "单只仓位" in result.reason
        assert result.severity == "BLOCK"

        # --- Order stays within limit (8k + 1k = 9 %) → allowed ---
        order_small = OrderRequest(
            ts_code="000001",
            side=OrderSide.BUY,
            quantity=100,
            price=10,
        )
        result_small = rm.check_order(order_small, account, positions)
        assert result_small.passed is True, (
            f"Expected pass for in-limit top-up, got: {result_small.reason}"
        )

    # ==================================================================
    # TC-RM03: Drawdown control
    # ==================================================================
    def test_drawdown_control(self):
        """Maximum drawdown from peak equity triggers a trading halt.

        Once the drawdown exceeds the configured threshold all
        subsequent orders are blocked with severity BLOCK.
        """
        rm = self._make_manager()

        # Simulate a historical peak of 100k and a 15 % drawdown limit
        rm.state.peak_equity = 100_000
        rm.state.max_drawdown_rate = -0.15

        # Account has fallen 20 % from peak → exceeds 15 % limit
        account = self._make_account(
            total_assets=80_000,
            available_cash=1_000_000,  # plenty so capital check passes
        )

        order = OrderRequest(
            ts_code="000001",
            side=OrderSide.BUY,
            quantity=100,
            price=10,
        )

        # --- First order: drawdown check triggers halt ---
        result = rm.check_order(order, account, {})
        assert result.passed is False, (
            f"Expected drawdown block, got: {result.reason}"
        )
        assert "最大回撤" in result.reason
        assert result.severity == "BLOCK"
        assert rm.state.trading_halted is True, (
            "trading_halted should be set after drawdown trigger"
        )

        # --- Follow-up order: halted at step 1 ---
        result2 = rm.check_order(order, account, {})
        assert result2.passed is False
        assert not result2.passed  # trading is halted, anything should be blocked

    # ==================================================================
    # TC-RM04: Risk rejection  (blacklisted stock via check_signal)
    # ==================================================================
    def test_risk_rejection(self):
        """Signals for blacklisted stocks are rejected by check_signal."""
        rm = self._make_manager(blacklist=["000666"])

        signal = Signal(
            ts_code="000666",
            name="BadCo",
            signal="BUY",
            reason="should be blocked",
        )

        account = self._make_account()
        result = rm.check_signal(signal, account, {})

        assert result.passed is False, (
            f"Expected rejection for blacklisted stock, got: {result.reason}"
        )
        assert "黑名单" in result.reason
        assert result.severity == "BLOCK"

    # ==================================================================
    # TC-RM05: Risk pass-through
    # ==================================================================
    def test_risk_pass_through(self):
        """Valid orders and signals that stay within every risk limit
        should pass all checks cleanly."""
        rm = self._make_manager(
            max_total_positions=5,
            max_single_position_weight=0.15,
            max_single_order_amount=50_000,
            max_daily_loss_rate=0.05,
        )
        # Activate daily-loss tracking without triggering the limit
        rm.state.starting_equity = 100_000
        rm.state.daily_loss_rate = -0.005  # 0.5 % loss (well within 5 %)

        account = self._make_account(
            total_assets=99_500,
            available_cash=50_000,
        )

        positions: dict = {
            "000001": self._make_position(
                ts_code="000001",
                market_value=5_000,  # 5 % — well under 15 %
            ),
        }

        # --- Valid BUY order ---
        order_buy = OrderRequest(
            ts_code="000002",
            side=OrderSide.BUY,
            quantity=100,
            price=20,  # 2000 cost
        )
        result_buy = rm.check_order(order_buy, account, positions)
        assert result_buy.passed is True, (
            f"BUY should pass, got: {result_buy.reason}"
        )
        assert "通过" in result_buy.reason

        # --- Valid SELL order (existing holding) ---
        order_sell = OrderRequest(
            ts_code="000001",
            side=OrderSide.SELL,
            quantity=100,
            price=10,
        )
        result_sell = rm.check_order(order_sell, account, positions)
        assert result_sell.passed is True, (
            f"SELL should pass, got: {result_sell.reason}"
        )

        # --- Valid Signal ---
        signal = Signal(
            ts_code="000002",
            signal="BUY",
            reason="valid signal",
        )
        result_signal = rm.check_signal(signal, account, positions)
        assert result_signal.passed is True, (
            f"Signal should pass, got: {result_signal.reason}"
        )
