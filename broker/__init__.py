"""
券商/交易接口注册表
设计模式与 strategy/__init__.py 一致
"""

from .base import BaseBroker, OrderSide, OrderType, OrderStatus
from .base import OrderRequest, OrderResult, AccountInfo, PositionInfo, Signal, DailyRiskLimit
from .base import is_trading_time, is_trading_day
from .sim_broker import SimBroker
from .qmt_broker import QMTBroker
from .ths_broker import THSBroker
from .notify import SignalNotifier
from .risk_manager import RiskManager

# 券商注册表
BROKER_REGISTRY = {
    'sim': {
        'class': SimBroker,
        'name': '模拟盘',
        'description': '本地模拟交易，虚拟资金，模拟真实费率',
        'category': 'simulation',
        'features': ['回测', '模拟', '教学'],
    },
    'qmt': {
        'class': QMTBroker,
        'name': 'QMT迅投',
        'description': '通过迅投 QMT 连接真实券商账户（需安装 xtquant）',
        'category': 'live',
        'features': ['实盘', '自动交易'],
    },
    'ths': {
        'class': THSBroker,
        'name': '同花顺',
        'description': '通过同花顺 PC 客户端自动交易（需安装 easytrader + 同花顺客户端）',
        'category': 'live',
        'features': ['实盘', '自动交易', '同花顺'],
    },
}


def get_broker(broker_name: str, config: dict = None) -> BaseBroker:
    """获取券商连接器实例"""
    if broker_name not in BROKER_REGISTRY:
        raise ValueError(f"未知券商: {broker_name}。可选: {list(BROKER_REGISTRY.keys())}")

    broker_info = BROKER_REGISTRY[broker_name]
    return broker_info['class'](config)


def get_all_brokers() -> dict:
    """获取所有券商信息"""
    return {
        name: {
            'name': info['name'],
            'description': info['description'],
            'category': info['category'],
            'features': info['features'],
        }
        for name, info in BROKER_REGISTRY.items()
    }
