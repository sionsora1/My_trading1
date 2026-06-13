"""
风控管理器
在每笔订单提交前进行多重校验，确保交易安全
"""

from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import json
import os

from .base import (
    OrderRequest, OrderResult, OrderSide, Signal,
    DailyRiskLimit, PositionInfo, AccountInfo
)


@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    reason: str = ''
    severity: str = 'INFO'       # INFO / WARNING / BLOCK
    requires_confirm: bool = False


@dataclass
class RiskState:
    """风控状态记录"""
    date: str = ''                    # 日期
    starting_equity: float = 0        # 当日初始权益
    current_equity: float = 0         # 当前权益
    daily_loss: float = 0             # 当日亏损
    daily_loss_rate: float = 0        # 当日亏损率
    daily_trade_count: int = 0        # 当日交易次数
    daily_buy_amount: float = 0       # 当日买入金额
    max_daily_loss_triggered: bool = False  # 是否触发日亏损限制
    trading_halted: bool = False      # 是否已暂停交易
    # 新增：连续亏损 & 最大回撤追踪
    peak_equity: float = 0            # 权益历史峰值
    consecutive_loss_days: int = 0    # 连续亏损天数
    max_consecutive_loss_days: int = 5  # 连续亏损上限
    max_drawdown_rate: float = -0.15  # 最大回撤限制（相对峰值）


