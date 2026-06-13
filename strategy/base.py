"""
策略基类
"""

from abc import ABC, abstractmethod
from typing import List, Dict


class BaseStrategy(ABC):
    """策略基类"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.name = self.__class__.__name__

    @abstractmethod
    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """
        生成交易信号

        Args:
            date: 当前日期
            market_data: {ts_code: stock_data}
            portfolio: {'cash': float, 'positions': {ts_code: position_data}}

        Returns:
            信号列表: [{'ts_code': str, 'signal': 'BUY'/'SELL', 'weight': float, 'reason': str}]
        """
        pass