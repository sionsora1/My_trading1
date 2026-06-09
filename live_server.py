"""
A股量化交易系统 - 实盘交易服务
支持模拟盘和实盘（QMT）两种模式
"""

import sys
import os
import time
import json
import threading
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broker import (
    get_broker, BROKER_REGISTRY, SignalNotifier, RiskManager,
    OrderRequest, OrderResult, OrderSide, OrderType, OrderStatus, Signal, DailyRiskLimit
)
from broker.executor import TradeChecklist
from strategy import get_strategy, STRATEGY_REGISTRY
from data.fetcher import DataFetcher, DataCache
from config.settings import LIVE_TRADING_CONFIG, BACKTEST_CONFIG


class LiveTradingServer:
    """
    实盘交易服务器

    两种运行模式：
    - 全自动 (auto): 策略信号 → 风控过滤 → 自动下单
    - 半自动 (semi): 策略信号 → 风控过滤 → 通知用户 → 确认后下单
    """

    def __init__(self, config: dict = None):
        self.config = config or LIVE_TRADING_CONFIG

        # 模式
        self.broker_name = self.config.get('broker', 'sim')
        self.trade_mode = self.config.get('mode', 'semi')

        # 初始化组件
        self._init_broker()
        self._init_risk_manager()
        self._init_notifier()
        self._init_data()

        # 状态
        self.running = False
        self.scan_thread = None
        self.last_scan_time = None
        self.scan_count = 0
        self.today_orders = []

        print(f"[实盘] 初始化完成 | 券商: {self.broker_name}({BROKER_REGISTRY[self.broker_name]['name']}) "
              f"| 模式: {'全自动' if self.trade_mode == 'auto' else '半自动'}")

    def _init_broker(self):
        """初始化券商连接器"""
        broker_config = self.config.get(self.broker_name, {})
        self.broker = get_broker(self.broker_name, broker_config)
        if not self.broker.connect():
            print(f"[实盘] 券商连接失败，使用模拟盘")
            self.broker = get_broker('sim', self.config.get('sim', {}))
            self.broker.connect()
            self.broker_name = 'sim'

    def _init_risk_manager(self):
        """初始化风控"""
        risk_config = self.config.get('risk', {})
        risk_limit = DailyRiskLimit(
            max_daily_loss_rate=risk_config.get('max_daily_loss_rate', 0.02),
            max_single_position_weight=risk_config.get('max_single_position_weight', 0.05),
            max_total_positions=risk_config.get('max_total_positions', 20),
            max_single_order_amount=risk_config.get('max_single_order_amount', 200000),
            require_confirm_large=risk_config.get('require_confirm_large', True),
            large_order_threshold=risk_config.get('large_order_threshold', 50000),
        )
        self.risk_manager = RiskManager(risk_limit)

    def _init_notifier(self):
        """初始化通知器"""
        notify_config = self.config.get('notify', {})
        self.notifier = SignalNotifier(notify_config)

        # 初始化执行清单
        data_dir = self.config.get('sim', {}).get('data_dir', './data_cache')
        self.checklist = TradeChecklist(data_dir)

    def _init_data(self):
        """初始化数据获取"""
        self.fetcher = DataFetcher()
        self.cache = DataCache()
        self.data_cache_ttl = 300  # 行情数据缓存5分钟

    # ============================================================
    # 状态查询
    # ============================================================

    def get_status(self) -> dict:
        """获取实盘服务状态"""
        account = self.broker.get_account()
        positions = self.broker.get_positions_list()
        risk = self.risk_manager.get_status()
        signal_stats = self.notifier.get_signal_stats()

        return {
            'running': self.running,
            'broker_name': self.broker_name,
            'broker_label': BROKER_REGISTRY[self.broker_name]['name'],
            'trade_mode': self.trade_mode,
            'trade_mode_label': '全自动' if self.trade_mode == 'auto' else '半自动',
            'account': {
                'total_assets': account.total_assets,
                'available_cash': account.available_cash,
                'market_value': account.market_value,
                'total_profit': account.total_profit,
                'total_profit_rate': account.total_profit_rate,
                'daily_profit': account.daily_profit,
                'position_count': account.position_count,
                'update_time': account.update_time,
            },
            'positions': positions,
            'risk': risk,
            'signals': signal_stats,
            'last_scan_time': self.last_scan_time,
            'scan_count': self.scan_count,
            'today_orders': len(self.today_orders),
        }

    def get_account(self) -> dict:
        """获取账户信息"""
        return self.broker.get_account_summary()

    def get_positions(self) -> list:
        """获取持仓"""
        return self.broker.get_positions_list()

    def get_orders(self, status: str = None, limit: int = 50) -> list:
        """获取订单"""
        order_status = OrderStatus(status) if status else None
        return self.broker.get_orders_list(order_status, limit)

    def get_signals(self) -> list:
        """获取信号"""
        return self.notifier.get_pending_signals()

    def get_signal_history(self) -> list:
        """获取信号历史"""
        return self.notifier.get_all_signals()

    # ============================================================
    # 交易操作
    # ============================================================

    def submit_order(self, ts_code: str, side: str, quantity: int,
                     price: float = 0, reason: str = '') -> dict:
        """手动/自动下单"""
        # 获取账户和持仓
        account = self.broker.get_account()
        positions = self.broker.get_positions()

        # 风控检查
        order_side = OrderSide.BUY if side.upper() == 'BUY' else OrderSide.SELL
        # 获取股票信息（名称等）
        stock_info = self._get_stock_info(ts_code)

        request = OrderRequest(
            ts_code=ts_code,
            side=order_side,
            quantity=quantity,
            price=price,
            order_type=OrderType.MARKET if price == 0 else OrderType.LIMIT,
            reason=reason,
            stock_name=stock_info.get('name', ts_code),
        )

        check = self.risk_manager.check_order(request, account, positions, stock_info)
        if not check.passed:
            return {
                'success': False,
                'error': check.reason,
                'severity': check.severity,
                'requires_confirm': check.requires_confirm,
            }

        # 需要人工确认
        if check.requires_confirm and self.trade_mode == 'semi':
            # 创建信号而非直接下单
            signal = Signal(
                ts_code=ts_code,
                name=stock_info.get('name', ts_code),
                signal=side.upper(),
                reason=reason,
                price=price,
                strategy='manual',
            )
            self.notifier.add_signal(signal)
            return {
                'success': True,
                'pending_confirm': True,
                'message': f'大额订单（{price * quantity:,.0f}元）需人工确认',
                'signal': {
                    'ts_code': ts_code,
                    'signal': side.upper(),
                    'reason': reason,
                }
            }

        # 执行下单
        result = self.broker.submit_order(request)

        if result.status == OrderStatus.FILLED:
            self.risk_manager.record_trade(result.amount)
            self.today_orders.append(result.order_id)
            return {
                'success': True,
                'order': {
                    'order_id': result.order_id,
                    'ts_code': result.ts_code,
                    'side': result.side.value,
                    'filled_price': result.filled_price,
                    'filled_quantity': result.filled_quantity,
                    'amount': result.amount,
                    'commission': result.commission,
                    'status': result.status.value,
                }
            }
        else:
            return {
                'success': False,
                'error': result.error_message or f'订单状态: {result.status.value}',
                'status': result.status.value,
            }

    def confirm_signal(self, ts_code: str, strategy: str, signal_type: str,
                       confirmed: bool = True) -> dict:
        """确认或拒绝信号"""
        success = self.notifier.confirm_signal(ts_code, strategy, signal_type, confirmed)

        if not success:
            return {'success': False, 'error': '信号不存在'}

        if not confirmed:
            return {'success': True, 'action': 'rejected', 'message': '信号已拒绝'}

        # 确认后执行下单
        # 从待处理信号中找到该信号
        pending = self.notifier.get_pending_signals()
        matched = None
        for s in pending:
            if s['ts_code'] == ts_code and s['strategy'] == strategy:
                matched = s
                break

        if not matched:
            return {'success': False, 'error': '信号已过期'}

        # 计算买入数量
        stock_info = self._get_stock_info(ts_code)
        price = matched.get('price', 0)
        if price == 0 and stock_info:
            price = stock_info.get('close', 0)

        account = self.broker.get_account()
        if matched['signal'] == 'BUY':
            weight = matched.get('weight', 0.05)
            target_amount = account.total_assets * weight
            quantity = int(target_amount / price / 100) * 100 if price > 0 else 100
            quantity = max(quantity, 100)
        else:
            positions = self.broker.get_positions()
            pos = positions.get(ts_code)
            quantity = pos.quantity if pos else 0

        if quantity < 100 and matched['signal'] == 'BUY':
            return {'success': False, 'error': f'可用资金不足，无法买入{ts_code}'}

        # 执行下单
        result = self.submit_order(
            ts_code=ts_code,
            side=matched['signal'],
            quantity=quantity,
            price=price,
            reason=matched.get('reason', '')
        )

        if result.get('success'):
            self.notifier.mark_executed(ts_code, strategy, signal_type,
                                        result.get('order', {}).get('order_id', ''))

        return result

    def cancel_order(self, order_id: str) -> dict:
        """撤销订单"""
        success = self.broker.cancel_order(order_id)
        return {'success': success, 'message': '已撤销' if success else '撤单失败'}

    def record_manual_trade(self, ts_code: str, side: str, price: float,
                           quantity: int, reason: str = '') -> dict:
        """
        记录一笔在APP上手动执行的交易

        用户在东方财富APP上完成交易后，
        回到系统记录成交信息，系统自动更新持仓和账户状态。

        Args:
            ts_code: 股票代码
            side: BUY / SELL
            price: 实际成交价
            quantity: 实际成交数量
            reason: 备注

        Returns:
            dict: 记录结果
        """
        if price <= 0 or quantity <= 0:
            return {'success': False, 'error': '价格和数量必须大于0'}

        # 风控检查（仅做黑名单/ST检查，不做仓位和资金检查——因为用户已在APP执行）
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        stock_info = self._get_stock_info(ts_code)

        # ST/退市检查
        if stock_info:
            name = stock_info.get('name', '')
            if any(kw.upper() in name.upper() for kw in ['ST', '*ST', '退市']):
                return {'success': False, 'error': f'{ts_code} {name} 风险警示股，禁止交易'}
            if stock_info.get('st_flag', False):
                return {'success': False, 'error': f'{ts_code} 标记为ST/退市'}

        # 黑名单检查
        if ts_code in self.risk_manager.config.blacklist:
            return {'success': False, 'error': f'{ts_code} 在黑名单中'}

        order_side = OrderSide.BUY if side.upper() == 'BUY' else OrderSide.SELL

        # 直接记录到 SimBroker（作为真实持仓镜像，不检查资金——用户在APP用自己的钱交易）
        if hasattr(self.broker, '_positions') and hasattr(self.broker, '_cash'):
            try:
                amount = price * quantity
                commission = max(amount * self.broker.commission_rate, self.broker.min_commission)
                stamp_tax = amount * self.broker.stamp_tax_rate if order_side == OrderSide.SELL else 0
                total_fee = commission + stamp_tax

                from broker.base import PositionInfo

                if order_side == OrderSide.BUY:
                    # 直接加持仓，不扣现金（用户真实账户已扣）
                    if ts_code in self.broker._positions:
                        pos = self.broker._positions[ts_code]
                        total_qty = pos.quantity + quantity
                        avg_cost = (pos.cost_price * pos.quantity + price * quantity) / total_qty
                        pos.quantity = total_qty
                        pos.available_quantity = total_qty
                        pos.cost_price = avg_cost
                        pos.current_price = price
                        pos.market_value = price * total_qty
                        pos.profit = (price - avg_cost) * total_qty
                        pos.profit_rate = (price / avg_cost - 1) if avg_cost > 0 else 0
                    else:
                        self.broker._positions[ts_code] = PositionInfo(
                            ts_code=ts_code,
                            name=stock_info.get('name', ts_code),
                            quantity=quantity,
                            available_quantity=quantity,
                            cost_price=price,
                            current_price=price,
                            market_value=amount,
                            entry_date=datetime.now().strftime('%Y%m%d'),
                            industry=stock_info.get('industry', ''),
                        )
                else:  # SELL
                    if ts_code in self.broker._positions:
                        pos = self.broker._positions[ts_code]
                        sell_qty = min(quantity, pos.quantity)
                        pos.quantity -= sell_qty
                        pos.available_quantity -= sell_qty
                        if pos.quantity <= 0:
                            del self.broker._positions[ts_code]
                        else:
                            pos.market_value = price * pos.quantity

                # Record as trade
                order_id = f"MANUAL_{datetime.now().strftime('%Y%m%d%H%M%S')}_{ts_code}"
                result = OrderResult(
                    order_id=order_id,
                    ts_code=ts_code,
                    side=order_side,
                    price=price,
                    quantity=quantity,
                    filled_quantity=quantity,
                    filled_price=price,
                    status=OrderStatus.FILLED,
                    commission=commission,
                    stamp_tax=stamp_tax,
                    amount=amount,
                    create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    reason=reason or '手动记录（APP执行后录入）',
                )
                self.broker._orders.append(result)
                self.risk_manager.record_trade(amount)
                self.today_orders.append(order_id)
                self.broker._save_account()

                # 同步更新执行清单
                self.checklist_items_done_for_code(ts_code, price, quantity)

                return {
                    'success': True,
                    'message': f'已记录 {ts_code} {side} {quantity}股 @ {price}',
                    'trade': {
                        'order_id': result.order_id,
                        'ts_code': result.ts_code,
                        'side': side.upper(),
                        'filled_price': result.filled_price,
                        'filled_quantity': result.filled_quantity,
                        'amount': result.amount,
                        'commission': result.commission,
                    }
                }
            except Exception as e:
                return {'success': False, 'error': str(e)}
        else:
            # Fallback：通过 submit_order 走正常流程
            return self.submit_order(ts_code, side, quantity, price, reason)

    def checklist_items_done_for_code(self, ts_code: str, price: float, quantity: int):
        """将清单中对应股票的项目标记为完成"""
        for item in self.checklist.items:
            if item['ts_code'] == ts_code and item['status'] == 'pending':
                self.checklist.mark_executed(item['id'], price, quantity)
                break

    # ============================================================
    # 策略扫描
    # ============================================================

    def scan_and_trade(self) -> dict:
        """
        执行一次策略扫描和交易

        Returns:
            dict: 本次扫描结果
        """
        if self.risk_manager.state.trading_halted:
            return {
                'status': 'halted',
                'reason': '交易已暂停（触发日亏损限制）',
                'signals': [],
                'trades': [],
            }

        try:
            # 1. 获取行情数据
            stock_pool = self.config.get('scan', {}).get('stock_pool', [])
            if not stock_pool:
                return {'status': 'skipped', 'reason': '未配置股票池', 'signals': [], 'trades': []}

            strategy_name = self.config.get('scan', {}).get('strategy', 'all')

            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')

            market_data = self._fetch_market_data(stock_pool, start_date, end_date)
            if not market_data:
                return {'status': 'skipped', 'reason': '获取行情数据失败', 'signals': [], 'trades': []}

            latest_date = sorted(market_data.keys())[-1]
            latest_data = market_data[latest_date]

            # 构建 portfolio 格式
            account = self.broker.get_account()
            positions = self.broker.get_positions()
            portfolio = self.broker.get_account_summary()
            portfolio['positions'] = {
                k: {
                    'ts_code': v.ts_code,
                    'quantity': v.quantity,
                    'cost_price': v.cost_price,
                    'current_price': v.current_price,
                    'profit_rate': v.profit_rate,
                    'highest_price': v.current_price,
                }
                for k, v in positions.items()
            }

            # 2. 确定要运行的策略列表
            if strategy_name == 'all':
                strategies_to_run = list(STRATEGY_REGISTRY.keys())
            elif strategy_name in STRATEGY_REGISTRY:
                strategies_to_run = [strategy_name]
            else:
                strategies_to_run = ['eight_factor']

            # 3. 依次运行每个策略，汇总所有信号
            all_filtered_signals = []
            all_trades = []

            for s_name in strategies_to_run:
                try:
                    strategy = get_strategy(s_name)
                    signals = strategy.generate_signals(latest_date, latest_data, portfolio)

                    # 风控过滤
                    for sig in signals:
                        stock_info = latest_data.get(sig['ts_code'], {})
                        signal_obj = Signal(
                            ts_code=sig['ts_code'],
                            name=stock_info.get('name', sig['ts_code']),
                            signal=sig['signal'],
                            weight=sig.get('weight', 0),
                            reason=sig.get('reason', ''),
                            price=stock_info.get('close', 0),
                            strategy=s_name,
                        )

                        check = self.risk_manager.check_signal(signal_obj, account, positions, stock_info)
                        if check.passed:
                            all_filtered_signals.append(signal_obj)
                except Exception as e:
                    print(f"[实盘] 策略 {s_name} 执行失败: {e}")

            # 4. 去重：同一股票如果多个策略都有信号，取置信度最高的
            seen_buy = set()
            seen_sell = set()
            deduped_signals = []
            for s in sorted(all_filtered_signals, key=lambda x: x.weight or 0, reverse=True):
                if s.signal == 'BUY' and s.ts_code not in seen_buy:
                    seen_buy.add(s.ts_code)
                    deduped_signals.append(s)
                elif s.signal == 'SELL' and s.ts_code not in seen_sell:
                    seen_sell.add(s.ts_code)
                    deduped_signals.append(s)
                elif s.signal not in ('BUY', 'SELL'):
                    deduped_signals.append(s)

            # 5. 处理信号
            max_positions = self.config.get('risk', {}).get('max_total_positions', 5)
            current_pos_count = len(positions)

            for signal in deduped_signals:
                price = signal.price
                if price == 0:
                    stock_info = latest_data.get(signal.ts_code, {})
                    price = stock_info.get('close', 0)

                if signal.signal == 'BUY':
                    # 检查持仓上限
                    if current_pos_count >= max_positions:
                        all_trades.append({
                            'signal': signal.ts_code,
                            'side': signal.signal,
                            'strategy': signal.strategy,
                            'result': {'success': False, 'error': f'持仓已达上限{max_positions}只'},
                        })
                        continue

                    # 动态权重：基于实盘持仓上限计算，确保资金充分利用
                    # 策略返回的 weight 是回测场景的（如5%），实盘需按上限数量重新算
                    target_utilization = 0.90  # 总资金利用率90%（留10%缓冲）
                    dynamic_weight = target_utilization / max(max_positions, 1)
                    # 取策略权重和动态权重中较大的
                    weight = max(signal.weight or 0, dynamic_weight)
                    # 限制不超过风控的单只上限
                    max_allowed = self.config.get('risk', {}).get('max_single_position_weight', 0.20)
                    weight = min(weight, max_allowed)

                    target_amount = account.total_assets * weight
                    # 确保不超过可用资金
                    target_amount = min(target_amount, account.available_cash * 0.95)
                    quantity = int(target_amount / price / 100) * 100 if price > 0 else 100
                    quantity = max(quantity, 100)
                elif signal.signal == 'SELL':
                    pos = positions.get(signal.ts_code)
                    quantity = pos.quantity if pos else 0
                else:
                    continue  # HOLD 信号不交易

                if quantity < 100 and signal.signal == 'BUY':
                    all_trades.append({
                        'signal': signal.ts_code,
                        'side': signal.signal,
                        'strategy': signal.strategy,
                        'result': {'success': False, 'error': '计算数量不足100股'},
                    })
                    continue

                if self.trade_mode == 'auto':
                    result = self.submit_order(
                        ts_code=signal.ts_code,
                        side=signal.signal,
                        quantity=quantity,
                        price=price,
                        reason=f"[{signal.strategy}] {signal.reason}"
                    )
                    if result.get('success'):
                        current_pos_count += 1
                    all_trades.append({
                        'signal': signal.ts_code,
                        'side': signal.signal,
                        'strategy': signal.strategy,
                        'result': result,
                    })
                else:
                    self.notifier.add_signal(signal)
                    all_trades.append({
                        'signal': signal.ts_code,
                        'side': signal.signal,
                        'strategy': signal.strategy,
                        'result': {'success': True, 'pending_confirm': True},
                    })

            # 5. 生成执行清单
            account = self.broker.get_account()
            account_dict = {
                'total_assets': account.total_assets,
                'available_cash': account.available_cash,
                'market_value': account.market_value,
            }
            checklist_data = [
                {
                    'ts_code': s.ts_code,
                    'name': s.name,
                    'signal': s.signal,
                    'price': s.price,
                    'reason': s.reason,
                    'strategy': s.strategy,
                    'weight': s.weight or 0.20,
                }
                for s in deduped_signals
            ]
            self.checklist.generate(checklist_data, account_dict)

            # 6. 打印信号
            if self.config.get('notify', {}).get('console_print', True):
                self.checklist.print_checklist()

            # 7. 更新状态
            self.last_scan_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.scan_count += 1

            return {
                'status': 'success',
                'scan_time': self.last_scan_time,
                'strategies_used': strategies_to_run,
                'signals': [
                    {'ts_code': s.ts_code, 'name': s.name, 'signal': s.signal,
                     'strategy': s.strategy, 'reason': s.reason,
                     'weight': s.weight, 'price': s.price}
                    for s in deduped_signals
                ],
                'trades': all_trades,
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            return {
                'status': 'error',
                'error': str(e),
                'signals': [],
                'trades': [],
            }

    # ============================================================
    # 生命周期
    # ============================================================

    def start(self, background: bool = True):
        """
        启动实盘服务

        Args:
            background: 是否后台运行（定时扫描）
        """
        if self.running:
            return {'status': 'error', 'message': '服务已在运行中'}

        self.running = True

        # 立即执行一次扫描
        result = self.scan_and_trade()

        if background:
            # 启动后台扫描线程
            interval = self.config.get('scan', {}).get('interval_seconds', 60)
            self.scan_thread = threading.Thread(
                target=self._scan_loop,
                args=(interval,),
                daemon=True
            )
            self.scan_thread.start()
            print(f"[实盘] 后台扫描已启动，间隔: {interval}秒")

        return {
            'status': 'started',
            'initial_scan': result,
            'background': background,
        }

    def stop(self) -> dict:
        """停止实盘服务"""
        self.running = False
        if self.scan_thread and self.scan_thread.is_alive():
            self.scan_thread.join(timeout=5)

        self.broker.disconnect()
        self.risk_manager._save_state()
        return {'status': 'stopped'}

    def _scan_loop(self, interval: int):
        """后台扫描循环"""
        while self.running:
            try:
                time.sleep(interval)
                if not self.running:
                    break
                self.scan_and_trade()
            except Exception as e:
                print(f"[实盘] 扫描异常: {e}")
                time.sleep(60)  # 出错后等待1分钟再试

    # ============================================================
    # 工具方法
    # ============================================================

    def _fetch_market_data(self, stock_pool: list, start_date: str, end_date: str) -> dict:
        """获取行情数据（带缓存）"""
        cache_filename = f'live_market_{end_date}_{len(stock_pool)}stocks'
        market_data = self.cache.load_market_data(cache_filename)

        if market_data and isinstance(market_data, dict) and len(market_data) > 0:
            return market_data

        market_data = self.fetcher.build_market_data_by_date(stock_pool, start_date, end_date)

        if market_data and len(market_data) > 0:
            self.cache.save_market_data(market_data, cache_filename)

        return market_data

    def _get_stock_info(self, ts_code: str) -> dict:
        """获取单只股票信息"""
        try:
            info = self.fetcher.get_stock_info(ts_code)
            return info
        except Exception:
            return {'name': ts_code, 'close': 0, 'industry': '未知'}

    def update_market_prices(self):
        """更新持仓市值（根据最新行情）"""
        stock_pool = list(self.broker.get_positions().keys())
        if not stock_pool:
            return

        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=5)).strftime('%Y%m%d')

        market_data = self._fetch_market_data(stock_pool, start_date, end_date)
        if not market_data:
            return

        latest_date = sorted(market_data.keys())[-1]
        latest_data = market_data[latest_date]

        # 更新 broker 中的持仓价格
        if hasattr(self.broker, 'update_market_prices'):
            self.broker.update_market_prices(latest_data)