class RiskManager:
    """
    风控管理器

    在下单前逐层检查：
    1. 交易时间检查（A股交易时段）
    2. 标的风控（ST/退市/黑名单）
    3. 仓位风控（单只上限/总持仓上限）
    4. 资金风控（可用资金/单笔上限）
    5. 亏损风控（单日最大亏损）
    6. 大额订单人工确认
    """

    def __init__(self, config: DailyRiskLimit = None):
        self.config = config or DailyRiskLimit()
        self.state = RiskState()
        self.state_file = './data_cache/risk_state.json'

        # 获取当日日期
        today = datetime.now().strftime('%Y%m%d')
        self.state.date = today

        # 尝试加载之前的状态
        self._load_state()

        # ST/退市股票关键词黑名单（会自动扩展）
        self.auto_blacklist_keywords = ['ST', '*ST', '退市', 'SST', 'S*ST']

    def check_order(
        self,
        order: OrderRequest,
        account: AccountInfo,
        positions: Dict[str, PositionInfo],
        stock_info: dict = None
    ) -> RiskCheckResult:
        """
        对订单进行完整的风控检查

        Args:
            order: 下单请求
            account: 当前账户信息
            positions: 当前持仓
            stock_info: 股票信息（名称/行业/ST标记等）

        Returns:
            RiskCheckResult: 检查结果
        """
        # 1. 交易暂停检查
        if self.state.trading_halted:
            return RiskCheckResult(
                passed=False,
                reason='今日交易已暂停（触发日亏损限制）',
                severity='BLOCK'
            )

        # 2. 标的风控
        check = self._check_symbol(order, stock_info)
        if not check.passed:
            return check

        # 3. 仓位风控
        check = self._check_position(order, account, positions)
        if not check.passed:
            return check

        # 4. 资金风控
        check = self._check_capital(order, account)
        if not check.passed:
            return check

        # 5. 亏损风控
        check = self._check_daily_loss(order, account)
        if not check.passed:
            return check

        # 5b. 连续亏损检查
        check = self._check_consecutive_losses(order, account)
        if not check.passed:
            return check

        # 5c. 最大回撤检查
        check = self._check_max_drawdown(order, account)
        if not check.passed:
            return check

        # 6. 大额订单确认
        order_amount = order.price * order.quantity if order.price > 0 else account.total_assets * 0.05
        if (self.config.require_confirm_large and
                order_amount >= self.config.large_order_threshold):
            return RiskCheckResult(
                passed=True,
                reason=f'大额订单（{order_amount:,.0f}元），需人工确认',
                severity='WARNING',
                requires_confirm=True
            )

        return RiskCheckResult(passed=True, reason='风控检查通过')

    def check_signal(self, signal: Signal, account: AccountInfo,
                     positions: Dict[str, PositionInfo],
                     stock_info: dict = None) -> RiskCheckResult:
        """检查策略信号是否可以执行"""
        order = OrderRequest(
            ts_code=signal.ts_code,
            side=OrderSide.BUY if signal.signal == 'BUY' else OrderSide.SELL,
            quantity=100,  # 占位，实际数量由 broker 决定
            reason=signal.reason
        )
        return self.check_order(order, account, positions, stock_info)

    def _check_symbol(self, order: OrderRequest, stock_info: dict = None) -> RiskCheckResult:
        """标的检查"""
        code = order.ts_code

        # 黑名单检查
        if code in self.config.blacklist:
            return RiskCheckResult(
                passed=False,
                reason=f'股票 {code} 在黑名单中',
                severity='BLOCK'
            )

        # ST/退市检查
        if stock_info:
            name = stock_info.get('name', '')
            for keyword in self.auto_blacklist_keywords:
                if keyword.upper() in name.upper():
                    return RiskCheckResult(
                        passed=False,
                        reason=f'股票 {code} {name} 为{keyword}，禁止交易',
                        severity='BLOCK'
                    )

            # st_flag 检查
            if stock_info.get('st_flag', False):
                return RiskCheckResult(
                    passed=False,
                    reason=f'股票 {code} 标记为ST/退市风险',
                    severity='BLOCK'
                )

        return RiskCheckResult(passed=True)

    def _check_position(self, order: OrderRequest, account: AccountInfo,
                       positions: Dict[str, PositionInfo]) -> RiskCheckResult:
        """仓位检查"""
        if order.side == OrderSide.BUY:
            # 检查总持仓上限
            current_count = len(positions)
            if current_count >= self.config.max_total_positions:
                # 如果已经在持仓中（加仓），则允许
                if order.ts_code not in positions:
                    return RiskCheckResult(
                        passed=False,
                        reason=f'持仓数已达上限（{self.config.max_total_positions}只）',
                        severity='BLOCK'
                    )

            # 检查单只仓位上限
            if order.ts_code in positions:
                pos = positions[order.ts_code]
                new_weight = (pos.market_value + order.price * order.quantity) / account.total_assets
            else:
                new_weight = (order.price * order.quantity) / account.total_assets

            if new_weight > self.config.max_single_position_weight:
                return RiskCheckResult(
                    passed=False,
                    reason=f'单只仓位({new_weight:.1%})超过上限({self.config.max_single_position_weight:.1%})',
                    severity='BLOCK'
                )

        elif order.side == OrderSide.SELL:
            # 检查是否有可用持仓
            if order.ts_code not in positions:
                return RiskCheckResult(
                    passed=False,
                    reason=f'不持有 {order.ts_code}，无法卖出',
                    severity='BLOCK'
                )
            pos = positions[order.ts_code]
            if pos.available_quantity < order.quantity:
                return RiskCheckResult(
                    passed=False,
                    reason=f'{order.ts_code} 可卖数量({pos.available_quantity})不足',
                    severity='BLOCK'
                )

        return RiskCheckResult(passed=True)

    def _check_capital(self, order: OrderRequest, account: AccountInfo) -> RiskCheckResult:
        """资金检查"""
        if order.side == OrderSide.BUY:
            # 估算成本（含佣金、印花税预留）
            estimated_cost = order.price * order.quantity * 1.001 if order.price > 0 else 0

            if estimated_cost > account.available_cash:
                return RiskCheckResult(
                    passed=False,
                    reason=f'可用资金不足（需要{estimated_cost:,.0f}，可用{account.available_cash:,.0f}）',
                    severity='BLOCK'
                )

            # 单笔上限
            if estimated_cost > self.config.max_single_order_amount:
                return RiskCheckResult(
                    passed=False,
                    reason=f'单笔金额({estimated_cost:,.0f})超过上限({self.config.max_single_order_amount:,.0f})',
                    severity='BLOCK'
                )

        return RiskCheckResult(passed=True)

    def _check_daily_loss(self, order: OrderRequest, account: AccountInfo) -> RiskCheckResult:
        """日亏损检查"""
        if self.state.starting_equity <= 0:
            # 还未设置初始值，跳过
            return RiskCheckResult(passed=True)

        current_daily_rate = self.state.daily_loss_rate

        if current_daily_rate <= -abs(self.config.max_daily_loss_rate):
            self.state.max_daily_loss_triggered = True
            self.state.trading_halted = True
            self._save_state()
            return RiskCheckResult(
                passed=False,
                reason=f'触发日亏损限制（当日亏损{current_daily_rate:.2%} ≥ {self.config.max_daily_loss_rate:.2%}），交易暂停',
                severity='BLOCK'
            )

        return RiskCheckResult(passed=True)

    def _check_consecutive_losses(self, order: OrderRequest, account: AccountInfo) -> RiskCheckResult:
        """连续亏损检查 — 连续N天亏损则暂停交易"""
        if self.state.consecutive_loss_days >= self.state.max_consecutive_loss_days:
            self.state.trading_halted = True
            self._save_state()
            return RiskCheckResult(
                passed=False,
                reason=f'连续亏损{self.state.consecutive_loss_days}天（上限{self.state.max_consecutive_loss_days}天），交易暂停',
                severity='BLOCK'
            )
        return RiskCheckResult(passed=True)

    def _check_max_drawdown(self, order: OrderRequest, account: AccountInfo) -> RiskCheckResult:
        """最大回撤检查 — 从权益峰值回撤超限则暂停"""
        if self.state.peak_equity <= 0:
            return RiskCheckResult(passed=True)

        drawdown = (account.total_assets - self.state.peak_equity) / self.state.peak_equity
        if drawdown <= self.state.max_drawdown_rate:
            self.state.trading_halted = True
            self._save_state()
            return RiskCheckResult(
                passed=False,
                reason=f'触发最大回撤限制（回撤{drawdown:.1%} ≥ {self.state.max_drawdown_rate:.1%}），交易暂停',
                severity='BLOCK'
            )
        return RiskCheckResult(passed=True)

    def update_daily_state(self, account: AccountInfo):
        """更新每日风控状态（含连续亏损和最大回撤追踪）"""
        today = datetime.now().strftime('%Y%m%d')
        if today != self.state.date:
            # 跨日：判断昨天是否亏损，更新连续亏损计数
            yesterday_loss = self.state.daily_loss_rate < -0.005  # 亏>0.5%算亏损日

            # 保留需要跨日的字段
            old_peak = max(self.state.peak_equity, account.total_assets)
            old_consecutive = self.state.consecutive_loss_days
            old_max_dd = self.state.max_drawdown_rate
            old_max_consec = self.state.max_consecutive_loss_days

            if yesterday_loss:
                old_consecutive += 1
            else:
                old_consecutive = 0  # 有盈利日则重置

            self.state = RiskState(
                date=today,
                starting_equity=account.total_assets,
                current_equity=account.total_assets,
                peak_equity=old_peak,
                consecutive_loss_days=old_consecutive,
                max_consecutive_loss_days=old_max_consec,
                max_drawdown_rate=old_max_dd,
            )
            self._save_state()
            return

        # 设置初始权益
        if self.state.starting_equity <= 0:
            self.state.starting_equity = account.total_assets

        # 更新峰值权益
        if account.total_assets > self.state.peak_equity:
            self.state.peak_equity = account.total_assets

        # 更新当前状态
        self.state.current_equity = account.total_assets
        self.state.daily_loss = account.total_assets - self.state.starting_equity
        self.state.daily_loss_rate = (
            self.state.daily_loss / self.state.starting_equity
            if self.state.starting_equity > 0 else 0
        )
        self._save_state()

    def record_trade(self, amount: float):
        """记录一笔交易"""
        self.state.daily_trade_count += 1
        if amount > 0:
            self.state.daily_buy_amount += amount
        self._save_state()

    def get_status(self) -> dict:
        """获取当前风控状态"""
        return {
            'date': self.state.date,
            'starting_equity': self.state.starting_equity,
            'current_equity': self.state.current_equity,
            'daily_loss': self.state.daily_loss,
            'daily_loss_rate': self.state.daily_loss_rate,
            'daily_trade_count': self.state.daily_trade_count,
            'daily_buy_amount': self.state.daily_buy_amount,
            'max_daily_loss_triggered': self.state.max_daily_loss_triggered,
            'trading_halted': self.state.trading_halted,
            'max_daily_loss_limit': self.config.max_daily_loss_rate,
            'peak_equity': self.state.peak_equity,
            'consecutive_loss_days': self.state.consecutive_loss_days,
            'max_consecutive_loss_days': self.state.max_consecutive_loss_days,
            'max_drawdown_rate': self.state.max_drawdown_rate,
        }

    def reset_daily_state(self):
        """手动重置日风控状态"""
        today = datetime.now().strftime('%Y%m%d')
        self.state = RiskState(date=today)
        self._save_state()

    def add_to_blacklist(self, code: str):
        """添加股票到黑名单"""
        if code not in self.config.blacklist:
            self.config.blacklist.append(code)

    def remove_from_blacklist(self, code: str):
        """从黑名单移除"""
        if code in self.config.blacklist:
            self.config.blacklist.remove(code)

    def _save_state(self):
        """保存风控状态到文件"""
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            state_dict = {
                'date': self.state.date,
                'starting_equity': self.state.starting_equity,
                'current_equity': self.state.current_equity,
                'daily_loss': self.state.daily_loss,
                'daily_loss_rate': self.state.daily_loss_rate,
                'daily_trade_count': self.state.daily_trade_count,
                'daily_buy_amount': self.state.daily_buy_amount,
                'max_daily_loss_triggered': self.state.max_daily_loss_triggered,
                'trading_halted': self.state.trading_halted,
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state_dict, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _load_state(self):
        """从文件加载风控状态"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                today = datetime.now().strftime('%Y%m%d')
                if data.get('date') == today:
                    self.state.date = data.get('date', '')
                    self.state.starting_equity = data.get('starting_equity', 0)
                    self.state.current_equity = data.get('current_equity', 0)
                    self.state.daily_loss = data.get('daily_loss', 0)
                    self.state.daily_loss_rate = data.get('daily_loss_rate', 0)
                    self.state.daily_trade_count = data.get('daily_trade_count', 0)
                    self.state.daily_buy_amount = data.get('daily_buy_amount', 0)
                    self.state.max_daily_loss_triggered = data.get('max_daily_loss_triggered', False)
                    self.state.trading_halted = data.get('trading_halted', False)
        except Exception:
            pass
