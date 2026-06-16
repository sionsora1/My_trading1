"""
数据源抽象基类
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import pandas as pd


class BaseDataSource(ABC):
    """数据源抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """获取A股股票列表"""
        ...

    @abstractmethod
    def get_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日K线数据"""
        ...

    @abstractmethod
    def get_realtime_quotes(self, codes: List[str]) -> Dict[str, dict]:
        """获取实时行情"""
        ...

    def get_stock_info(self, symbol: str) -> dict:
        """获取股票基本信息 (可选)"""
        return {}

    def get_financial_data(self, symbol: str) -> dict:
        """获取财务数据 (可选)"""
        return {}

    def get_minute_data(self, ts_code: str, period: str = '5',
                        start_time: str = None, end_time: str = None) -> pd.DataFrame:
        """获取分钟K线 (可选)"""
        return pd.DataFrame()

    def get_xdxr_data(self, ts_code: str) -> list:
        """获取除权除息数据 (可选)"""
        return []

    def get_transaction_data(self, ts_code: str, count: int = 10) -> list:
        """获取分笔成交 (可选)"""
        return []
