"""
信号过滤器
对交易信号进行多维度过滤，返回 (通过, 原因) 元组
"""

from typing import Tuple, List, Optional


class SignalFilters:
    """
    信号过滤器

    在信号进入执行队列前逐层检查：
    1. 日亏损检查
    2. 单只仓位权重检查
    3. 持仓数量检查
    4. 单笔金额检查
    5. 黑名单检查
    6. 涨跌停检查
    7. 最小金额检查

    每个方法返回 Tuple[bool, str]：通过标志 + 原因说明
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.max_daily_loss_rate = config.get('max_daily_loss_rate', 0.02)
        self.max_single_position_weight = config.get('max_single_position_weight', 0.22)
        self.max_total_positions = config.get('max_total_positions', 5)
        self.max_single_order_amount = config.get('max_single_order_amount', 25000)
        self.min_order_amount = config.get('min_order_amount', 2000)
        self._blacklist: List[str] = config.get('blacklist', [])

    # ============================================================
    # 核心检查方法
    # ============================================================

    def check_daily_loss(self, daily_pnl: float, total_assets: float) -> Tuple[bool, str]:
        """
        日亏损检查

        Args:
            daily_pnl: 当日盈亏金额（负数为亏损）
            total_assets: 总资产

        Returns:
            (passed, reason)
        """
        if daily_pnl >= 0:
            return True, ''

        loss_rate = abs(daily_pnl) / total_assets if total_assets > 0 else 0
        if loss_rate > self.max_daily_loss_rate:
            return False, (
                f'日亏损({loss_rate:.2%})超过上限'
                f'({self.max_daily_loss_rate:.2%})'
            )
        return True, ''

    def check_position_weight(
        self,
        ts_code: str,
        suggest_amount: float,
        total_assets: float
    ) -> Tuple[bool, str]:
        """
        单只仓位权重检查

        Args:
            ts_code: 股票代码
            suggest_amount: 建议买入金额
            total_assets: 总资产

        Returns:
            (passed, reason)
        """
        if total_assets <= 0:
            return False, '总资产无效'

        weight = suggest_amount / total_assets
        if weight > self.max_single_position_weight:
            return False, (
                f'{ts_code} 建议仓位({weight:.1%})超过'
                f'单只上限({self.max_single_position_weight:.1%})'
            )
        return True, ''

    def check_position_count(
        self,
        current_count: int,
        ts_code: str,
        position_codes: List[str]
    ) -> Tuple[bool, str]:
        """
        持仓数量检查

        如果持仓已满，只有加仓已有持仓才允许通过。

        Args:
            current_count: 当前持仓数量
            ts_code: 待买入的股票代码
            position_codes: 当前持仓的股票代码列表

        Returns:
            (passed, reason)
        """
        if current_count >= self.max_total_positions:
            if ts_code not in position_codes:
                return False, (
                    f'持仓数已达上限({self.max_total_positions}只)，'
                    f'无法新买入 {ts_code}'
                )
        return True, ''

    def check_order_amount(self, amount: float) -> Tuple[bool, str]:
        """
        单笔金额检查

        Args:
            amount: 订单金额

        Returns:
            (passed, reason)
        """
        if amount > self.max_single_order_amount:
            return False, (
                f'单笔金额({amount:,.0f})超过上限'
                f'({self.max_single_order_amount:,.0f})'
            )
        return True, ''

    def check_blacklist(self, ts_code: str) -> Tuple[bool, str]:
        """
        黑名单检查

        Args:
            ts_code: 股票代码

        Returns:
            (passed, reason)
        """
        if ts_code in self._blacklist:
            return False, f'{ts_code} 在黑名单中'
        return True, ''

    def check_limit_up_down(
        self,
        stock: dict,
        direction: str
    ) -> Tuple[bool, str]:
        """
        涨跌停检查

        - 买入时，若股票涨跌幅 >= 9.9%（接近涨停），返回失败
        - 卖出时，若股票涨跌幅 <= -9.9%（接近跌停），返回失败

        Args:
            stock: 股票行情数据，需包含 pct_chg 字段
            direction: 'BUY' 或 'SELL'

        Returns:
            (passed, reason)
        """
        if not stock:
            return True, ''  # 没有行情数据时跳过

        pct_chg = stock.get('pct_chg', 0)
        ts_code = stock.get('ts_code', '') or stock.get('code', '')

        if direction == 'BUY' and pct_chg >= 9.9:
            return False, f'{ts_code} 接近涨停({pct_chg:.1f}%)，暂停买入'
        if direction == 'SELL' and pct_chg <= -9.9:
            return False, f'{ts_code} 接近跌停({pct_chg:.1f}%)，暂停卖出'

        return True, ''

    def check_min_amount(
        self,
        amount: float,
        min_amount: float = None
    ) -> Tuple[bool, str]:
        """
        最小金额检查

        Args:
            amount: 订单金额
            min_amount: 最小金额阈值，默认使用实例配置

        Returns:
            (passed, reason)
        """
        threshold = min_amount if min_amount is not None else self.min_order_amount
        if amount < threshold:
            return False, (
                f'订单金额({amount:,.0f})低于最低限额'
                f'({threshold:,.0f})'
            )
        return True, ''

    def run_all_checks(
        self,
        ts_code: str,
        direction: str,
        suggest_amount: float,
        total_assets: float,
        daily_pnl: float,
        position_count: int,
        position_codes: List[str],
        stock_info: dict = None
    ) -> Tuple[bool, str]:
        """
        运行所有过滤器检查

        Args:
            ts_code: 股票代码
            direction: 买卖方向 'BUY' / 'SELL'
            suggest_amount: 建议金额
            total_assets: 总资产
            daily_pnl: 当日盈亏
            position_count: 当前持仓数
            position_codes: 当前持仓代码列表
            stock_info: 股票行情数据（用于涨跌停检查）

        Returns:
            (passed, reason) — 首个未通过的检查原因，或全部通过
        """
        # 1. 日亏损检查
        passed, reason = self.check_daily_loss(daily_pnl, total_assets)
        if not passed:
            return False, reason

        # 2. 黑名单检查
        passed, reason = self.check_blacklist(ts_code)
        if not passed:
            return False, reason

        # 3. 涨跌停检查
        passed, reason = self.check_limit_up_down(stock_info, direction)
        if not passed:
            return False, reason

        # 4. 仅买入做以下检查
        if direction == 'BUY':
            passed, reason = self.check_position_count(
                position_count, ts_code, position_codes
            )
            if not passed:
                return False, reason

            passed, reason = self.check_position_weight(
                ts_code, suggest_amount, total_assets
            )
            if not passed:
                return False, reason

            passed, reason = self.check_order_amount(suggest_amount)
            if not passed:
                return False, reason

            passed, reason = self.check_min_amount(suggest_amount)
            if not passed:
                return False, reason

        return True, '全部检查通过'

    # ============================================================
    # 黑名单管理
    # ============================================================

    def add_to_blacklist(self, ts_code: str):
        """添加股票到黑名单"""
        if ts_code not in self._blacklist:
            self._blacklist.append(ts_code)

    def remove_from_blacklist(self, ts_code: str):
        """从黑名单移除股票"""
        if ts_code in self._blacklist:
            self._blacklist.remove(ts_code)

    def get_blacklist(self) -> List[str]:
        """获取当前黑名单列表"""
        return list(self._blacklist)

    def is_blacklisted(self, ts_code: str) -> bool:
        """检查股票是否在黑名单中"""
        return ts_code in self._blacklist


# ============================================================
# 快速验证
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("SignalFilters 验证")
    print("=" * 60)

    filters = SignalFilters()

    # 测试 check_daily_loss
    passed, reason = filters.check_daily_loss(-1500, 100000)
    print(f"\n1. check_daily_loss(-1500, 100000): passed={passed}, reason='{reason}'")
    assert passed, "1.5% loss within 2% limit should pass"

    passed, reason = filters.check_daily_loss(-5000, 100000)
    print(f"2. check_daily_loss(-5000, 100000): passed={passed}, reason='{reason}'")
    assert not passed, "5% loss should fail (max 2%)"

    # 测试 check_position_weight
    passed, reason = filters.check_position_weight('000001', 20000, 100000)
    print(f"\n3. check_position_weight('000001', 20000, 100000): passed={passed}, reason='{reason}'")
    assert passed, "20% weight within 22% limit should pass"

    passed, reason = filters.check_position_weight('000002', 25000, 100000)
    print(f"4. check_position_weight('000002', 25000, 100000): passed={passed}, reason='{reason}'")
    assert not passed, "25% weight should fail (max 22%)"

    # 测试 check_position_count
    passed, reason = filters.check_position_count(5, '000003', ['000001', '000002'])
    print(f"\n5. check_position_count(5, '000003', [...2 codes]): passed={passed}, reason='{reason}'")
    assert not passed, "At max positions, new code should fail"

    passed, reason = filters.check_position_count(5, '000001', ['000001', '000002'])
    print(f"6. check_position_count(5, '000001', [...2 codes]): passed={passed}, reason='{reason}'")
    assert passed, "At max positions, existing code (add) should pass"

    # 测试 check_order_amount
    passed, reason = filters.check_order_amount(30000)
    print(f"\n7. check_order_amount(30000): passed={passed}, reason='{reason}'")
    assert not passed, "30k order should fail (max 25k)"

    passed, reason = filters.check_order_amount(20000)
    print(f"8. check_order_amount(20000): passed={passed}, reason='{reason}'")
    assert passed, "20k order within 25k limit should pass"

    # 测试 check_blacklist
    filters.add_to_blacklist('600000')
    passed, reason = filters.check_blacklist('600000')
    print(f"\n9. check_blacklist('600000') after add: passed={passed}, reason='{reason}'")
    assert not passed, "Blacklisted stock should fail"

    passed, reason = filters.check_blacklist('000001')
    print(f"10. check_blacklist('000001'): passed={passed}, reason='{reason}'")
    assert passed, "Non-blacklisted stock should pass"

    filters.remove_from_blacklist('600000')
    passed, reason = filters.check_blacklist('600000')
    print(f"11. check_blacklist('600000') after remove: passed={passed}, reason='{reason}'")
    assert passed, "Removed from blacklist should pass"

    # 测试 check_limit_up_down
    passed, reason = filters.check_limit_up_down({'pct_chg': 9.95}, 'BUY')
    print(f"\n12. check_limit_up_down(pct_chg=9.95, BUY): passed={passed}, reason='{reason}'")
    assert not passed, "BUY at 9.95% should fail (near limit-up)"

    passed, reason = filters.check_limit_up_down({'pct_chg': -9.95}, 'SELL')
    print(f"13. check_limit_up_down(pct_chg=-9.95, SELL): passed={passed}, reason='{reason}'")
    assert not passed, "SELL at -9.95% should fail (near limit-down)"

    passed, reason = filters.check_limit_up_down({'pct_chg': 5.0}, 'BUY')
    print(f"14. check_limit_up_down(pct_chg=5.0, BUY): passed={passed}, reason='{reason}'")
    assert passed, "BUY at 5% should pass"

    # 测试 check_min_amount
    passed, reason = filters.check_min_amount(1500)
    print(f"\n15. check_min_amount(1500): passed={passed}, reason='{reason}'")
    assert not passed, "1500 should fail (min 2000)"

    passed, reason = filters.check_min_amount(5000)
    print(f"16. check_min_amount(5000): passed={passed}, reason='{reason}'")
    assert passed, "5000 should pass"

    # 测试 run_all_checks (全通过场景)
    passed, reason = filters.run_all_checks(
        ts_code='000001',
        direction='BUY',
        suggest_amount=10000,
        total_assets=100000,
        daily_pnl=1000,
        position_count=2,
        position_codes=['000001', '000002'],
        stock_info={'pct_chg': 3.0}
    )
    print(f"\n17. run_all_checks(正常买入): passed={passed}, reason='{reason}'")
    assert passed, "All-checks normal should pass"

    # 测试 run_all_checks (大额场景)
    passed, reason = filters.run_all_checks(
        ts_code='000003',
        direction='BUY',
        suggest_amount=30000,
        total_assets=100000,
        daily_pnl=1000,
        position_count=2,
        position_codes=['000001', '000002'],
        stock_info={'pct_chg': 3.0}
    )
    print(f"18. run_all_checks(大额买入): passed={passed}, reason='{reason}'")
    assert not passed, "Large order should fail"

    print("\n" + "=" * 60)
    print("所有 SignalFilters 验证通过!")
    print("=" * 60)
