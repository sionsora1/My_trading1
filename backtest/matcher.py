"""
交易撮合引擎
模拟真实交易：佣金、印花税、滑点、涨跌停、T+1
"""

from dataclasses import dataclass
from enum import Enum


class OrderSide(Enum):
    BUY = '买入'
    SELL = '卖出'

class OrderStatus(Enum):
    PENDING = '待成交'
    FILLED = '已成交'
    CANCELLED = '已取消'
    REJECTED = '已拒绝'

@dataclass
class Order:
    """订单"""
    order_id: str
    ts_code: str
    side: OrderSide
    price: float
    quantity: int
    order_date: str
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float = 0
    fill_quantity: int = 0
    fill_date: str = ''
    commission: float = 0
    slippage: float = 0
    reject_reason: str = ''


class MatchEngine:
    """交易撮合引擎"""

    def __init__(self, config: dict):
        self.commission_rate = config.get('commission_rate', 0.0003)
        self.stamp_tax_rate = config.get('stamp_tax_rate', 0.0005)
        self.slippage_rate = config.get('slippage_rate', 0.002)
        self.min_commission = config.get('min_commission', 5.0)
        self.limit_up_rate = config.get('limit_up_rate', 0.10)
        self.limit_down_rate = config.get('limit_down_rate', -0.10)

    def calculate_commission(self, amount: float, side: OrderSide) -> float:
        """计算佣金"""
        commission = amount * self.commission_rate
        commission = max(commission, self.min_commission)
        if side == OrderSide.SELL:
            commission += amount * self.stamp_tax_rate
        return round(commission, 2)

    def calculate_slippage(self, price: float, side: OrderSide) -> float:
        """计算滑点"""
        slippage = price * self.slippage_rate
        return slippage if side == OrderSide.BUY else -slippage

    def check_limit(self, open_price: float, prev_close: float) -> tuple:
        """检查涨跌停"""
        limit_up = round(prev_close * (1 + self.limit_up_rate), 2)
        limit_down = round(prev_close * (1 + self.limit_down_rate), 2)

        if open_price >= limit_up:
            return False, limit_up, limit_down
        if open_price <= limit_down:
            return False, limit_up, limit_down

        return True, limit_up, limit_down

    def match_order(self, order: Order, stock_data: dict, prev_close: float) -> Order:
        """撮合订单"""
        open_price = stock_data.get('open', stock_data.get('close', 0))

        can_trade, _, _ = self.check_limit(open_price, prev_close)

        if not can_trade:
            if order.side == OrderSide.BUY:
                order.status = OrderStatus.REJECTED
                order.reject_reason = '涨停无法买入'
                return order
            else:
                order.status = OrderStatus.REJECTED
                order.reject_reason = '跌停无法卖出'
                return order

        base_price = open_price
        slippage = self.calculate_slippage(base_price, order.side)
        fill_price = base_price + slippage

        amount = fill_price * order.quantity
        commission = self.calculate_commission(amount, order.side)

        order.status = OrderStatus.FILLED
        order.fill_price = round(fill_price, 2)
        order.fill_quantity = order.quantity
        order.fill_date = stock_data.get('trade_date', order.order_date)
        order.commission = commission
        order.slippage = abs(slippage) * order.quantity

        return order