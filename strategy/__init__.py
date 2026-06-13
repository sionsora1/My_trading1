from .base import BaseStrategy
from .eight_factor import EightFactorStrategy
from .position_strategy import PositionStrategy
from .ai_strategy import AIStrategy
from .momentum_strategy import (
    MomentumStrategy,
    MeanReversionStrategy,
    ValueStrategy,
    QualityStrategy,
    LowVolatilityStrategy,
    TrendFollowingStrategy as SimpleTrendFollowingStrategy  # still in momentum_strategy
)
from .trend_following import EnhancedTrendFollowingStrategy
from .mean_reversion import EnhancedMeanReversionStrategy
from .intraday_reversal import IntradayReversalStrategy
from .low_volatility import EnhancedLowVolatilityStrategy
from .sector_rotation import SectorRotationStrategy

# 策略注册表
STRATEGY_REGISTRY = {
    'eight_factor': {
        'class': EightFactorStrategy,
        'name': '8因子选股策略',
        'description': '价值(EP) + 成长(增速) + 反转 + 低换手 + 低波动 + 质量(ROE) + 利润质量',
        'category': 'multi_factor'
    },
    'ai_factor': {
        'class': AIStrategy,
        'name': 'AI因子挖掘策略',
        'description': 'LightGBM自动从50+特征中学习非线性因子组合，替代人工定义因子权重',
        'category': 'ai_ml'
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
        'class': EnhancedMeanReversionStrategy,
        'name': '均值回归策略',
        'description': '买入近期跌幅大的股票，超跌反弹（增强版：多因子超卖评分+下降趋势过滤）',
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
        'class': EnhancedLowVolatilityStrategy,
        'name': '低波动防御策略（增强）',
        'description': '低波动+高质量+低估值，熊市/高波动环境防守型策略',
        'category': 'defensive'
    },
    'sector_rotation': {
        'class': SectorRotationStrategy,
        'name': '板块轮动策略',
        'description': '跟踪行业资金流向，在强势行业中选龙头，适合结构性行情',
        'category': 'sector'
    },
    'trend_following': {
        'class': EnhancedTrendFollowingStrategy,
        'name': '趋势跟随策略',
        'description': '买入站上均线的股票，趋势跟踪（增强版：多因子趋势评分0-1）',
        'category': 'trend'
    },
    'intraday_reversal': {
        'class': IntradayReversalStrategy,
        'name': '盘中反转策略',
        'description': '基于5分钟K线检测V型底和A型顶，捕捉盘中反转机会',
        'category': 'intraday'
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