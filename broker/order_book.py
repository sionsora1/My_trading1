"""
盘口估算器
基于5档盘口预估真实成交价
"""

from typing import Dict
from utils.logger import get_logger

logger = get_logger('live_trading', 'live_trading.log')


class OrderBookEstimator:
    """基于5档盘口预估成交价和流动性"""

    @staticmethod
    def estimate(quotes: dict, side: str, quantity: int) -> dict:
        """
        基于盘口深度预估成交价

        Args:
            quotes: TDX 实时行情 dict, 包含:
                    ask1-ask5 (卖价), ask_vol1-ask_vol5 (卖量, 单位:手)
                    bid1-bid5 (买价), bid_vol1-bid_vol5 (买量, 单位:手)
            side: 'BUY' or 'SELL'
            quantity: 目标股数

        Returns:
            {
                estimated_price: 预估成交均价,
                depth_available: 盘口可成交量(股),
                enough_liquidity: 流动性是否充足,
                slippage_from_best: 偏离最优价的百分比,
                levels_consumed: 吃掉了几档
            }
        """
        if not quotes:
            return {
                'estimated_price': 0,
                'depth_available': 0,
                'enough_liquidity': False,
                'slippage_from_best': 0,
                'levels_consumed': 0,
            }

        remaining = quantity
        total_cost = 0.0
        levels_used = 0
        best_price = 0

        for level in range(1, 6):
            if side == 'BUY':
                price = quotes.get(f'ask{level}', 0) or 0
                vol = (quotes.get(f'ask_vol{level}', 0) or 0) * 100  # 手→股
            else:  # SELL
                price = quotes.get(f'bid{level}', 0) or 0
                vol = (quotes.get(f'bid_vol{level}', 0) or 0) * 100

            if price <= 0:
                continue

            if best_price == 0:
                best_price = price

            take = min(remaining, vol)
            if take > 0:
                total_cost += price * take
                remaining -= take
                levels_used += 1

            if remaining <= 0:
                break

        filled = quantity - remaining
        avg_price = total_cost / filled if filled > 0 else 0

        return {
            'estimated_price': round(avg_price, 2),
            'depth_available': filled,
            'enough_liquidity': remaining <= 0,
            'slippage_from_best': round((avg_price / best_price - 1) * 100, 4) if best_price > 0 else 0,
            'levels_consumed': levels_used,
        }

    @staticmethod
    def get_spread(quotes: dict) -> dict:
        """计算买卖价差"""
        bid1 = quotes.get('bid1', 0) or 0
        ask1 = quotes.get('ask1', 0) or 0
        if bid1 <= 0 or ask1 <= 0:
            return {'spread': 0, 'spread_pct': 0}
        spread = ask1 - bid1
        return {
            'spread': round(spread, 2),
            'spread_pct': round(spread / bid1 * 100, 4),
        }

    @staticmethod
    def get_depth_summary(quotes: dict) -> dict:
        """盘口深度摘要"""
        total_bid_vol = sum(quotes.get(f'bid_vol{i}', 0) or 0 for i in range(1, 6))
        total_ask_vol = sum(quotes.get(f'ask_vol{i}', 0) or 0 for i in range(1, 6))
        return {
            'total_bid_vol': total_bid_vol * 100,  # 手→股
            'total_ask_vol': total_ask_vol * 100,
            'bid_ask_ratio': round(total_bid_vol / total_ask_vol, 2) if total_ask_vol > 0 else 0,
        }
