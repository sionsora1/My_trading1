"""
回测引擎
改进：增加每日操作报告、止损止盈、更详细的交易记录
"""

from dataclasses import dataclass, field
from typing import List, Dict
from datetime import datetime
import pandas as pd

from .matcher import MatchEngine, Order, OrderSide, OrderStatus
from config.settings import BACKTEST_CONFIG


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 100_000
    commission_rate: float = 0.0003
    stamp_tax_rate: float = 0.0005
    slippage_rate: float = 0.002
    min_commission: float = 5.0
    max_position_num: int = 5
    max_single_weight: float = 0.15
    stop_loss_rate: float = -0.08
    move_stop_rate: float = -0.10
    limit_up_rate: float = 0.10
    limit_down_rate: float = -0.10
    t_plus_1: bool = True
    start_date: str = '20230101'
    end_date: str = '20241231'
    rebalance_frequency: str = 'weekly'  # daily/weekly/monthly

    @classmethod
    def from_dict(cls, d: dict) -> 'BacktestConfig':
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Position:
    """持仓"""
    ts_code: str
    quantity: int
    cost_price: float
    current_price: float = 0
    market_value: float = 0
    profit: float = 0
    profit_rate: float = 0
    entry_date: str = ''
    stop_loss_price: float = 0
    highest_price: float = 0  # 用于移动止盈

@dataclass
class TradeRecord:
    """成交记录"""
    order_id: str
    ts_code: str
    side: str
    price: float
    quantity: int
    amount: float
    commission: float
    slippage: float
    trade_date: str
    reason: str = ''  # 交易原因

@dataclass
class DailyOperation:
    """每日操作记录"""
    date: str
    buys: List[dict] = field(default_factory=list)
    sells: List[dict] = field(default_factory=list)
    holds: List[dict] = field(default_factory=list)
    portfolio_value: float = 0
    cash: float = 0
    position_count: int = 0
    daily_return: float = 0
    cumulative_return: float = 0


