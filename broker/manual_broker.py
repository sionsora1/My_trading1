"""
手动券商（东方财富半自动模式）
系统生成交易信号，用户在东方财富App手动下单，
然后回到系统录入成交信息。

设计理念：
- submit_order() 只创建 PENDING 信号，不执行成交
- confirm_order() 由用户录入实际成交价和数量后执行
- reject_order() 标记信号为拒绝
- 支持手动同步持仓和现金
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional

from .base import (
    BaseBroker, OrderRequest, OrderResult, OrderSide,
    OrderType, OrderStatus, AccountInfo, PositionInfo,
)


class ManualBroker(BaseBroker):
    """
    半自动券商连接器（东方财富）

    工作流程：
    1. 策略生成信号 → submit_order() 创建 PENDING 信号
    2. 用户在东方财富App手动下单
    3. 用户回到系统 → confirm_order() 录入成交
    4. 系统更新持仓和现金
    """

    # 费率常量
    COMMISSION_RATE = 0.0003     # 佣金 0.03%（万三）
    STAMP_TAX_RATE = 0.0005      # 印花税 0.05%（万五）
    MIN_COMMISSION = 5.0         # 最低佣金 5 元

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.name = '东方财富(手动)'

        # 资金配置
        self.initial_capital = self.config.get('initial_capital', 100_000)

        # 持久化路径
        self.data_dir = self.config.get('data_dir', './data_cache')
        self.state_file = os.path.join(self.data_dir, 'manual_account.json')

        # 内部状态
        self._cash: float = self.initial_capital
        self._positions: Dict[str, PositionInfo] = {}
        self._orders: List[OrderResult] = []
        self._order_counter: int = 0

        # 尝试加载已有状态
        self._load_state()

    # ============================================================
    # 连接
    # ============================================================

    def connect(self) -> bool:
        """连接到券商（手动模式仅初始化本地状态）"""
        self.connected = True
        account = self.get_account()
        print(f"[手动券商-东方财富] 初始化完成 | "
              f"初始资金: {self.initial_capital:,.0f} | "
              f"当前现金: {self._cash:,.0f} | "
              f"持仓: {account.position_count} 只 | "
              f"总资产: {account.total_assets:,.0f}")
        return True

    def disconnect(self) -> bool:
        """断开连接，保存状态"""
        self._save_state()
        self.connected = False
        print(f"[手动券商-东方财富] 已保存状态并断开连接")
        return True

    # ============================================================
    # 账户
    # ============================================================

    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        market_value = sum(p.market_value for p in self._positions.values())
        total_assets = self._cash + market_value
        total_profit = total_assets - self.initial_capital
        total_profit_rate = total_profit / self.initial_capital if self.initial_capital > 0 else 0

        return AccountInfo(
            broker_name='东方财富(手动)',
            account_id='MANUAL-001',
            total_assets=total_assets,
            available_cash=self._cash,
            frozen_cash=0,
            market_value=market_value,
            total_profit=total_profit,
            total_profit_rate=total_profit_rate,
            daily_profit=0,
            daily_profit_rate=0,
            position_count=len(self._positions),
            update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )

    def get_positions(self) -> Dict[str, PositionInfo]:
        """获取持仓列表"""
        return dict(self._positions)

    # ============================================================
    # 订单
    # ============================================================

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """
        提交订单（仅创建信号，不执行成交）

        系统生成信号后，用户需要在东方财富App手动下单，
        然后在系统中调用 confirm_order() 录入成交。
        """
        order_id = self._gen_order_id()

        result = OrderResult(
            order_id=order_id,
            ts_code=request.ts_code,
            side=request.side,
            price=request.price,
            quantity=request.quantity,
            status=OrderStatus.PENDING,
            create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            reason=request.reason,
        )

        self._orders.append(result)
        self._save_state()

        print(f"[手动券商-东方财富] 新信号已创建: {order_id} | "
              f"{request.side.value} {request.stock_name or request.ts_code} "
              f"{request.quantity}股 @ {request.price:.2f}")

        return result

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单（标记为已撤销）"""
        for order in self._orders:
            if order.order_id == order_id:
                if order.status == OrderStatus.PENDING:
                    order.status = OrderStatus.CANCELLED
                    order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self._save_state()
                    print(f"[手动券商-东方财富] 信号已撤销: {order_id}")
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
    # 半自动特有方法
    # ============================================================

    def confirm_order(self, order_id: str, fill_price: float,
                      fill_qty: int) -> Optional[OrderResult]:
        """
        确认成交（核心方法）

        用户在东方财富App手动下单后，回到系统录入成交信息。
        系统根据成交价和数量更新持仓和现金。

        Args:
            order_id: 信号订单ID
            fill_price: 实际成交价
            fill_qty: 实际成交数量

        Returns:
            更新后的 OrderResult，失败则返回 None
        """
        order = self.get_order(order_id)
        if order is None:
            print(f"[手动券商-东方财富] 订单不存在: {order_id}")
            return None

        if order.status != OrderStatus.PENDING:
            print(f"[手动券商-东方财富] 订单状态不是 PENDING: {order_id} -> {order.status}")
            return None

        # 成交数量不超过委托数量
        fill_qty = min(fill_qty, order.quantity)

        if order.side == OrderSide.BUY:
            self._execute_buy_confirm(order, fill_price, fill_qty)
        else:
            self._execute_sell_confirm(order, fill_price, fill_qty)

        self._save_state()

        print(f"[手动券商-东方财富] 成交确认: {order_id} | "
              f"{order.side.value} {order.ts_code} "
              f"成交价 {fill_price:.2f} x {fill_qty}股")

        return order

    def _execute_buy_confirm(self, order: OrderResult, fill_price: float,
                             fill_qty: int):
        """执行买入确认"""
        amount = fill_price * fill_qty
        commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
        stamp_tax = 0.0  # 印花税仅在卖出时收取
        total_cost = amount + commission + stamp_tax

        if total_cost > self._cash:
            order.status = OrderStatus.REJECTED
            order.error_message = f'可用资金不足（需要{total_cost:,.0f}，可用{self._cash:,.0f}）'
            order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[手动券商-东方财富] 买入失败: {order.error_message}")
            return

        # 扣款
        self._cash -= total_cost

        # 更新持仓
        if order.ts_code in self._positions:
            pos = self._positions[order.ts_code]
            total_qty = pos.quantity + fill_qty
            avg_cost = (pos.cost_price * pos.quantity + fill_price * fill_qty) / total_qty
            pos.quantity = total_qty
            pos.available_quantity = pos.quantity
            pos.cost_price = avg_cost
            pos.current_price = fill_price
            pos.market_value = fill_price * total_qty
            pos.profit = (fill_price - avg_cost) * total_qty
            pos.profit_rate = (fill_price / avg_cost - 1) if avg_cost > 0 else 0
        else:
            self._positions[order.ts_code] = PositionInfo(
                ts_code=order.ts_code,
                name=order.reason.split('|')[-1].strip() if '|' in order.reason else order.ts_code,
                quantity=fill_qty,
                available_quantity=fill_qty,
                cost_price=fill_price,
                current_price=fill_price,
                market_value=amount,
                profit=0,
                profit_rate=0,
                entry_date=datetime.now().strftime('%Y%m%d'),
            )

        order.status = OrderStatus.FILLED
        order.filled_quantity = fill_qty
        order.filled_price = fill_price
        order.commission = commission
        order.stamp_tax = stamp_tax
        order.amount = amount
        order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def _execute_sell_confirm(self, order: OrderResult, fill_price: float,
                              fill_qty: int):
        """执行卖出确认"""
        if order.ts_code not in self._positions:
            order.status = OrderStatus.REJECTED
            order.error_message = f'不持有 {order.ts_code}，无法卖出'
            order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[手动券商-东方财富] 卖出失败: {order.error_message}")
            return

        pos = self._positions[order.ts_code]
        if pos.quantity < fill_qty:
            order.status = OrderStatus.REJECTED
            order.error_message = f'{order.ts_code} 持仓不足（需{fill_qty}股，持有{pos.quantity}股）'
            order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[手动券商-东方财富] 卖出失败: {order.error_message}")
            return

        amount = fill_price * fill_qty
        commission = max(amount * self.COMMISSION_RATE, self.MIN_COMMISSION)
        stamp_tax = amount * self.STAMP_TAX_RATE
        total_fee = commission + stamp_tax

        # 收款
        self._cash += (amount - total_fee)

        # 更新持仓
        pos.quantity -= fill_qty
        pos.available_quantity = pos.quantity
        if pos.quantity <= 0:
            del self._positions[order.ts_code]
        else:
            pos.current_price = fill_price
            pos.market_value = fill_price * pos.quantity
            pos.profit = (fill_price - pos.cost_price) * pos.quantity
            pos.profit_rate = (fill_price / pos.cost_price - 1) if pos.cost_price > 0 else 0

        order.status = OrderStatus.FILLED
        order.filled_quantity = fill_qty
        order.filled_price = fill_price
        order.commission = commission
        order.stamp_tax = stamp_tax
        order.amount = amount
        order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def reject_order(self, order_id: str, reason: str = '') -> Optional[OrderResult]:
        """
        拒绝信号

        用户在东方财富App决定不跟单时，标记信号为拒绝。

        Args:
            order_id: 信号订单ID
            reason: 拒绝原因

        Returns:
            更新后的 OrderResult，失败则返回 None
        """
        order = self.get_order(order_id)
        if order is None:
            print(f"[手动券商-东方财富] 订单不存在: {order_id}")
            return None

        if order.status != OrderStatus.PENDING:
            print(f"[手动券商-东方财富] 订单状态不是 PENDING: {order_id}")
            return None

        order.status = OrderStatus.REJECTED
        order.error_message = reason or '用户拒绝'
        order.update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._save_state()

        print(f"[手动券商-东方财富] 信号已拒绝: {order_id} | 原因: {reason or '未提供'}")
        return order

    def get_pending_signals(self) -> List[OrderResult]:
        """
        获取待处理的信号列表

        Returns:
            所有状态为 PENDING 的订单（未被成交、拒绝或撤销）
        """
        return [o for o in self._orders if o.status == OrderStatus.PENDING]

    # ============================================================
    # 手动同步
    # ============================================================

    def sync_positions(self, positions_dict: dict):
        """
        从用户输入同步持仓（完全替换）

        用户在东方财富App查看实际持仓后，可批量录入替换系统持仓。

        Args:
            positions_dict: {
                ts_code: {
                    name, quantity, cost_price, current_price, entry_date, industry
                }
            }
        """
        self._positions = {}
        for ts_code, info in positions_dict.items():
            qty = info.get('quantity', 0)
            cost = info.get('cost_price', 0)
            cur_price = info.get('current_price', cost)
            mv = cur_price * qty
            profit = (cur_price - cost) * qty if qty > 0 else 0
            profit_rate = (cur_price / cost - 1) if cost > 0 else 0

            self._positions[ts_code] = PositionInfo(
                ts_code=ts_code,
                name=info.get('name', ''),
                quantity=qty,
                available_quantity=qty,
                cost_price=cost,
                current_price=cur_price,
                market_value=mv,
                profit=profit,
                profit_rate=profit_rate,
                entry_date=info.get('entry_date', ''),
                industry=info.get('industry', ''),
            )

        self._save_state()
        print(f"[手动券商-东方财富] 持仓已同步: {len(self._positions)} 只股票")

    def update_cash(self, cash_amount: float):
        """
        手动设置现金余额

        用户从东方财富App查看实际可用资金后更新。

        Args:
            cash_amount: 当前可用资金
        """
        old_cash = self._cash
        self._cash = cash_amount
        self._save_state()
        print(f"[手动券商-东方财富] 现金已更新: {old_cash:,.0f} -> {cash_amount:,.0f}")

    def update_prices(self, market_data: dict):
        """
        根据行情数据更新持仓市值

        Args:
            market_data: {ts_code: {close, name, industry, ...}}
        """
        updated_count = 0
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
                updated_count += 1

        if updated_count > 0:
            print(f"[手动券商-东方财富] 行情已更新: {updated_count} 只股票")

    # ============================================================
    # 持久化
    # ============================================================

    def _save_state(self):
        """保存账户状态到 JSON 文件"""
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

            # 序列化订单（保留最近 200 条）
            orders_data = []
            for o in self._orders[-200:]:
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
                    'amount': o.amount,
                    'create_time': o.create_time,
                    'update_time': o.update_time,
                    'reason': o.reason,
                    'error_message': o.error_message,
                })

            data = {
                'initial_capital': self.initial_capital,
                'cash': self._cash,
                'positions': pos_data,
                'orders': orders_data,
                'order_counter': self._order_counter,
                'last_save': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

        except Exception as e:
            print(f"[手动券商-东方财富] 保存状态失败: {e}")

    def _load_state(self):
        """从 JSON 文件加载账户状态"""
        try:
            if not os.path.exists(self.state_file):
                return

            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.initial_capital = data.get('initial_capital', self.initial_capital)
            self._cash = data.get('cash', self.initial_capital)
            self._order_counter = data.get('order_counter', 0)

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
                    amount=o.get('amount', 0),
                    create_time=o.get('create_time', ''),
                    update_time=o.get('update_time', ''),
                    reason=o.get('reason', ''),
                    error_message=o.get('error_message', ''),
                ))

            # 避免重置时丢失 order_counter
            self._order_counter = max(self._order_counter, len(self._orders))

            account = self.get_account()
            print(f"[手动券商-东方财富] 已加载账户状态: 总资产 {account.total_assets:,.0f} | "
                  f"持仓 {account.position_count} 只 | 现金 {account.available_cash:,.0f}")

        except Exception as e:
            print(f"[手动券商-东方财富] 加载状态失败: {e}，使用初始状态")
            self._cash = self.initial_capital

    # ============================================================
    # 内部方法
    # ============================================================

    def _gen_order_id(self) -> str:
        """生成订单ID"""
        self._order_counter += 1
        return f"MANUAL_{datetime.now().strftime('%Y%m%d')}_{self._order_counter:06d}"


