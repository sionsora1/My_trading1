from .base import BaseStrategy
from .eight_factor import EightFactorStrategy
from .position_strategy import PositionStrategy
from .momentum_strategy import (
    MomentumStrategy,
    MeanReversionStrategy,
    ValueStrategy,
    QualityStrategy,
    LowVolatilityStrategy,
    TrendFollowingStrategy
)

# 策略注册表
STRATEGY_REGISTRY = {
    'eight_factor': {
        'class': EightFactorStrategy,
        'name': '8因子选股策略',
        'description': '价值(EP) + 成长(增速) + 反转 + 低换手 + 低波动 + 质量(ROE) + 利润质量',
        'category': 'multi_factor'
    },
    'position': {
        'class': PositionStrategy,
        'name': '位置判断策略',
        'description': '低位看基本面/消息面，高位看趋势/量价',
        'category': 'hybrid'
    },
    'momentum': {
        'class': MomentumStrategy,
        'name': '动量策略',
        'description': '买入近期涨幅大的股票，强者恒强',
        'category': 'trend'
    },
    'mean_reversion': {
        'class': MeanReversionStrategy,
        'name': '均值回归策略',
        'description': '买入近期跌幅大的股票，超跌反弹',
        'category': 'contrarian'
    },
    'value': {
        'class': ValueStrategy,
        'name': '价值策略',
        'description': '买入低PE、低PB的股票，长期持有',
        'category': 'fundamental'
    },
    'quality': {
        'class': QualityStrategy,
        'name': '质量策略',
        'description': '买入高ROE、低波动的优质公司',
        'category': 'fundamental'
    },
    'low_volatility': {
        'class': LowVolatilityStrategy,
        'name': '低波动策略',
        'description': '买入波动率低的股票，低波动异象',
        'category': 'risk'
    },
    'trend_following': {
        'class': TrendFollowingStrategy,
        'name': '趋势跟随策略',
        'description': '买入站上均线的股票，趋势跟踪',
        'category': 'trend'
    }
}


def get_strategy(strategy_name: str, config: dict = None):
    """获取策略实例"""
    if strategy_name not in STRATEGY_REGISTRY:
        raise ValueError(f"未知策略: {strategy_name}")

    strategy_info = STRATEGY_REGISTRY[strategy_name]
    return strategy_info['class'](config)


def get_all_strategies():
    """获取所有策略信息"""
    return {
        name: {
            'name': info['name'],
            'description': info['description'],
            'category': info['category']
        }
        for name, info in STRATEGY_REGISTRY.items()
    }