class BacktestEngine:
    """回测引擎"""

    def __init__(self, config):
        if isinstance(config, dict):
            self.config = BacktestConfig.from_dict(config)
        else:
            self.config = config

        self.match_engine = MatchEngine({
            'commission_rate': self.config.commission_rate,
            'stamp_tax_rate': self.config.stamp_tax_rate,
            'slippage_rate': self.config.slippage_rate,
            'min_commission': self.config.min_commission,
            'limit_up_rate': self.config.limit_up_rate,
            'limit_down_rate': self.config.limit_down_rate,
        })

        self.cash = self.config.initial_capital
        self.positions: Dict[str, Position] = {}
        self.trade_records: List[TradeRecord] = []
        self.daily_nav: List[dict] = []
        self.daily_operations: List[DailyOperation] = []  # 每日操作记录
        self.order_counter = 0
        self.current_date = ''

    def reset(self):
        """重置回测状态"""
        self.cash = self.config.initial_capital
        self.positions = {}
        self.trade_records = []
        self.daily_nav = []
        self.daily_operations = []
        self.order_counter = 0

    def generate_order_id(self) -> str:
        self.order_counter += 1
        return f"BT_{self.order_counter:06d}"

    def get_portfolio(self) -> dict:
        """获取当前账户状态"""
        positions_value = sum(p.market_value for p in self.positions.values())
        total_value = self.cash + positions_value
        total_profit = total_value - self.config.initial_capital
        total_profit_rate = total_profit / self.config.initial_capital

        return {
            'cash': self.cash,
            'positions': {k: {
                'ts_code': v.ts_code,
                'quantity': v.quantity,
                'cost_price': v.cost_price,
                'current_price': v.current_price,
                'market_value': v.market_value,
                'profit': v.profit,
                'profit_rate': v.profit_rate,
                'entry_date': v.entry_date,
                'stop_loss_price': v.stop_loss_price,
                'highest_price': v.highest_price,
            } for k, v in self.positions.items()},
            'total_value': total_value,
            'total_profit': total_profit,
            'total_profit_rate': total_profit_rate
        }

    def update_positions(self, market_data: dict):
        """更新持仓市值"""
        for ts_code, pos in self.positions.items():
            if ts_code in market_data:
                current_price = market_data[ts_code].get('close', pos.current_price)
                pos.current_price = current_price
                pos.market_value = current_price * pos.quantity
                pos.profit = (current_price - pos.cost_price) * pos.quantity
                pos.profit_rate = (current_price / pos.cost_price - 1) if pos.cost_price > 0 else 0
                # 更新最高价
                if current_price > pos.highest_price:
                    pos.highest_price = current_price

    def record_daily_nav(self, date: str):
        """记录每日净值"""
        portfolio = self.get_portfolio()
        prev_value = self.daily_nav[-1]['total_value'] if self.daily_nav else self.config.initial_capital
        daily_return = (portfolio['total_value'] / prev_value - 1) if prev_value > 0 else 0

        self.daily_nav.append({
            'date': date,
            'total_value': portfolio['total_value'],
            'cash': portfolio['cash'],
            'position_value': portfolio['total_value'] - portfolio['cash'],
            'position_count': len(portfolio['positions']),
            'total_return': portfolio['total_profit_rate'],
            'daily_return': daily_return,
        })

    def calculate_buy_quantity(self, price: float, target_weight: float) -> int:
        """计算买入数量（100股整数倍），动态利用剩余资金"""
        portfolio = self.get_portfolio()
        # 剩余可用槽位
        remaining = max(self.config.max_position_num - len(self.positions), 1)
        # 动态权重：总利用率 / 总槽位数
        target_utilization = 0.95
        dynamic_weight = target_utilization / max(self.config.max_position_num, 1)
        weight = max(target_weight, dynamic_weight)
        # 不超过配置的单只仓位上限
        weight = min(weight, self.config.max_single_weight)
        target_amount = portfolio['total_value'] * weight
        # 按剩余槽位分配现金，避免前期买太少后期买不起
        available_amount = min(target_amount, self.cash * 0.98 / remaining)
        quantity = int(available_amount / price / 100) * 100
        return max(quantity, 0)

    def get_sell_quantity(self, ts_code: str) -> int:
        """获取可卖数量"""
        if ts_code in self.positions:
            return self.positions[ts_code].quantity
        return 0

    def check_t_plus_1(self, ts_code: str, current_date: str) -> bool:
        """检查T+1限制：返回True表示可卖出"""
        if not self.config.t_plus_1:
            return True
        if ts_code not in self.positions:
            return False  # 无持仓，不能卖出
        return current_date > self.positions[ts_code].entry_date

    def check_stop_loss(self, ts_code: str, current_price: float) -> bool:
        """检查止损"""
        if ts_code in self.positions:
            pos = self.positions[ts_code]
            if pos.stop_loss_price > 0 and current_price <= pos.stop_loss_price:
                return True
        return False

    def check_move_stop(self, ts_code: str, current_price: float) -> bool:
        """检查移动止盈"""
        if ts_code in self.positions:
            pos = self.positions[ts_code]
            if pos.highest_price > 0:
                drawdown = (current_price - pos.highest_price) / pos.highest_price
                if drawdown <= self.config.move_stop_rate:
                    return True
        return False

    def execute_buy(self, ts_code: str, price: float, quantity: int, stock_data: dict, reason: str = '') -> bool:
        """执行买入"""
        if quantity < 100:
            return False

        prev_close = stock_data.get('prev_close', price)
        order = Order(
            order_id=self.generate_order_id(),
            ts_code=ts_code,
            side=OrderSide.BUY,
            price=price,
            quantity=quantity,
            order_date=self.current_date
        )

        order = self.match_engine.match_order(order, stock_data, prev_close)

        if order.status != OrderStatus.FILLED:
            return False

        amount = order.fill_price * order.fill_quantity
        total_cost = amount + order.commission

        if total_cost > self.cash:
            return False

        self.cash -= total_cost

        # 计算止损价
        stop_loss_price = order.fill_price * (1 + self.config.stop_loss_rate)

        if ts_code in self.positions:
            pos = self.positions[ts_code]
            total_quantity = pos.quantity + order.fill_quantity
            avg_cost = (pos.cost_price * pos.quantity + order.fill_price * order.fill_quantity) / total_quantity
            pos.quantity = total_quantity
            pos.cost_price = avg_cost
            pos.stop_loss_price = avg_cost * (1 + self.config.stop_loss_rate)
        else:
            self.positions[ts_code] = Position(
                ts_code=ts_code,
                quantity=order.fill_quantity,
                cost_price=order.fill_price,
                current_price=order.fill_price,
                market_value=amount,
                entry_date=self.current_date,
                stop_loss_price=stop_loss_price,
                highest_price=order.fill_price
            )

        self.trade_records.append(TradeRecord(
            order_id=order.order_id,
            ts_code=ts_code,
            side='BUY',
            price=order.fill_price,
            quantity=order.fill_quantity,
            amount=amount,
            commission=order.commission,
            slippage=order.slippage,
            trade_date=self.current_date,
            reason=reason
        ))

        return True

    def execute_sell(self, ts_code: str, price: float, quantity: int, stock_data: dict, reason: str = '') -> bool:
        """执行卖出"""
        if ts_code not in self.positions or quantity <= 0:
            return False

        if not self.check_t_plus_1(ts_code, self.current_date):
            return False

        prev_close = stock_data.get('prev_close', price)
        order = Order(
            order_id=self.generate_order_id(),
            ts_code=ts_code,
            side=OrderSide.SELL,
            price=price,
            quantity=quantity,
            order_date=self.current_date
        )

        order = self.match_engine.match_order(order, stock_data, prev_close)

        if order.status != OrderStatus.FILLED:
            return False

        amount = order.fill_price * order.fill_quantity
        self.cash += amount - order.commission

        # 计算盈亏
        pos = self.positions[ts_code]
        profit = (order.fill_price - pos.cost_price) * quantity
        profit_rate = (order.fill_price / pos.cost_price - 1) if pos.cost_price > 0 else 0

        self.positions[ts_code].quantity -= order.fill_quantity
        if self.positions[ts_code].quantity <= 0:
            del self.positions[ts_code]

        self.trade_records.append(TradeRecord(
            order_id=order.order_id,
            ts_code=ts_code,
            side='SELL',
            price=order.fill_price,
            quantity=order.fill_quantity,
            amount=amount,
            commission=order.commission,
            slippage=order.slippage,
            trade_date=self.current_date,
            reason=f"{reason} | 盈亏: {profit:+.0f} ({profit_rate:+.2%})"
        ))

        return True

    def check_rebalance_day(self, date: str, prev_date: str = None) -> bool:
        """判断是否是调仓日"""
        if self.config.rebalance_frequency == 'daily':
            return True

        dt = datetime.strptime(date, '%Y%m%d')

        if self.config.rebalance_frequency == 'weekly':
            # 每周一调仓
            return dt.weekday() == 0
        elif self.config.rebalance_frequency == 'monthly':
            # 每月第一个交易日调仓
            if prev_date is None:
                return True
            prev_dt = datetime.strptime(prev_date, '%Y%m%d')
            return dt.month != prev_dt.month

        return False

    def check_stop_loss_and_take_profit(self, market_data: dict) -> List[dict]:
        """检查止损止盈"""
        stop_signals = []

        for ts_code, pos in list(self.positions.items()):
            if ts_code not in market_data:
                continue

            current_price = market_data[ts_code].get('close', pos.current_price)

            # 检查止损
            if self.check_stop_loss(ts_code, current_price):
                stop_signals.append({
                    'ts_code': ts_code,
                    'signal': 'SELL',
                    'reason': f'止损触发（止损价: {pos.stop_loss_price:.2f}）'
                })
            # 检查移动止盈
            elif self.check_move_stop(ts_code, current_price):
                stop_signals.append({
                    'ts_code': ts_code,
                    'signal': 'SELL',
                    'reason': f'移动止盈触发（最高价: {pos.highest_price:.2f}，回撤超{abs(self.config.move_stop_rate):.0%}）'
                })

        return stop_signals

    def generate_daily_operation(self, date: str, market_data: dict,
                                  buy_records: List[TradeRecord],
                                  sell_records: List[TradeRecord]) -> DailyOperation:
        """生成每日操作记录"""
        portfolio = self.get_portfolio()
        prev_nav = self.daily_nav[-2]['total_value'] if len(self.daily_nav) > 1 else self.config.initial_capital
        daily_return = (portfolio['total_value'] / prev_nav - 1) if prev_nav > 0 else 0

        op = DailyOperation(
            date=date,
            portfolio_value=portfolio['total_value'],
            cash=portfolio['cash'],
            position_count=len(portfolio['positions']),
            daily_return=daily_return,
            cumulative_return=portfolio['total_profit_rate']
        )

        # 记录买入
        for t in buy_records:
            if t.trade_date == date:
                stock = market_data.get(t.ts_code, {})
                op.buys.append({
                    'ts_code': t.ts_code,
                    'name': stock.get('name', ''),
                    'price': t.price,
                    'quantity': t.quantity,
                    'amount': t.amount,
                    'reason': t.reason
                })

        # 记录卖出
        for t in sell_records:
            if t.trade_date == date:
                stock = market_data.get(t.ts_code, {})
                op.sells.append({
                    'ts_code': t.ts_code,
                    'name': stock.get('name', ''),
                    'price': t.price,
                    'quantity': t.quantity,
                    'amount': t.amount,
                    'reason': t.reason
                })

        # 记录持仓
        for ts_code, pos in portfolio['positions'].items():
            stock = market_data.get(ts_code, {})
            op.holds.append({
                'ts_code': ts_code,
                'name': stock.get('name', ''),
                'quantity': pos['quantity'],
                'cost_price': pos['cost_price'],
                'current_price': pos['current_price'],
                'profit_rate': pos['profit_rate'],
            })

        return op

    def print_daily_operation(self, op: DailyOperation):
        """打印每日操作报告"""
        print(f"\n{'='*70}")
        print(f"【每日操作报告】{op.date}")
        print(f"{'='*70}")

        # 账户概况
        print(f"\n◆ 账户概况")
        print(f"  总资产: {op.portfolio_value:>14,.2f}")
        print(f"  可用资金: {op.cash:>12,.2f}")
        print(f"  持仓数量: {op.position_count}只")
        print(f"  今日收益: {op.daily_return:>+.2%}")
        print(f"  累计收益: {op.cumulative_return:>+.2%}")

        # 卖出操作
        if op.sells:
            print(f"\n  [卖出] {len(op.sells)}笔")
            for s in op.sells:
                print(f"    x {s['ts_code']} {s['name']}")
                print(f"      成交价: {s['price']:.2f} | 数量: {s['quantity']}股 | 金额: {s['amount']:,.0f}")
                print(f"      原因: {s['reason']}")

        # 买入操作
        if op.buys:
            print(f"\n  [买入] {len(op.buys)}笔")
            for b in op.buys:
                print(f"    + {b['ts_code']} {b['name']}")
                print(f"      成交价: {b['price']:.2f} | 数量: {b['quantity']}股 | 金额: {b['amount']:,.0f}")
                print(f"      原因: {b['reason']}")

        # 当前持仓
        if op.holds:
            print(f"\n◆ 当前持仓（{len(op.holds)}只）")
            print(f"  {'代码':<12} {'名称':<8} {'数量':>6} {'成本':>8} {'现价':>8} {'盈亏率':>8}")
            print(f"  {'-'*54}")
            for h in op.holds[:15]:  # 最多显示15只
                sign = "+" if h['profit_rate'] > 0 else "-" if h['profit_rate'] < 0 else " "
                print(f"  {h['ts_code']:<12} {h['name']:<8} {h['quantity']:>6} "
                      f"{h['cost_price']:>8.2f} {h['current_price']:>8.2f} "
                      f"[{sign}]{h['profit_rate']:>+7.2%}")
            if len(op.holds) > 15:
                print(f"  ... 还有{len(op.holds)-15}只")

        print(f"{'='*70}")

    def run(self, market_data_by_date: dict, strategy, print_report: bool = True) -> dict:
        """
        运行回测

        Args:
            market_data_by_date: {date: {ts_code: stock_data}}
            strategy: 策略对象
            print_report: 是否打印每日报告
        """
        from .performance import PerformanceAnalyzer

        self.reset()
        dates = sorted(market_data_by_date.keys())
        prev_date = None

        print(f"开始回测 | 区间: {dates[0]} ~ {dates[-1]} | 初始资金: {self.config.initial_capital:,.0f}")
        print(f"调仓频率: {self.config.rebalance_frequency} | 止损线: {self.config.stop_loss_rate:.0%} | 移动止盈: {self.config.move_stop_rate:.0%}")

        for i, date in enumerate(dates):
            self.current_date = date
            market_data = market_data_by_date[date]

            # 更新持仓
            self.update_positions(market_data)

            # 检查止损止盈
            stop_signals = self.check_stop_loss_and_take_profit(market_data)

            # 记录本日买入卖出
            day_buys = []
            day_sells = []

            # 执行止损止盈卖出
            for sig in stop_signals:
                ts_code = sig['ts_code']
                quantity = self.get_sell_quantity(ts_code)
                if quantity > 0 and ts_code in market_data:
                    price = market_data[ts_code].get('open', market_data[ts_code].get('close', 0))
                    old_count = len(self.trade_records)
                    if self.execute_sell(ts_code, price, quantity, market_data[ts_code], sig['reason']):
                        if len(self.trade_records) > old_count:
                            day_sells.append(self.trade_records[-1])

            # 判断是否调仓日
            is_rebalance = self.check_rebalance_day(date, prev_date)

            if is_rebalance:
                portfolio = self.get_portfolio()
                signals = strategy.generate_signals(date, market_data, portfolio)

                sell_signals = [s for s in signals if s['signal'] == 'SELL']
                buy_signals = [s for s in signals if s['signal'] == 'BUY']

                # 执行卖出
                for sig in sell_signals:
                    ts_code = sig['ts_code']
                    if ts_code in [s.ts_code for s in day_sells]:
                        continue  # 已经止损卖出的跳过
                    quantity = self.get_sell_quantity(ts_code)
                    if quantity > 0 and ts_code in market_data:
                        price = market_data[ts_code].get('open', market_data[ts_code].get('close', 0))
                        old_count = len(self.trade_records)
                        if self.execute_sell(ts_code, price, quantity, market_data[ts_code], sig['reason']):
                            if len(self.trade_records) > old_count:
                                day_sells.append(self.trade_records[-1])

                # 执行买入
                for sig in buy_signals:
                    ts_code = sig['ts_code']
                    if len(self.positions) >= self.config.max_position_num:
                        break
                    if ts_code in self.positions or ts_code not in market_data:
                        continue

                    price = market_data[ts_code].get('open', market_data[ts_code].get('close', 0))
                    quantity = self.calculate_buy_quantity(price, sig['weight'])
                    if quantity >= 100:
                        old_count = len(self.trade_records)
                        if self.execute_buy(ts_code, price, quantity, market_data[ts_code], sig['reason']):
                            if len(self.trade_records) > old_count:
                                day_buys.append(self.trade_records[-1])

            # 记录净值
            self.record_daily_nav(date)

            # 生成每日操作记录
            if day_buys or day_sells or is_rebalance:
                op = self.generate_daily_operation(date, market_data, day_buys, day_sells)
                self.daily_operations.append(op)

                if print_report:
                    self.print_daily_operation(op)

            prev_date = date

        # 最终绩效
        metrics = PerformanceAnalyzer.calculate_metrics(
            self.daily_nav, self.trade_records, self.config.initial_capital
        )

        PerformanceAnalyzer.print_report(metrics)

        return {
            'metrics': metrics,
            'daily_nav': self.daily_nav,
            'trade_records': self.trade_records,
            'daily_operations': self.daily_operations,
            'final_portfolio': self.get_portfolio()
        }

    def export_daily_report(self, filepath: str = 'daily_report.csv'):
        """导出每日操作报告到CSV"""
        rows = []
        for op in self.daily_operations:
            row = {
                '日期': op.date,
                '总资产': op.portfolio_value,
                '可用资金': op.cash,
                '持仓数量': op.position_count,
                '今日收益': op.daily_return,
                '累计收益': op.cumulative_return,
                '买入数量': len(op.buys),
                '卖出数量': len(op.sells),
                '买入标的': ','.join([b['ts_code'] for b in op.buys]),
                '卖出标的': ','.join([s['ts_code'] for s in op.sells]),
            }
            rows.append(row)

        df = pd.DataFrame(rows)
        df.to_csv(filepath, index=False, encoding='utf-8-sig')
        print(f"每日报告已导出: {filepath}")