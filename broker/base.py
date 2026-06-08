"""
券商/交易接口抽象基类
设计模式：Strategy Pattern（与 BaseStrategy 一致）
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Optional
from datetime import datetime


class OrderSide(Enum):
    BUY = 'BUY'
    SELL = 'SELL'


class OrderType(Enum):
    MARKET = 'MARKET'    # 市价单
    LIMIT = 'LIMIT'      # 限价单


class OrderStatus(Enum):
    PENDING = 'PENDING'       # 已提交，等待成交
    PARTIAL = 'PARTIAL'       # 部分成交
    FILLED = 'FILLED'         # 全部成交
    CANCELLED = 'CANCELLED'   # 已撤销
    REJECTED = 'REJECTED'     # 已拒绝
    EXPIRED = 'EXPIRED'       # 已过期


@dataclass
class OrderRequest:
    """下单请求"""
    ts_code: str           # 股票代码
    side: OrderSide        # 买卖方向
    quantity: int          # 数量（股）
    price: float = 0       # 价格（0=市价）
    order_type: OrderType = OrderType.MARKET
    reason: str = ''       # 下单原因


@dataclass
class OrderResult:
    """下单结果"""
    order_id: str
    ts_code: str
    side: OrderSide
    price: float
    quantity: int
    filled_quantity: int = 0
    filled_price: float = 0
    status: OrderStatus = OrderStatus.PENDING
    commission: float = 0
    stamp_tax: float = 0
    slippage: float = 0
    amount: float = 0
    create_time: str = ''
    update_time: str = ''
    reason: str = ''
    error_message: str = ''


@dataclass
class AccountInfo:
    """账户信息"""
    broker_name: str = ''
    account_id: str = ''
    total_assets: float = 0        # 总资产
    available_cash: float = 0      # 可用资金
    frozen_cash: float = 0         # 冻结资金
    market_value: float = 0        # 持仓市值
    total_profit: float = 0        # 总盈亏
    total_profit_rate: float = 0   # 总收益率
    daily_profit: float = 0        # 当日盈亏
    daily_profit_rate: float = 0   # 当日收益率
    position_count: int = 0        # 持仓数量
    update_time: str = ''


@dataclass
class PositionInfo:
    """持仓信息"""
    ts_code: str
    name: str = ''
    quantity: int = 0              # 持仓数量
    available_quantity: int = 0    # 可用数量（T+1冻结）
    cost_price: float = 0          # 成本价
    current_price: float = 0       # 现价
    market_value: float = 0        # 市值
    profit: float = 0              # 盈亏
    profit_rate: float = 0         # 盈亏率
    entry_date: str = ''           # 建仓日期
    industry: str = ''             # 行业


@dataclass
class Signal:
    """交易信号"""
    ts_code: str
    name: str = ''
    signal: str = 'BUY'            # BUY / SELL / HOLD
    weight: float = 0
    reason: str = ''
    price: float = 0
    strategy: str = ''             # 来源策略
    create_time: str = ''
    confirmed: bool = False        # 是否已确认（半自动模式）
    executed: bool = False         # 是否已执行
    order_id: str = ''             # 对应的订单ID


@dataclass
class DailyRiskLimit:
    """日风控限制"""
    max_daily_loss_rate: float = 0.02      # 单日最大亏损2%
    max_single_position_weight: float = 0.05  # 单只最大仓位5%
    max_total_positions: int = 20           # 最大持仓数
    max_single_order_amount: float = 200000  # 单笔最大20万
    blacklist: List[str] = field(default_factory=list)  # 黑名单股票
    require_confirm_large: bool = True       # 大额需确认
    large_order_threshold: float = 50000     # 大额阈值


class BaseBroker(ABC):
    """
    券商/交易接口抽象基类

    所有实盘/模拟盘券商连接器都继承此类，
    实现统一的交易接口，方便切换不同券商。
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.name = self.config.get('name', 'BaseBroker')
        self.connected = False

    @abstractmethod
    def connect(self) -> bool:
        """连接到券商交易系统"""
        pass

    @abstractmethod
    def disconnect(self) -> bool:
        """断开连接"""
        pass

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        pass

    @abstractmethod
    def get_positions(self) -> Dict[str, PositionInfo]:
        """获取持仓列表 {ts_code: PositionInfo}"""
        pass

    @abstractmethod
    def submit_order(self, request: OrderRequest) -> OrderResult:
        """提交订单"""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤销订单"""
        pass

    @abstractmethod
    def get_orders(self, status: OrderStatus = None, limit: int = 50) -> List[OrderResult]:
        """获取订单列表"""
        pass

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[OrderResult]:
        """查询单个订单"""
        pass

    def get_account_summary(self) -> dict:
        """获取账户摘要（通用格式，用于 Web 展示）"""
        account = self.get_account()
        if account is None:
            return {}
        return {
            'broker_name': account.broker_name,
            'total_assets': account.total_assets,
            'available_cash': account.available_cash,
            'market_value': account.market_value,
            'total_profit': account.total_profit,
            'total_profit_rate': account.total_profit_rate,
            'daily_profit': account.daily_profit,
            'position_count': account.position_count,
            'update_time': account.update_time,
        }

    def get_positions_list(self) -> List[dict]:
        """获取持仓列表（通用格式）"""
        positions = self.get_positions()
        result = []
        for ts_code, pos in positions.items():
            result.append({
                'ts_code': pos.ts_code,
                'name': pos.name,
                'quantity': pos.quantity,
                'cost_price': pos.cost_price,
                'current_price': pos.current_price,
                'market_value': pos.market_value,
                'profit': pos.profit,
                'profit_rate': pos.profit_rate,
                'entry_date': pos.entry_date,
                'industry': pos.industry,
            })
        return result

    def get_orders_list(self, status: OrderStatus = None, limit: int = 50) -> List[dict]:
        """获取订单列表（通用格式）"""
        orders = self.get_orders(status, limit)
        result = []
        for o in orders:
            result.append({
                'order_id': o.order_id,
                'ts_code': o.ts_code,
                'side': o.side.value,
                'price': o.price,
                'quantity': o.quantity,
                'filled_quantity': o.filled_quantity,
                'filled_price': o.filled_price,
                'status': o.status.value,
                'commission': o.commission,
                'amount': o.amount,
                'create_time': o.create_time,
                'reason': o.reason,
            })
        return result
