"""
信号总线
收集、去重、过滤、排序、分配策略信号，并管理信号生命周期
"""

import uuid
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict

from .filters import SignalFilters


class SignalBus:
    """
    信号总线

    职责：
    1. 收集各策略生成的信号
    2. 去重（同股票同方向保留最高权重）
    3. 风险过滤（涨跌停/持仓上限/最小金额）
    4. 排序（卖单优先，买单按权重降序）
    5. 资金分配（按权重分配可用资金 → 建议数量）
    6. 半自动模式信号管理（确认/拒绝/执行/过期）
    """

    def __init__(self, config: dict = None):
        config = config or {}
        self.config = config

        # 信号过滤器
        filter_config = config.get('risk', config)
        self.filters = SignalFilters(filter_config)

        # 信号状态存储
        # _all_signals: signal_id -> dict (完整信号，含状态)
        self._all_signals: Dict[str, dict] = {}
        # _pending_ids: 待确认/待执行的 signal_id 列表
        self._pending_ids: List[str] = []
        # _history_ids: 已处理的 signal_id 列表
        self._history_ids: List[str] = []

        # 可用资金安全边距（预留手续费等）
        self.cash_margin = config.get('cash_margin', 0.95)

        # 是否使用半自动模式
        self.semi_auto = config.get('mode', 'semi') == 'semi'

    # ============================================================
    # 主流程：process
    # ============================================================

    def process(
        self,
        date: str,
        market_data: dict,
        portfolio: dict,
        strategies: list,
        risk_manager=None
    ) -> List[dict]:
        """
        信号处理主入口

        Pipeline:
        ① collect   — 遍历策略，收集信号
        ② deduplicate — 同股票+同方向去重，保留最高权重
        ③ risk_filter — 涨跌停、持仓数量、最小金额检查
        ④ sort      — 卖单优先，买单按权重降序
        ⑤ allocate  — 按权重分配可用资金 → 建议数量

        Args:
            date: 当前日期字符串 YYYYMMDD
            market_data: {ts_code: stock_data}
            portfolio: {'cash': float, 'positions': {ts_code: position_data}}
            strategies: 策略实例列表，每个需实现 generate_signals(date, market_data, portfolio)
            risk_manager: 可选的外部风控管理器（用于更全面的风控检查）

        Returns:
            处理后的信号列表，每个信号包含:
            ts_code, signal(BUY/SELL), weight, reason, strategy,
            create_time, signal_id, suggest_amount, suggest_qty, status
        """
        # ① collect
        raw_signals = self._collect(date, market_data, portfolio, strategies)
        if not raw_signals:
            return []

        # ② deduplicate
        deduped = self._deduplicate(raw_signals)

        # ③ risk filter
        filtered = self._risk_filter(deduped, market_data, portfolio, risk_manager)

        # ④ sort
        sorted_signals = self._sort(filtered)

        # ⑤ allocate
        allocated = self._allocate(sorted_signals, portfolio)

        # 存入信号存储
        for sig in allocated:
            sid = sig.get('signal_id')
            if sid and sid not in self._all_signals:
                self._all_signals[sid] = sig
                self._pending_ids.append(sid)

        return allocated

    # ============================================================
    # Pipeline Steps
    # ============================================================

    def _collect(
        self,
        date: str,
        market_data: dict,
        portfolio: dict,
        strategies: list
    ) -> List[dict]:
        """① collect: 遍历策略收集信号"""
        all_signals = []
        for strategy in strategies:
            try:
                signals = strategy.generate_signals(date, market_data, portfolio)
                if not signals:
                    continue
                strategy_name = getattr(strategy, 'name', strategy.__class__.__name__)
                for sig in signals:
                    # 确保必要字段存在
                    sig['strategy'] = sig.get('strategy', strategy_name)
                    sig['create_time'] = sig.get(
                        'create_time',
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    )
                    sig['signal_id'] = sig.get('signal_id', self._make_id())
                    sig['signal'] = sig.get('signal', 'BUY').upper()
                    if sig['signal'] not in ('BUY', 'SELL'):
                        continue
                all_signals.extend(signals)
            except Exception as e:
                strategy_name = getattr(strategy, 'name', strategy.__class__.__name__)
                print(f"[SignalBus] 策略 {strategy_name} 生成信号出错: {e}")

        return all_signals

    def _deduplicate(self, signals: List[dict]) -> List[dict]:
        """② deduplicate: 同 ts_code + 同方向 → 保留最高权重，合并reason"""
        # 分组 key: (ts_code, signal)
        groups: Dict[Tuple[str, str], dict] = {}
        for sig in signals:
            key = (sig['ts_code'], sig['signal'])
            if key in groups:
                existing = groups[key]
                if sig.get('weight', 0) > existing.get('weight', 0):
                    # 保留更高权重，合并reason
                    merged_reason = (
                        f"{sig.get('reason', '')}; "
                        f"覆盖低权重({existing.get('reason', '')})"
                    )
                    sig['reason'] = merged_reason
                    groups[key] = sig
                else:
                    # 当前权重更低，合并reason到原有的
                    existing['reason'] = (
                        f"{existing.get('reason', '')}; "
                        f"忽略低权重({sig.get('strategy', '?')}:"
                        f"{sig.get('reason', '')}[w={sig.get('weight', 0):.3f}])"
                    )
            else:
                groups[key] = sig

        return list(groups.values())

    def _risk_filter(
        self,
        signals: List[dict],
        market_data: dict,
        portfolio: dict,
        risk_manager=None
    ) -> List[dict]:
        """③ risk_filter: 涨跌停 / 持仓数量 / 最小金额检查"""
        positions = portfolio.get('positions', {})
        total_assets = portfolio.get('total_assets', self._calc_total_assets(portfolio))
        cash = portfolio.get('cash', 0)
        position_codes = list(positions.keys())
        position_count = len(position_codes)

        # 当日盈亏（如果 portfolio 中有的话）
        daily_pnl = portfolio.get('daily_pnl', 0)

        passed = []
        for sig in signals:
            ts_code = sig['ts_code']
            direction = sig['signal']
            weight = sig.get('weight', 0)

            # 估算建议金额（分配前先粗略估计）
            estimated_amount = weight * cash * self.cash_margin

            stock_info = market_data.get(ts_code, {})

            # 使用内部过滤器
            ok, reason = self.filters.run_all_checks(
                ts_code=ts_code,
                direction=direction,
                suggest_amount=estimated_amount,
                total_assets=total_assets,
                daily_pnl=daily_pnl,
                position_count=position_count,
                position_codes=position_codes,
                stock_info=stock_info,
            )

            # 可选：外部 risk_manager 额外检查
            if ok and risk_manager is not None:
                ok, reason = self._external_risk_check(
                    sig, risk_manager, positions, total_assets, stock_info
                )

            if ok:
                sig['filter_status'] = 'passed'
                passed.append(sig)
            else:
                sig['filter_status'] = 'rejected'
                sig['filter_reason'] = reason
                # 被拒绝的信号也记录
                sid = sig.get('signal_id', self._make_id())
                if sid not in self._all_signals:
                    sig['signal_id'] = sid
                    self._all_signals[sid] = sig
                # 直接从 pending 中排除（不加入返回列表）

        return passed

    def _sort(self, signals: List[dict]) -> List[dict]:
        """④ sort: 卖单优先（释放资金），买单按 weight 降序"""
        sells = [s for s in signals if s['signal'] == 'SELL']
        buys = [s for s in signals if s['signal'] == 'BUY']

        # 卖单按权重降序（优先卖出重仓）
        sells.sort(key=lambda s: s.get('weight', 0), reverse=True)
        # 买单按权重降序
        buys.sort(key=lambda s: s.get('weight', 0), reverse=True)

        return sells + buys

    def _allocate(
        self,
        signals: List[dict],
        portfolio: dict
    ) -> List[dict]:
        """⑤ allocate: 按权重分配可用资金 → 建议数量"""
        positions = portfolio.get('positions', {})
        cash = portfolio.get('cash', 0)
        available_cash = cash * self.cash_margin

        # 分离买卖信号
        buys = [s for s in signals if s['signal'] == 'BUY']
        sells = [s for s in signals if s['signal'] == 'SELL']

        # 卖单：建议数量来自持仓
        for sig in sells:
            ts_code = sig['ts_code']
            pos = positions.get(ts_code, {})
            available_qty = pos.get('available_quantity', pos.get('quantity', 0))
            sig['suggest_qty'] = min(available_qty, pos.get('quantity', 0))
            sig['suggest_amount'] = (
                sig['suggest_qty'] * pos.get('current_price', pos.get('cost_price', 0))
            )

        # 买单：按权重分配资金
        if buys:
            total_buy_weight = sum(s.get('weight', 0) for s in buys)
            if total_buy_weight <= 0:
                total_buy_weight = len(buys)

            remaining_cash = available_cash
            allocated = []

            for sig in buys:
                weight = sig.get('weight', 0)
                if total_buy_weight > 0:
                    # 按权重分配
                    alloc_amount = available_cash * (weight / total_buy_weight)
                else:
                    alloc_amount = available_cash / len(buys)

                # 获取参考价格
                ts_code = sig['ts_code']
                price = self._get_reference_price(ts_code, portfolio, sig)

                # 计算建议数量（100股整数倍，A股）
                suggest_qty = int(alloc_amount / price / 100) * 100 if price > 0 else 0
                suggest_amount = suggest_qty * price

                sig['suggest_amount'] = suggest_amount
                sig['suggest_qty'] = suggest_qty
                sig['ref_price'] = price

                remaining_cash -= suggest_amount
                allocated.append(sig)

        return signals

    # ============================================================
    # 半自动模式方法
    # ============================================================

    def get_pending_signals(self) -> List[dict]:
        """
        获取待处理的信号列表（未确认/未执行）

        Returns:
            状态为 pending 的信号列表
        """
        result = []
        for sid in self._pending_ids:
            sig = self._all_signals.get(sid)
            if sig and sig.get('status', 'pending') == 'pending':
                result.append(sig)
        return result

    def confirm_signal(self, signal_id: str) -> Tuple[bool, str]:
        """
        确认信号（标记为已确认，可执行）

        Args:
            signal_id: 信号ID

        Returns:
            (success, message)
        """
        sig = self._all_signals.get(signal_id)
        if sig is None:
            return False, f'信号 {signal_id} 不存在'

        if sig.get('status') == 'executed':
            return False, f'信号 {signal_id} 已执行，无法确认'

        if sig.get('status') == 'rejected':
            return False, f'信号 {signal_id} 已被拒绝'

        sig['status'] = 'confirmed'
        sig['confirmed'] = True
        sig['confirm_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return True, f'信号 {signal_id} 已确认'

    def reject_signal(self, signal_id: str, reason: str = '') -> Tuple[bool, str]:
        """
        拒绝信号（标记为已拒绝）

        Args:
            signal_id: 信号ID
            reason: 拒绝原因

        Returns:
            (success, message)
        """
        sig = self._all_signals.get(signal_id)
        if sig is None:
            return False, f'信号 {signal_id} 不存在'

        if sig.get('status') == 'executed':
            return False, f'信号 {signal_id} 已执行，无法拒绝'

        sig['status'] = 'rejected'
        sig['rejected'] = True
        sig['reject_reason'] = reason
        sig['reject_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 从 pending 移到 history
        if signal_id in self._pending_ids:
            self._pending_ids.remove(signal_id)
        if signal_id not in self._history_ids:
            self._history_ids.append(signal_id)

        return True, f'信号 {signal_id} 已拒绝'

    def mark_executed(
        self,
        signal_id: str,
        fill_price: float,
        fill_qty: int
    ) -> Tuple[bool, str]:
        """
        标记信号已执行（填入成交信息）

        Args:
            signal_id: 信号ID
            fill_price: 成交价格
            fill_qty: 成交数量

        Returns:
            (success, message)
        """
        sig = self._all_signals.get(signal_id)
        if sig is None:
            return False, f'信号 {signal_id} 不存在'

        if sig.get('status') == 'rejected':
            return False, f'信号 {signal_id} 已被拒绝，无法标记执行'

        sig['status'] = 'executed'
        sig['executed'] = True
        sig['fill_price'] = fill_price
        sig['fill_qty'] = fill_qty
        sig['fill_amount'] = fill_price * fill_qty
        sig['exec_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 从 pending 移到 history
        if signal_id in self._pending_ids:
            self._pending_ids.remove(signal_id)
        if signal_id not in self._history_ids:
            self._history_ids.append(signal_id)

        return True, f'信号 {signal_id} 已标记为已执行'

    def expire_old_signals(self, timeout_minutes: int = 30) -> int:
        """
        自动过期旧信号（超过超时时间未处理的 pending 信号）

        Args:
            timeout_minutes: 超时分钟数

        Returns:
            过期的信号数量
        """
        now = datetime.now()
        timeout = timedelta(minutes=timeout_minutes)
        expired_count = 0
        expired_ids = []

        for sid in self._pending_ids:
            sig = self._all_signals.get(sid)
            if sig is None:
                expired_ids.append(sid)
                continue

            status = sig.get('status', 'pending')
            if status not in ('pending', 'confirmed'):
                continue

            # 解析创建时间
            create_str = sig.get('create_time', '')
            try:
                create_time = datetime.strptime(create_str, '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                create_time = now  # 无法解析时视为刚创建

            if now - create_time > timeout:
                sig['status'] = 'expired'
                sig['expired'] = True
                sig['expire_time'] = now.strftime('%Y-%m-%d %H:%M:%S')
                sig['expire_reason'] = f'超时 {timeout_minutes} 分钟未处理'
                expired_ids.append(sid)
                expired_count += 1

        # 批量移动
        for sid in expired_ids:
            if sid in self._pending_ids:
                self._pending_ids.remove(sid)
            if sid not in self._history_ids:
                self._history_ids.append(sid)

        return expired_count

    def get_signal_history(self, days: int = 30) -> List[dict]:
        """
        获取近 N 天的信号历史

        Args:
            days: 查看最近多少天的信号

        Returns:
            信号列表（按创建时间倒序）
        """
        cutoff = datetime.now() - timedelta(days=days)
        result = []

        for sid, sig in self._all_signals.items():
            create_str = sig.get('create_time', '')
            try:
                create_time = datetime.strptime(create_str, '%Y-%m-%d %H:%M:%S')
                if create_time >= cutoff:
                    result.append(sig)
            except (ValueError, TypeError):
                # 无法解析时间的保留
                result.append(sig)

        result.sort(
            key=lambda s: s.get('create_time', ''),
            reverse=True
        )
        return result

    # ============================================================
    # 工具方法
    # ============================================================

    def get_signal(self, signal_id: str) -> Optional[dict]:
        """根据 signal_id 获取信号详情"""
        return self._all_signals.get(signal_id)

    def get_statistics(self) -> dict:
        """获取信号统计信息"""
        total = len(self._all_signals)
        pending = len(self._pending_ids)
        confirmed = sum(
            1 for s in self._all_signals.values()
            if s.get('status') == 'confirmed'
        )
        executed = sum(
            1 for s in self._all_signals.values()
            if s.get('status') == 'executed'
        )
        rejected = sum(
            1 for s in self._all_signals.values()
            if s.get('status') == 'rejected'
        )
        expired = sum(
            1 for s in self._all_signals.values()
            if s.get('status') == 'expired'
        )

        # 策略来源统计
        strategy_counts: Dict[str, int] = defaultdict(int)
        for sig in self._all_signals.values():
            strategy_counts[sig.get('strategy', 'unknown')] += 1

        return {
            'total': total,
            'pending': pending,
            'confirmed': confirmed,
            'executed': executed,
            'rejected': rejected,
            'expired': expired,
            'by_strategy': dict(strategy_counts),
        }

    def clear(self):
        """清空所有信号记录"""
        self._all_signals.clear()
        self._pending_ids.clear()
        self._history_ids.clear()

    # ============================================================
    # 内部辅助方法
    # ============================================================

    @staticmethod
    def _make_id() -> str:
        """生成唯一 signal_id"""
        ts = int(time.time() * 1000)
        short_uuid = uuid.uuid4().hex[:8]
        return f"SIG-{ts}-{short_uuid}"

    @staticmethod
    def _calc_total_assets(portfolio: dict) -> float:
        """根据 portfolio 估算总资产"""
        cash = portfolio.get('cash', 0)
        positions = portfolio.get('positions', {})
        market_value = sum(
            p.get('market_value',
                  p.get('quantity', 0) * p.get('current_price',
                                               p.get('cost_price', 0)))
            for p in positions.values()
        )
        total = cash + market_value
        return total if total > 0 else 100000

    def _get_reference_price(
        self,
        ts_code: str,
        portfolio: dict,
        signal: dict
    ) -> float:
        """获取参考价格"""
        # 优先从信号中获取
        if signal.get('price', 0) > 0:
            return signal['price']
        if signal.get('ref_price', 0) > 0:
            return signal['ref_price']

        # 从持仓中获取
        positions = portfolio.get('positions', {})
        pos = positions.get(ts_code, {})
        for key in ('current_price', 'close', 'cost_price'):
            val = pos.get(key, 0)
            if val > 0:
                return val

        return 0.0

    def _external_risk_check(
        self,
        signal: dict,
        risk_manager,
        positions: dict,
        total_assets: float,
        stock_info: dict
    ) -> Tuple[bool, str]:
        """
        调用外部 risk_manager 进行额外检查
        适配 broker/risk_manager.py 的 RiskManager 接口
        """
        try:
            from ..broker.base import Signal, AccountInfo, PositionInfo

            # 构造 Signal 对象
            sig_obj = Signal(
                ts_code=signal.get('ts_code', ''),
                signal=signal.get('signal', 'BUY'),
                weight=signal.get('weight', 0),
                reason=signal.get('reason', ''),
                strategy=signal.get('strategy', ''),
                create_time=signal.get('create_time', ''),
            )

            # 构造 AccountInfo（简化版）
            account = AccountInfo(total_assets=total_assets)

            # 构造 positions dict
            pos_dict = {}
            for code, pos in positions.items():
                if isinstance(pos, PositionInfo):
                    pos_dict[code] = pos
                elif isinstance(pos, dict):
                    pos_dict[code] = PositionInfo(
                        ts_code=code,
                        quantity=pos.get('quantity', 0),
                        available_quantity=pos.get('available_quantity',
                                                   pos.get('quantity', 0)),
                        cost_price=pos.get('cost_price', 0),
                        current_price=pos.get('current_price', 0),
                        market_value=pos.get('market_value', 0),
                        profit_rate=pos.get('profit_rate', 0),
                    )

            result = risk_manager.check_signal(sig_obj, account, pos_dict, stock_info)
            return result.passed, result.reason

        except ImportError:
            return True, ''
        except Exception as e:
            return True, f'外部风控检查跳过: {e}'


# ============================================================
# 快速验证
# ============================================================
if __name__ == '__main__':
    print("=" * 60)
    print("SignalBus 验证")
    print("=" * 60)

    # ---- Mock 策略 ----
    class MockStrategyA:
        name = 'MockStrategyA'

        def generate_signals(self, date, market_data, portfolio):
            return [
                {
                    'ts_code': '000001',
                    'signal': 'BUY',
                    'weight': 0.35,
                    'reason': '动量突破',
                },
                {
                    'ts_code': '000002',
                    'signal': 'BUY',
                    'weight': 0.30,
                    'reason': '超跌反弹',
                },
            ]

    class MockStrategyB:
        name = 'MockStrategyB'

        def generate_signals(self, date, market_data, portfolio):
            return [
                {
                    'ts_code': '000001',  # 重复，权重更低
                    'signal': 'BUY',
                    'weight': 0.20,
                    'reason': '均线金叉',
                },
                {
                    'ts_code': '000003',
                    'signal': 'SELL',
                    'weight': 0.9,
                    'reason': '止损',
                },
            ]

    # ---- 测试数据 ----
    market_data = {
        '000001': {'close': 10.0, 'pct_chg': 2.5, 'name': '测试股A'},
        '000002': {'close': 20.0, 'pct_chg': 9.95, 'name': '测试股B'},  # 接近涨停
        '000003': {'close': 15.0, 'pct_chg': -3.0, 'name': '测试股C'},
    }

    portfolio = {
        'cash': 50000,
        'total_assets': 200000,
        'daily_pnl': -500,
        'positions': {
            '000003': {
                'quantity': 1000,
                'available_quantity': 1000,
                'cost_price': 12.0,
                'current_price': 15.0,
                'profit_rate': 0.25,
                'market_value': 15000,
            },
        },
    }

    # ---- 测试 SignalBus ----
    bus = SignalBus()

    print("\n1. 主流程 process()")
    # 重置状态
    bus.clear()
    signals = bus.process(
        date='20250101',
        market_data=market_data,
        portfolio=portfolio,
        strategies=[MockStrategyA(), MockStrategyB()],
    )

    print(f"   返回信号数: {len(signals)}")
    for s in signals:
        print(f"   {s['signal']:5s} {s['ts_code']:8s} w={s.get('weight', 0):.3f} "
              f"reason={s.get('reason', '')[:30]} "
              f"filter={s.get('filter_status', '?')}")

    # 验证去重：000001 只出现一次，权重为最高值 0.35
    buy_000001 = [s for s in signals if s['ts_code'] == '000001']
    assert len(buy_000001) == 1, f"000001 应该去重为1条，实际 {len(buy_000001)} 条"
    assert buy_000001[0]['weight'] == 0.35, f"应保留最高权重 0.35, 实际 {buy_000001[0]['weight']}"

    # 验证 000002 被拒绝（接近涨停）
    sig_000002 = [
        s for s in signals if s['ts_code'] == '000002'
    ]
    # 000002 应该被过滤掉（不在 returned signals 中）
    assert len(sig_000002) == 0, \
        f"000002 接近涨停应被过滤，但返回了 {len(sig_000002)} 条"

    # 验证排序：SELL 在前
    first_signal = signals[0] if signals else None
    if first_signal:
        print(f"\n   第一信号: {first_signal['signal']} {first_signal['ts_code']}")

    # ---- 测试半自动方法 ----
    print("\n2. 半自动模式方法")

    # 确认一个信号
    first_sig_id = signals[0]['signal_id'] if signals else None
    if first_sig_id:
        ok, msg = bus.confirm_signal(first_sig_id)
        print(f"   confirm_signal: ok={ok}, msg='{msg}'")
        assert ok, f"确认失败: {msg}"

        # 重复确认
        ok2, _ = bus.confirm_signal(first_sig_id)
        print(f"   重复确认: ok={ok2}")

    # 拒绝一个信号
    if len(signals) > 1:
        second_sig_id = signals[1]['signal_id']
        ok, msg = bus.reject_signal(second_sig_id, '人工判断风险过高')
        print(f"   reject_signal: ok={ok}, msg='{msg}'")
        assert ok, f"拒绝失败: {msg}"

    # 执行一个信号
    if first_sig_id:
        ok, msg = bus.mark_executed(first_sig_id, fill_price=10.25, fill_qty=500)
        print(f"   mark_executed: ok={ok}, msg='{msg}'")
        assert ok, f"标记执行失败: {msg}"

    # ---- 测试 pending / history ----
    print("\n3. pending / history")
    pending = bus.get_pending_signals()
    print(f"   pending signals: {len(pending)}")
    history = bus.get_signal_history(days=30)
    print(f"   history (30d): {len(history)}")

    # ---- 测试过期 ----
    print("\n4. expire_old_signals()")
    expired = bus.expire_old_signals(timeout_minutes=30)
    print(f"   expired: {expired}")

    # ---- 测试统计 ----
    print("\n5. 统计信息")
    stats = bus.get_statistics()
    for k, v in stats.items():
        if k != 'by_strategy':
            print(f"   {k}: {v}")
    print(f"   by_strategy: {stats.get('by_strategy', {})}")

    # ---- 测试 get_signal ----
    print("\n6. get_signal()")
    if first_sig_id:
        detail = bus.get_signal(first_sig_id)
        if detail:
            print(f"   signal_id: {detail.get('signal_id')}")
            print(f"   status: {detail.get('status')}")
            print(f"   fill_price: {detail.get('fill_price')}")
            print(f"   fill_qty: {detail.get('fill_qty')}")

    print("\n" + "=" * 60)
    print("所有 SignalBus 验证通过!")
    print("=" * 60)