# ============================================================
# 命令行入口
# ============================================================

def main():
    """命令行启动实盘服务"""
    import argparse

    parser = argparse.ArgumentParser(description='A股量化实盘交易服务')
    parser.add_argument('--mode', choices=['auto', 'semi'], default='semi',
                        help='交易模式（auto=全自动, semi=半自动）')
    parser.add_argument('--broker', choices=['sim', 'qmt', 'ths'], default='sim',
                        help='券商（sim=模拟盘, qmt=迅投QMT, ths=同花顺）')
    parser.add_argument('--interval', type=int, default=60,
                        help='扫描间隔（秒），默认60秒')
    parser.add_argument('--oneshot', action='store_true',
                        help='单次扫描模式（不循环）')
    parser.add_argument('--reset', action='store_true',
                        help='重置模拟账户')

    args = parser.parse_args()

    # 构建配置
    config = dict(LIVE_TRADING_CONFIG)
    config['mode'] = args.mode
    config['broker'] = args.broker
    config.setdefault('scan', {})['interval_seconds'] = args.interval

    # 创建服务
    server = LiveTradingServer(config)

    # 重置账户
    if args.reset and args.broker == 'sim':
        if hasattr(server.broker, 'reset_account'):
            server.broker.reset_account()

    # 单次扫描
    if args.oneshot:
        print("\n执行单次扫描...")
        result = server.scan_and_trade()
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return

    # 持续运行
    print("\n" + "=" * 60)
    print("A股量化实盘交易服务")
    print(f"券商: {args.broker} | 模式: {args.mode} | 扫描间隔: {args.interval}秒")
    print("=" * 60)
    print("\n按 Ctrl+C 停止服务\n")

    try:
        result = server.start(background=False)
        # 手动循环（前台模式）
        while server.running:
            time.sleep(args.interval)
            server.scan_and_trade()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
        server.stop()
        print("服务已停止")


if __name__ == '__main__':
    main()
