"""
ETF 行情轮动策略
ETF Rotation Strategy

在 6 只 ETF 之间做行情轮动：
- 持有 20 日涨幅排名第一且站上 28 日均线的 ETF
- 排名下滑（差值 > 1% 缓冲）或跌破均线时切换
- 全部跌破 28 日均线时空仓
"""

from typing import List
from .base import BaseStrategy


class EtfRotationStrategy(BaseStrategy):
    """ETF 行情轮动策略

    核心逻辑：
    1. 筛选合格池：close > MA28
    2. 合格池按 20 日涨幅降序排列
    3. 始终持有排名第 1 的 ETF（全仓）
    4. 排名掉到第 2 或以下（且差值 > 缓冲）时切换
    5. 持仓跌破 MA28 无条件卖出
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.etf_codes = self.config.get('etf_codes', [
            '510300', '159915', '518880', '159941', '513330', '511260',
        ])
        self.ma_period = self.config.get('ma_period', 28)
        self.buffer_pct = self.config.get('buffer_pct', 0.01)
        self.max_single_weight = self.config.get('max_single_weight', 1.0)

    def _get_ma28(self, stock: dict) -> float:
        """获取 ma28，缺失时回退到 close"""
        return stock.get('ma28', stock.get('close', 0))

    def _is_qualified(self, stock: dict) -> bool:
        """ETF 是否合格：close > MA28"""
        close = stock.get('close', 0)
        ma28 = self._get_ma28(stock)
        return close > ma28

    def _get_return_20d(self, stock: dict) -> float:
        """获取 20 日涨幅"""
        return stock.get('return_20d', 0) or 0

    def _rank_qualified(self, market_data: dict) -> list:
        """对合格池的 ETF 按 20 日涨幅降序排列

        Returns:
            [(code, return_20d), ...] 仅包含合格 ETF
        """
        qualified = []
        for code in self.etf_codes:
            stock = market_data.get(code)
            if stock is None:
                continue
            if self._is_qualified(stock):
                ret = self._get_return_20d(stock)
                qualified.append((code, ret))
        # 按 20 日涨幅降序，平局时代码升序保证确定性
        qualified.sort(key=lambda x: (-x[1], x[0]))
        return qualified

    def generate_signals(self, date: str, market_data: dict,
                         portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals: List[dict] = []
        current_positions = portfolio.get('positions', {})
        holding_code = None
        holding_return = 0.0

        # 找到当前持仓的 ETF
        for code in self.etf_codes:
            if code in current_positions:
                holding_code = code
                stock = market_data.get(code)
                if stock:
                    holding_return = self._get_return_20d(stock)
                break

        # 获取合格池排名
        ranked = self._rank_qualified(market_data)
        best_code = ranked[0][0] if ranked else None
        best_return = ranked[0][1] if ranked else 0.0

        # ----- 无持仓：尝试建仓 -----
        if holding_code is None:
            if best_code:
                stock = market_data[best_code]
                signals.append({
                    'ts_code': best_code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': (
                        f'ETF轮动/建仓（20日涨幅{best_return:+.2%}，'
                        f'MA28={self._get_ma28(stock):.3f}，'
                        f'close={stock["close"]:.3f}）'
                    ),
                })
            return signals

        # ----- 有持仓：检查是否需要卖出 -----
        holding_stock = market_data.get(holding_code)
        if holding_stock is None:
            return signals

        should_sell = False
        sell_reason = ''

        # 条件1：持仓 ETF 跌破 MA28（无条件卖出）
        if not self._is_qualified(holding_stock):
            should_sell = True
            sell_reason = (
                f'ETF轮动/止损（close={holding_stock["close"]:.3f}'
                f' < MA28={self._get_ma28(holding_stock):.3f}）'
            )

        # 条件2：持仓不在合格池第1，且差值 > 缓冲
        elif best_code and best_code != holding_code:
            diff = best_return - holding_return
            if diff > self.buffer_pct:
                should_sell = True
                sell_reason = (
                    f'ETF轮动/卖出（排名降至第2，'
                    f'{holding_code}={holding_return:+.2%}'
                    f' < {best_code}={best_return:+.2%}，'
                    f'差值{diff:.1%}>{self.buffer_pct:.0%}）'
                )

        if should_sell:
            signals.append({
                'ts_code': holding_code,
                'signal': 'SELL',
                'weight': 0,
                'reason': sell_reason,
            })
            # 卖出后切换到合格池第1
            if best_code and best_code != holding_code:
                stock = market_data[best_code]
                signals.append({
                    'ts_code': best_code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': (
                        f'ETF轮动/切换（{best_code} 20日涨幅{best_return:+.2%}'
                        f'排名第1，MA28上方）'
                    ),
                })

        return signals
