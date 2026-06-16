"""
分钟线执行模拟器
基于历史分钟线计算真实 VWAP 成交价，替代固定滑点
"""

from typing import List, Dict, Optional
from utils.logger import get_logger

logger = get_logger('backtest', 'backtest.log')


class MinuteBarExecutor:
    """基于历史分钟线模拟订单执行"""

    def __init__(self, minute_bars: List[dict]):
        """
        Args:
            minute_bars: 当日分钟线列表，每项含 {trade_time, open, high, low, close, volume, amount}
                         已按时间排序
        """
        self._bars = minute_bars or []

    @property
    def has_data(self) -> bool:
        return len(self._bars) > 0

    def _calc_vwap(self) -> float:
        """计算全日 VWAP"""
        total_vol = 0.0
        total_amount = 0.0
        for b in self._bars:
            vol = b.get('volume', 0) or 0
            price = b.get('close', 0) or 0
            total_vol += vol
            total_amount += price * vol
        if total_vol == 0:
            return self._bars[0]['close'] if self._bars else 0
        return total_amount / total_vol

    def estimate_fill_price(
        self,
        side: str,
        quantity: int,
        time_index: int = 30,
    ) -> dict:
        """
        预估成交价格

        从触发时间点开始，逐分钟累加成交量，直到满足目标数量。
        按成交量加权计算平均成交价。

        Args:
            side: 'BUY' or 'SELL'
            quantity: 目标股数
            time_index: 假设在第几根分钟线触发（默认 30 = 09:45）

        Returns:
            {fill_price, slippage_pct, filled_quantity, bars_consumed, fully_filled}
        """
        if not self._bars or quantity <= 0:
            return {
                'fill_price': 0,
                'slippage_pct': 0,
                'filled_quantity': 0,
                'bars_consumed': 0,
                'fully_filled': False,
            }

        # Clamp time_index
        time_index = max(0, min(time_index, len(self._bars) - 1))
        trigger_price = self._bars[time_index].get('close', 0)

        accumulated_vol = 0.0
        accumulated_amount = 0.0

        for i in range(time_index, len(self._bars)):
            bar = self._bars[i]
            bar_vol = bar.get('volume', 0) or 0
            bar_price = bar.get('close', 0) or 0

            vol_needed = quantity - accumulated_vol
            take_vol = min(vol_needed, bar_vol)

            accumulated_vol += take_vol
            accumulated_amount += bar_price * take_vol

            if accumulated_vol >= quantity:
                avg_price = accumulated_amount / accumulated_vol if accumulated_vol > 0 else bar_price
                return {
                    'fill_price': round(avg_price, 2),
                    'slippage_pct': round((avg_price / trigger_price - 1) * 100, 4) if trigger_price > 0 else 0,
                    'filled_quantity': int(accumulated_vol),
                    'bars_consumed': i - time_index + 1,
                    'fully_filled': True,
                }

        # Not enough volume — partial fill
        avg_price = accumulated_amount / accumulated_vol if accumulated_vol > 0 else self._bars[-1]['close']
        return {
            'fill_price': round(avg_price, 2),
            'slippage_pct': round((avg_price / trigger_price - 1) * 100, 4) if trigger_price > 0 else 0,
            'filled_quantity': int(accumulated_vol),
            'bars_consumed': len(self._bars) - time_index,
            'fully_filled': False,
        }

    def get_liquidity_profile(self) -> dict:
        """返回当日流动性概况"""
        if not self._bars:
            return {}
        total_vol = sum(b.get('volume', 0) or 0 for b in self._bars)
        avg_vol_per_bar = total_vol / len(self._bars) if self._bars else 0
        prices = [b['close'] for b in self._bars if b.get('close')]
        return {
            'total_volume': total_vol,
            'avg_volume_per_bar': round(avg_vol_per_bar, 1),
            'vwap': round(self._calc_vwap(), 2),
            'high': max(prices) if prices else 0,
            'low': min(prices) if prices else 0,
            'bar_count': len(self._bars),
        }
