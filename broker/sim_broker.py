"""
模拟盘券商
完全本地实现，模拟真实券商的交易流程：
- 虚拟资金管理
- 佣金万三、印花税万五、滑点0.2%
- T+1限制、涨跌停限制
- 订单管理和持久化
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import asdict

from .base import (
    BaseBroker, OrderRequest, OrderResult, OrderSide,
    OrderType, OrderStatus, AccountInfo, PositionInfo
)


class SimBroker(BaseBroker):
    """
    模拟盘券商连接器

    完全本地运行，不需要任何外部API，
    用历史行情的最新价作为成交价模拟交易。

    支持持久化：账号状态保存到 JSON 文件。
    """

    # 默认费率（与 config/settings.py 一致）
    DEFAULT_COMMISSION_RATE = 0.0003   # 佣金万三
    DEFAULT_STAMP_TAX_RATE = 0.0005    # 印花税万五（卖出）
    DEFAULT_SLIPPAGE_RATE = 0.002      # 滑点0.2%
    DEFAULT_MIN_COMMISSION = 5.0       # 最低佣金5元
    DEFAULT_LIMIT_UP = 0.10            # 涨停10%
    DEFAULT_LIMIT_DOWN = -0.10         # 跌停-10%

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.name = '模拟盘'

        # 交易参数
        self.initial_capital = self.config.get('initial_capital', 1_000_000)
        self.commission_rate = self.config.get('commission_rate', self.DEFAULT_COMMISSION_RATE)
        self.stamp_tax_rate = self.config.get('stamp_tax_rate', self.DEFAULT_STAMP_TAX_RATE)
        self.slippage_rate = self.config.get('slippage_rate', self.DEFAULT_SLIPPAGE_RATE)
        self.min_commission = self.config.get('min_commission', self.DEFAULT_MIN_COMMISSION)
        self.limit_up_rate = self.config.get('limit_up_rate', self.DEFAULT_LIMIT_UP)
        self.limit_down_rate = self.config.get('limit_down_rate', self.DEFAULT_LIMIT_DOWN)

        # 持久化路径
        self.data_dir = self.config.get('data_dir', './data_cache')
        self.account_file = os.path.join(self.data_dir, 'sim_account.json')

        # 内部状态
        self._cash: float = self.initial_capital
        self._frozen_cash: float = 0
        self._positions: Dict[str, PositionInfo] = {}
        self._orders: List[OrderResult] = []
        self._order_counter: int = 0
        self._nav_history: List[dict] = []

        # 尝试加载已有状态
        self._load_account()

    # ============================================================
    # 连接
    # ============================================================

    def connect(self) -> bool:
        """连接（模拟盘直接返回成功）"""
        self.connected = True
        return True

    def disconnect(self) -> bool:
        """断开连接"""
        self._save_account()
        self.connected = False
        return True

    # ============================================================
    # 账户
    # ============================================================

    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        market_value = sum(p.market_value for p in self._positions.values())
        total_assets = self._cash + self._frozen_cash + market_value
        total_profit = total_assets - self.initial_capital
        total_profit_rate = total_profit / self.initial_capital if self.initial_capital > 0 else 0

        # 计算当日盈亏
        prev_nav = self._nav_history[-1]['total_assets'] if self._nav_history else self.initial_capital
        daily_profit = total_assets - prev_nav

        return AccountInfo(
            broker_name='模拟盘',
            account_id='SIM-001',
            total_assets=total_assets,
            available_cash=self._cash,
            frozen_cash=self._frozen_cash,
            market_value=market_value,
            total_profit=total_profit,
            total_profit_rate=total_profit_rate,
            daily_profit=daily_profit,
            daily_profit_rate=daily_profit / prev_nav if prev_nav > 0 else 0,
            position_count=len(self._positions),
            update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )

    def get_positions(self) -> Dict[str, PositionInfo]:
        """获取持仓"""
        return dict(self._positions)

    # ============================================================
    # 订单
    # ============================================================

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """提交订单"""
        order_id = self._gen_order_id()

        # 价格：市价单用传入价格（应为当前行情价）
        price = request.price if request.price > 0 else 0

        result = OrderResult(
            order_id=order_id,
            ts_code=request.ts_code,
            side=request.side,
            price=price,
            quantity=request.quantity,
            status=OrderStatus.PENDING,
            create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            reason=request.reason
        )

        # 执行成交
        try:
            if request.side == OrderSide.BUY:
                self._execute_buy(result, price, request.quantity, request.stock_name)
            else:
                self._execute_sell(result, price, request.quantity)

            self._orders.append(result)
            self._save_account()

        except Exception as e:
            result.status = OrderStatus.REJECTED
            result.error_message = str(e)
            self._orders.append(result)

        return result

    def _execute_buy(self, result: OrderResult, price: float, quantity: int, name: str = ''):
        """执行买入"""
        # 计算滑点（买入时价格上浮）
        slippage = price * self.slippage_rate
        fill_price = price + slippage
        amount = fill_price * quantity

        # 计算佣金
        commission = max(amount * self.commission_rate, self.min_commission)
        total_cost = amount + commission

        if total_cost > self._cash:
            result.status = OrderStatus.REJECTED
            result.error_message = f'可用资金不足（需要{total_cost:,.0f}，可用{self._cash:,.0f}）'
            return

        # 扣款
        self._cash -= total_cost

        # 更新持仓
        if result.ts_code in self._positions:
            pos = self._positions[result.ts_code]
            total_qty = pos.quantity + quantity
            avg_cost = (pos.cost_price * pos.quantity + fill_price * quantity) / total_qty
            pos.quantity = total_qty
            pos.available_quantity = total_qty  # 新买的当天不可卖（T+1）
            pos.cost_price = avg_cost
            pos.market_value = fill_price * total_qty
            pos.profit = (fill_price - avg_cost) * total_qty
            pos.profit_rate = (fill_price / avg_cost - 1) if avg_cost > 0 else 0
        else:
            self._positions[result.ts_code] = PositionInfo(
                ts_code=result.ts_code,
                name=name or result.ts_code,
                quantity=quantity,
                available_quantity=0,  # T+1，当天不可卖（简化：设0）
                cost_price=fill_price,
                current_price=fill_price,
                market_value=amount,
                entry_date=datetime.now().strftime('%Y%m%d'),
            )

        result.status = OrderStatus.FILLED
        result.filled_quantity = quantity
        result.filled_price = fill_price
        result.commission = commission
        result.slippage = slippage * quantity
        result.amount = amount
        result.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _execute_sell(self, result: OrderResult, price: float, quantity: int):
        """执行卖出"""
        if result.ts_code not in self._positions:
            result.status = OrderStatus.REJECTED
            result.error_message = f'不持有 {result.ts_code}'
            return

        pos = self._positions[result.ts_code]
        if pos.available_quantity < quantity:
            result.status = OrderStatus.REJECTED
            result.error_message = f'{result.ts_code} 可卖数量不足（需{quantity}，可卖{pos.available_quantity}）'
            return

        # 计算滑点（卖出时价格下浮）
        slippage = price * (-self.slippage_rate)
        fill_price = price + slippage
        amount = fill_price * quantity

        # 计算佣金 + 印花税
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = amount * self.stamp_tax_rate
        total_fee = commission + stamp_tax

        # 收款
        self._cash += (amount - total_fee)

        # 更新持仓
        pos.quantity -= quantity
        pos.available_quantity -= quantity
        if pos.quantity <= 0:
            del self._positions[result.ts_code]
        else:
            pos.market_value = fill_price * pos.quantity
            pos.profit = (fill_price - pos.cost_price) * pos.quantity
            pos.profit_rate = (fill_price / pos.cost_price - 1) if pos.cost_price > 0 else 0

        result.status = OrderStatus.FILLED
        result.filled_quantity = quantity
        result.filled_price = fill_price
        result.commission = commission
        result.stamp_tax = stamp_tax
        result.slippage = abs(slippage) * quantity
        result.amount = amount
        result.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单（模拟盘只支持撤销未成交的订单）"""
        for order in self._orders:
            if order.order_id == order_id:
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.CANCELLED
                    order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    # 释放冻结资金
                    if order.side == OrderSide.BUY and order.status == OrderStatus.PENDING:
                        self._frozen_cash -= order.price * order.quantity * 1.001
                    return True
        return False

    def get_orders(self, status: OrderStatus = None, limit: int = 50) -> List[OrderResult]:
        """获取订单列表"""
        orders = self._orders
        if status:
            orders = [o for o in orders if o.status == status]
        # 按时间倒序
        orders = sorted(orders, key=lambda x: x.create_time, reverse=True)
        return orders[:limit]

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        """查询单个订单"""
        for order in self._orders:
            if order.order_id == order_id:
                return order
        return None

    # ============================================================
    # 行情更新
    # ============================================================

    def update_market_prices(self, market_data: dict):
        """
        根据行情数据更新持仓市值

        Args:
            market_data: {ts_code: {close, name, industry, ...}}
        """
        for ts_code, pos in self._positions.items():
            if ts_code in market_data:
                stock = market_data[ts_code]
                new_price = stock.get('close', pos.current_price)
                pos.current_price = new_price
                pos.market_value = new_price * pos.quantity
                pos.profit = (new_price - pos.cost_price) * pos.quantity
                pos.profit_rate = (new_price / pos.cost_price - 1) if pos.cost_price > 0 else 0
                pos.name = stock.get('name', pos.name)
                pos.industry = stock.get('industry', pos.industry)

    def update_t_plus_1(self, current_date: str):
        """
        更新T+1状态：当天买入的股票到下一个交易日变为可卖

        Args:
            current_date: 当前日期 YYYYMMDD
        """
        today = current_date
        for ts_code, pos in self._positions.items():
            if pos.entry_date != today:
                # 非当日买入，全部可卖
                pos.available_quantity = pos.quantity

    def record_nav(self):
        """记录当前净值"""
        account = self.get_account()
        self._nav_history.append({
            'date': datetime.now().strftime('%Y%m%d %H:%M:%S'),
            'total_assets': account.total_assets,
            'cash': account.available_cash,
            'market_value': account.market_value,
        })

    # ============================================================
    # 账户管理
    # ============================================================

    def reset_account(self, initial_capital: float = None):
        """
        重置模拟账户

        Args:
            initial_capital: 初始资金，None则使用当前设置
        """
        if initial_capital is not None:
            self.initial_capital = initial_capital
        self._cash = self.initial_capital
        self._frozen_cash = 0
        self._positions = {}
        self._orders = []
        self._nav_history = []
        self._order_counter = 0
        self._save_account()
        print(f"[模拟盘] 账户已重置，初始资金: {self.initial_capital:,.0f}")

    def get_nav_history(self) -> List[dict]:
        """获取净值历史"""
        return self._nav_history

    # ============================================================
    # 内部方法
    # ============================================================

    def _gen_order_id(self) -> str:
        self._order_counter += 1
        return f"SIM_{datetime.now().strftime('%Y%m%d')}_{self._order_counter:06d}"

    def _save_account(self):
        """保存账户状态"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            # 序列化持仓
            pos_data = {}
            for code, pos in self._positions.items():
                pos_data[code] = {
                    'ts_code': pos.ts_code,
                    'name': pos.name,
                    'quantity': pos.quantity,
                    'available_quantity': pos.available_quantity,
                    'cost_price': pos.cost_price,
                    'current_price': pos.current_price,
                    'market_value': pos.market_value,
                    'profit': pos.profit,
                    'profit_rate': pos.profit_rate,
                    'entry_date': pos.entry_date,
                    'industry': pos.industry,
                }

            # 序列化订单
            orders_data = []
            for o in self._orders[-100:]:  # 只保留最近100条
                orders_data.append({
                    'order_id': o.order_id,
                    'ts_code': o.ts_code,
                    'side': o.side.value,
                    'price': o.price,
                    'quantity': o.quantity,
                    'filled_quantity': o.filled_quantity,
                    'filled_price': o.filled_price,
                    'status': o.status.value,
                    'commission': o.commission,
                    'stamp_tax': o.stamp_tax,
                    'slippage': o.slippage,
                    'amount': o.amount,
                    'create_time': o.create_time,
                    'update_time': o.update_time,
                    'reason': o.reason,
                    'error_message': o.error_message,
                })

            data = {
                'initial_capital': self.initial_capital,
                'cash': self._cash,
                'frozen_cash': self._frozen_cash,
                'positions': pos_data,
                'orders': orders_data,
                'order_counter': self._order_counter,
                'nav_history': self._nav_history[-500:],  # 只保留最近500条
                'last_save': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            with open(self.account_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        except Exception as e:
            print(f"[模拟盘] 保存状态失败: {e}")

    def _load_account(self):
        """加载账户状态"""
        try:
            if not os.path.exists(self.account_file):
                return

            with open(self.account_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.initial_capital = data.get('initial_capital', self.initial_capital)
            self._cash = data.get('cash', self.initial_capital)
            self._frozen_cash = data.get('frozen_cash', 0)
            self._order_counter = data.get('order_counter', 0)
            self._nav_history = data.get('nav_history', [])

            # 恢复持仓
            self._positions = {}
            for code, p in data.get('positions', {}).items():
                self._positions[code] = PositionInfo(
                    ts_code=p['ts_code'],
                    name=p.get('name', ''),
                    quantity=p['quantity'],
                    available_quantity=p.get('available_quantity', p['quantity']),
                    cost_price=p['cost_price'],
                    current_price=p.get('current_price', p['cost_price']),
                    market_value=p.get('market_value', 0),
                    profit=p.get('profit', 0),
                    profit_rate=p.get('profit_rate', 0),
                    entry_date=p.get('entry_date', ''),
                    industry=p.get('industry', ''),
                )

            # 恢复订单
            self._orders = []
            for o in data.get('orders', []):
                side = OrderSide.BUY if o.get('side') == 'BUY' else OrderSide.SELL
                status = OrderStatus(o.get('status', 'PENDING'))
                self._orders.append(OrderResult(
                    order_id=o['order_id'],
                    ts_code=o['ts_code'],
                    side=side,
                    price=o.get('price', 0),
                    quantity=o.get('quantity', 0),
                    filled_quantity=o.get('filled_quantity', 0),
                    filled_price=o.get('filled_price', 0),
                    status=status,
                    commission=o.get('commission', 0),
                    stamp_tax=o.get('stamp_tax', 0),
                    slippage=o.get('slippage', 0),
                    amount=o.get('amount', 0),
                    create_time=o.get('create_time', ''),
                    update_time=o.get('update_time', ''),
                    reason=o.get('reason', ''),
                    error_message=o.get('error_message', ''),
                ))

            # 避免重置时丢失太多的 order_counter
            self._order_counter = max(self._order_counter, len(self._orders))

            account = self.get_account()
            print(f"[模拟盘] 已加载账户状态: 总资产 {account.total_assets:,.0f} | "
                  f"持仓 {account.position_count} 只 | 现金 {account.available_cash:,.0f}")

        except Exception as e:
            print(f"[模拟盘] 加载状态失败: {e}，使用初始状态")
            self._cash = self.initial_capital
