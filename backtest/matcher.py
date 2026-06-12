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

    def check_limit(self, open_price: float, prev_close: float) -> dict:
        """检查涨跌停，返回各方向是否可交易"""
        limit_up = round(prev_close * (1 + self.limit_up_rate), 2)
        limit_down = round(prev_close * (1 + self.limit_down_rate), 2)

        can_buy = open_price < limit_up     # 涨停时不能买入
        can_sell = open_price > limit_down  # 跌停时不能卖出

        return {
            'can_buy': can_buy,
            'can_sell': can_sell,
            'limit_up': limit_up,
            'limit_down': limit_down,
            'at_limit_up': open_price >= limit_up,
            'at_limit_down': open_price <= limit_down,
        }

    def match_order(self, order: Order, stock_data: dict, prev_close: float) -> Order:
        """撮合订单（成交价约束在当日最高/最低价范围内）"""
        open_price = stock_data.get('open', stock_data.get('close', 0))
        day_high = stock_data.get('high', open_price)
        day_low = stock_data.get('low', open_price)

        limit_up = round(prev_close * (1 + self.limit_up_rate), 2)
        limit_down = round(prev_close * (1 + self.limit_down_rate), 2)

        # 涨停时不能买入（但可以卖出），跌停时不能卖出（但可以买入）
        if order.side == OrderSide.BUY and open_price >= limit_up:
            order.status = OrderStatus.REJECTED
            order.reject_reason = '涨停无法买入'
            return order
        if order.side == OrderSide.SELL and open_price <= limit_down:
            order.status = OrderStatus.REJECTED
            order.reject_reason = '跌停无法卖出'
            return order

        # ============================================================
        # 成交价 = 开盘价 + 滑点，约束在当日 [最低价, 最高价] 范围内
        # 不能低于当天最低价，也不能高于当天最高价
        # ============================================================
        base_price = open_price
        slippage = self.calculate_slippage(base_price, order.side)
        fill_price = base_price + slippage

        # 约束在当日价格区间内
        if day_high > 0 and day_low > 0:
            fill_price = max(day_low, min(day_high, fill_price))

        amount = fill_price * order.quantity
        commission = self.calculate_commission(amount, order.side)

        order.status = OrderStatus.FILLED
        order.fill_price = round(fill_price, 2)
        order.fill_quantity = order.quantity
        order.fill_date = stock_data.get('trade_date', order.order_date)
        order.commission = commission
        order.slippage = abs(slippage) * order.quantity

        return order