# ============================================================
# 验证代码
# ============================================================

if __name__ == '__main__':
    import tempfile

    print("=" * 60)
    print("ManualBroker 验证测试")
    print("=" * 60)

    # 使用临时目录避免污染 data_cache
    with tempfile.TemporaryDirectory() as tmpdir:
        config = {
            'initial_capital': 100_000,
            'data_dir': tmpdir,
        }

        # 1. 创建实例
        print("\n[1] 创建 ManualBroker...")
        broker = ManualBroker(config)

        # 2. 连接
        print("\n[2] connect()...")
        broker.connect()

        # 3. 提交买入信号
        print("\n[3] submit_order(BUY)...")
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
        assert buy_result.status == OrderStatus.PENDING
        assert buy_result.order_id.startswith('MANUAL_')
        print(f"  订单ID: {buy_result.order_id}, 状态: {buy_result.status}")

        # 4. 确认成交
        print("\n[4] confirm_order(BUY)...")
        confirmed = broker.confirm_order(buy_result.order_id, fill_price=1800.00, fill_qty=50)
        assert confirmed is not None
        assert confirmed.status == OrderStatus.FILLED
        assert confirmed.filled_quantity == 50
        assert confirmed.commission > 0
        assert confirmed.stamp_tax == 0  # 买入不收取印花税
        assert confirmed.amount == 1800.00 * 50  # 90,000
        print(f"  成交状态: {confirmed.status}")
        print(f"  成交价: {confirmed.filled_price}")
        print(f"  佣金: {confirmed.commission:.2f}")
        print(f"  印花税: {confirmed.stamp_tax:.2f}")

        # 5. 验证账户
        print("\n[5] get_account()...")
        account = broker.get_account()
        assert account.position_count == 1
        expected_cost = 1800 * 50 + confirmed.commission + confirmed.stamp_tax
        print(f"  持仓数: {account.position_count}")
        print(f"  剩余现金: {account.available_cash:,.2f}")
        print(f"  市值: {account.market_value:,.2f}")
        print(f"  总资产: {account.total_assets:,.2f}")

        # 6. 提交卖出信号并确认
        print("\n[6] submit_order(SELL) + confirm_order...")
        sell_req = OrderRequest(
            ts_code='600519.SH',
            side=OrderSide.SELL,
            quantity=25,
            price=1850.00,
            order_type=OrderType.LIMIT,
            reason='止盈卖出',
            stock_name='贵州茅台',
        )
        sell_result = broker.submit_order(sell_req)
        confirmed_sell = broker.confirm_order(sell_result.order_id, fill_price=1850.00, fill_qty=25)
        assert confirmed_sell.status == OrderStatus.FILLED
        assert confirmed_sell.stamp_tax > 0  # 卖出收取印花税
        assert broker.get_account().position_count == 1  # 还有25股
        print(f"  卖出成交: {confirmed_sell.filled_quantity}股 @ {confirmed_sell.filled_price}")

        # 7. 获取待处理信号
        print("\n[7] get_pending_signals()...")
        pending = broker.get_pending_signals()
        assert len(pending) == 0
        print(f"  待处理信号数: {len(pending)}")

        # 8. 拒绝信号测试
        print("\n[8] reject_order()...")
        new_req = OrderRequest(
            ts_code='000858.SZ',
            side=OrderSide.BUY,
            quantity=200,
            price=150.00,
            reason='测试拒绝',
            stock_name='五粮液',
        )
        new_result = broker.submit_order(new_req)
        rejected = broker.reject_order(new_result.order_id, reason='风险过高，不跟单')
        assert rejected is not None
        assert rejected.status == OrderStatus.REJECTED
        assert '风险过高' in rejected.error_message
        print(f"  已拒绝: {rejected.order_id}, 原因: {rejected.error_message}")

        # 9. 同步持仓
        print("\n[9] sync_positions()...")
        broker.sync_positions({
            '000001.SZ': {
                'name': '平安银行',
                'quantity': 500,
                'cost_price': 10.50,
                'current_price': 11.20,
                'entry_date': '20240601',
                'industry': '银行',
            }
        })
        positions = broker.get_positions()
        assert len(positions) == 1
        assert '000001.SZ' in positions
        assert positions['000001.SZ'].quantity == 500
        print(f"  同步后持仓数: {len(positions)}")
        print(f"  平安银行: {positions['000001.SZ'].quantity}股 @ {positions['000001.SZ'].cost_price}")

        # 10. 更新行情
        print("\n[10] update_prices()...")
        broker.update_prices({
            '000001.SZ': {'close': 11.80, 'name': '平安银行', 'industry': '银行'},
        })
        pos = broker.get_positions()['000001.SZ']
        assert pos.current_price == 11.80
        assert pos.market_value == 11.80 * 500
        print(f"  现价: {pos.current_price}, 市值: {pos.market_value:,.2f}")

        # 11. 更新现金
        print("\n[11] update_cash()...")
        broker.update_cash(50_000)
        assert broker._cash == 50_000
        account = broker.get_account()
        print(f"  更新后现金: {account.available_cash:,.2f}")

        # 12. 持久化测试
        print("\n[12] 持久化测试 (_save_state / _load_state)...")
        broker.disconnect()

        # 重新加载
        broker2 = ManualBroker(config)
        broker2.connect()
        positions2 = broker2.get_positions()
        assert len(positions2) == 1
        assert '000001.SZ' in positions2
        assert positions2['000001.SZ'].quantity == 500
        assert broker2._cash == 50_000
        print(f"  重新加载成功: 持仓 {len(positions2)} 只, 现金 {broker2._cash:,.2f}")

        # 13. 获取订单历史
        print("\n[13] get_orders()...")
        all_orders = broker2.get_orders()
        filled_orders = broker2.get_orders(status=OrderStatus.FILLED)
        rejected_orders = broker2.get_orders(status=OrderStatus.REJECTED)
        print(f"  总订单: {len(all_orders)}")
        print(f"  已成交: {len(filled_orders)}")
        print(f"  已拒绝: {len(rejected_orders)}")

        # 14. cancel_order 测试
        print("\n[14] cancel_order()...")
        cancel_req = OrderRequest(
            ts_code='600036.SH',
            side=OrderSide.BUY,
            quantity=100,
            price=35.00,
            reason='测试撤销',
            stock_name='招商银行',
        )
        cancel_result = broker2.submit_order(cancel_req)
        assert cancel_result.status == OrderStatus.PENDING
        cancelled = broker2.cancel_order(cancel_result.order_id)
        assert cancelled is True
        assert broker2.get_order(cancel_result.order_id).status == OrderStatus.CANCELLED
        print(f"  撤销成功: {cancel_result.order_id}")

        broker2.disconnect()

    print("\n" + "=" * 60)
    print("全部验证测试通过!")
    print("=" * 60)
