"""
市场环境判断模块
判断当前是牛市、熊市还是震荡市，并推荐适合的策略
"""

import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from dataclasses import dataclass
from enum import Enum


class MarketRegime(Enum):
    """市场环境类型"""
    BULL = "bull"           # 牛市
    BEAR = "bear"           # 熊市
    SIDEWAYS = "sideways"   # 震荡市
    VOLATILE = "volatile"   # 高波动市
    CRASH = "crash"         # 暴跌市


@dataclass
class RegimeAnalysis:
    """市场环境分析结果"""
    regime: MarketRegime
    confidence: float           # 置信度 0-1
    description: str            # 描述
    indicators: Dict[str, float]  # 各指标值
    recommended_strategies: List[str]  # 推荐策略
    risk_level: str             # 风险等级
    position_advice: str        # 仓位建议


class MarketRegimeDetector:
    """
    市场环境检测器

    综合多个指标判断当前市场环境：
    1. 趋势指标：均线、涨跌幅
    2. 波动指标：波动率、ATR
    3. 情绪指标：涨停/跌停比、换手率
    4. 资金指标：北向资金、融资余额
    """

    def __init__(self):
        # 各指标权重
        self.weights = {
            'trend': 0.30,      # 趋势
            'volatility': 0.20, # 波动
            'momentum': 0.25,   # 动量
            'breadth': 0.15,    # 市场广度
            'sentiment': 0.10,  # 情绪
        }

    def detect(self, market_data: Dict, index_data: Dict = None) -> RegimeAnalysis:
        """
        检测市场环境

        Args:
            market_data: 市场数据 {ts_code: stock_data}
            index_data: 指数数据（可选）

        Returns:
            RegimeAnalysis: 市场环境分析结果
        """
        # 计算各维度得分
        trend_score = self._calc_trend_score(market_data, index_data)
        volatility_score = self._calc_volatility_score(market_data)
        momentum_score = self._calc_momentum_score(market_data)
        breadth_score = self._calc_breadth_score(market_data)
        sentiment_score = self._calc_sentiment_score(market_data)

        # 综合得分
        scores = {
            'trend': trend_score,
            'volatility': volatility_score,
            'momentum': momentum_score,
            'breadth': breadth_score,
            'sentiment': sentiment_score,
        }

        weighted_score = sum(scores[k] * self.weights[k] for k in scores)

        # 判断市场环境
        regime, confidence = self._classify_regime(weighted_score, scores)

        # 推荐策略
        recommended = self._recommend_strategies(regime, scores)

        # 风险等级和仓位建议
        risk_level, position_advice = self._get_risk_advice(regime, scores)

        # 描述
        description = self._generate_description(regime, scores, confidence)

        return RegimeAnalysis(
            regime=regime,
            confidence=confidence,
            description=description,
            indicators=scores,
            recommended_strategies=recommended,
            risk_level=risk_level,
            position_advice=position_advice
        )

    def _calc_trend_score(self, market_data: Dict, index_data: Dict = None) -> float:
        """
        计算趋势得分
        -1: 强下跌趋势
         0: 无明显趋势
        +1: 强上涨趋势
        """
        scores = []

        for code, stock in market_data.items():
            close = stock.get('close', 0)
            ma5 = stock.get('ma5', close)
            ma10 = stock.get('ma10', close)
            ma20 = stock.get('ma20', close)
            ma60 = stock.get('ma60', close)

            if ma60 == 0:
                continue

            # 均线多头排列程度
            ma_score = 0
            if close > ma5 > ma10 > ma20 > ma60:
                ma_score = 1.0  # 完美多头
            elif close > ma20 > ma60:
                ma_score = 0.5  # 部分多头
            elif close < ma5 < ma10 < ma20 < ma60:
                ma_score = -1.0  # 完美空头
            elif close < ma20 < ma60:
                ma_score = -0.5  # 部分空头

            # 距离均线的偏离度
            deviation = (close - ma60) / ma60 if ma60 > 0 else 0
            deviation_score = np.clip(deviation * 5, -1, 1)  # 放大5倍并限制在[-1,1]

            scores.append(ma_score * 0.6 + deviation_score * 0.4)

        return np.mean(scores) if scores else 0

    def _calc_volatility_score(self, market_data: Dict) -> float:
        """
        计算波动率得分
        -1: 极高波动（风险高）
         0: 正常波动
        +1: 低波动（稳定）
        """
        volatilities = []

        for code, stock in market_data.items():
            vol = stock.get('volatility', 0.25)
            volatilities.append(vol)

        if not volatilities:
            return 0

        avg_vol = np.mean(volatilities)

        # 波动率评分（A股平均波动率约0.25-0.30）
        if avg_vol < 0.15:
            return 0.8   # 低波动，市场稳定
        elif avg_vol < 0.25:
            return 0.4   # 正常偏低
        elif avg_vol < 0.35:
            return -0.2  # 正常偏高
        elif avg_vol < 0.50:
            return -0.6  # 高波动
        else:
            return -1.0  # 极高波动

    def _calc_momentum_score(self, market_data: Dict) -> float:
        """
        计算动量得分
        -1: 强负动量（持续下跌）
         0: 无明显动量
        +1: 强正动量（持续上涨）
        """
        returns_20d = []
        returns_60d = []

        for code, stock in market_data.items():
            r20 = stock.get('return_20d', 0)
            r60 = stock.get('return_60d', 0)
            returns_20d.append(r20)
            returns_60d.append(r60)

        if not returns_20d:
            return 0

        avg_20d = np.mean(returns_20d)
        avg_60d = np.mean(returns_60d)

        # 短期动量
        if avg_20d > 0.10:
            short_score = 0.8
        elif avg_20d > 0.03:
            short_score = 0.4
        elif avg_20d > -0.03:
            short_score = 0
        elif avg_20d > -0.10:
            short_score = -0.4
        else:
            short_score = -0.8

        # 中期动量
        if avg_60d > 0.20:
            long_score = 0.8
        elif avg_60d > 0.05:
            long_score = 0.4
        elif avg_60d > -0.05:
            long_score = 0
        elif avg_60d > -0.20:
            long_score = -0.4
        else:
            long_score = -0.8

        return short_score * 0.6 + long_score * 0.4

    def _calc_breadth_score(self, market_data: Dict) -> float:
        """
        计算市场广度得分
        -1: 大部分股票下跌
         0: 涨跌各半
        +1: 大部分股票上涨
        """
        up_count = 0
        down_count = 0
        total = 0

        for code, stock in market_data.items():
            ret = stock.get('return_1d', 0)
            total += 1
            if ret > 0:
                up_count += 1
            elif ret < 0:
                down_count += 1

        if total == 0:
            return 0

        up_ratio = up_count / total

        if up_ratio > 0.70:
            return 0.8   # 普涨
        elif up_ratio > 0.55:
            return 0.3   # 偏强
        elif up_ratio > 0.45:
            return 0     # 平衡
        elif up_ratio > 0.30:
            return -0.3  # 偏弱
        else:
            return -0.8  # 普跌

    def _calc_sentiment_score(self, market_data: Dict) -> float:
        """
        计算情绪得分（简化版）
        基于涨跌幅极端程度
        """
        extreme_up = 0
        extreme_down = 0
        total = 0

        for code, stock in market_data.items():
            ret = stock.get('return_1d', 0)
            total += 1
            if ret > 0.05:
                extreme_up += 1
            elif ret < -0.05:
                extreme_down += 1

        if total == 0:
            return 0

        # 极端涨跌比
        if extreme_down > extreme_up * 2:
            return -0.8  # 恐慌
        elif extreme_down > extreme_up:
            return -0.3  # 偏恐慌
        elif extreme_up > extreme_down * 2:
            return 0.8   # 贪婪
        elif extreme_up > extreme_down:
            return 0.3   # 偏贪婪
        else:
            return 0     # 中性

    def _classify_regime(self, weighted_score: float, scores: Dict) -> Tuple[MarketRegime, float]:
        """分类市场环境"""
        # 极端情况判断
        if scores['momentum'] < -0.7 and scores['breadth'] < -0.5:
            return MarketRegime.CRASH, 0.9

        if scores['volatility'] < -0.7:
            return MarketRegime.VOLATILE, 0.8

        # 常规判断
        if weighted_score > 0.35:
            confidence = min(0.5 + weighted_score, 0.95)
            return MarketRegime.BULL, confidence
        elif weighted_score < -0.35:
            confidence = min(0.5 + abs(weighted_score), 0.95)
            return MarketRegime.BEAR, confidence
        else:
            confidence = 0.6 - abs(weighted_score)
            return MarketRegime.SIDEWAYS, confidence

    def _recommend_strategies(self, regime: MarketRegime, scores: Dict) -> List[str]:
        """推荐策略"""
        recommendations = {
            MarketRegime.BULL: [
                "momentum",         # 动量策略：牛市追涨
                "trend_following",  # 趋势跟随：顺势而为
                "eight_factor",     # 8因子：多头市场因子有效
            ],
            MarketRegime.BEAR: [
                "low_volatility",   # 低波动：防守型
                "quality",          # 质量：优质公司抗跌
                "value",            # 价值：低估值安全边际
            ],
            MarketRegime.SIDEWAYS: [
                "mean_reversion",   # 均值回归：震荡市高抛低吸
                "eight_factor",     # 8因子：综合选股
                "position",         # 位置判断：灵活应对
            ],
            MarketRegime.VOLATILE: [
                "low_volatility",   # 低波动：避开高波动
                "quality",          # 质量：稳健型
            ],
            MarketRegime.CRASH: [
                # 暴跌市建议空仓或极轻仓
                "low_volatility",   # 低波动：最防守
            ],
        }

        return recommendations.get(regime, ["eight_factor"])

    def _get_risk_advice(self, regime: MarketRegime, scores: Dict) -> Tuple[str, str]:
        """获取风险等级和仓位建议"""
        risk_map = {
            MarketRegime.BULL: ("中等", "可以保持较高仓位(60-80%)，但注意追高风险"),
            MarketRegime.BEAR: ("高", "降低仓位(30-50%)，以防守为主"),
            MarketRegime.SIDEWAYS: ("中等", "控制仓位(40-60%)，高抛低吸"),
            MarketRegime.VOLATILE: ("高", "降低仓位(20-40%)，等待波动收敛"),
            MarketRegime.CRASH: ("极高", "建议空仓或极轻仓(0-20%)，现金为王"),
        }

        return risk_map.get(regime, ("中等", "根据个股情况决定"))

    def _generate_description(self, regime: MarketRegime, scores: Dict, confidence: float) -> str:
        """生成描述"""
        regime_names = {
            MarketRegime.BULL: "牛市",
            MarketRegime.BEAR: "熊市",
            MarketRegime.SIDEWAYS: "震荡市",
            MarketRegime.VOLATILE: "高波动市",
            MarketRegime.CRASH: "暴跌市",
        }

        name = regime_names[regime]

        # 各维度描述
        details = []

        if scores['trend'] > 0.3:
            details.append("趋势向上")
        elif scores['trend'] < -0.3:
            details.append("趋势向下")

        if scores['momentum'] > 0.3:
            details.append("正动量强")
        elif scores['momentum'] < -0.3:
            details.append("负动量强")

        if scores['breadth'] > 0.3:
            details.append("多数股票上涨")
        elif scores['breadth'] < -0.3:
            details.append("多数股票下跌")

        if scores['volatility'] < -0.3:
            details.append("波动率偏高")

        detail_str = "，".join(details) if details else "无明显特征"

        return f"当前判断为{name}（置信度{confidence:.0%}）。{detail_str}。"


class StrategyRegimeAdapter:
    """
    策略环境适配器

    根据市场环境调整策略参数
    """

    # 各策略在不同市场环境下的表现评分
    STRATEGY_PERFORMANCE = {
        'eight_factor': {
            MarketRegime.BULL: 0.8,
            MarketRegime.BEAR: 0.4,
            MarketRegime.SIDEWAYS: 0.7,
            MarketRegime.VOLATILE: 0.5,
            MarketRegime.CRASH: 0.2,
        },
        'momentum': {
            MarketRegime.BULL: 0.9,
            MarketRegime.BEAR: 0.2,
            MarketRegime.SIDEWAYS: 0.4,
            MarketRegime.VOLATILE: 0.5,
            MarketRegime.CRASH: 0.1,
        },
        'mean_reversion': {
            MarketRegime.BULL: 0.5,
            MarketRegime.BEAR: 0.5,
            MarketRegime.SIDEWAYS: 0.9,
            MarketRegime.VOLATILE: 0.6,
            MarketRegime.CRASH: 0.3,
        },
        'value': {
            MarketRegime.BULL: 0.6,
            MarketRegime.BEAR: 0.7,
            MarketRegime.SIDEWAYS: 0.7,
            MarketRegime.VOLATILE: 0.5,
            MarketRegime.CRASH: 0.4,
        },
        'quality': {
            MarketRegime.BULL: 0.6,
            MarketRegime.BEAR: 0.8,
            MarketRegime.SIDEWAYS: 0.7,
            MarketRegime.VOLATILE: 0.6,
            MarketRegime.CRASH: 0.5,
        },
        'low_volatility': {
            MarketRegime.BULL: 0.5,
            MarketRegime.BEAR: 0.8,
            MarketRegime.SIDEWAYS: 0.6,
            MarketRegime.VOLATILE: 0.7,
            MarketRegime.CRASH: 0.6,
        },
        'trend_following': {
            MarketRegime.BULL: 0.9,
            MarketRegime.BEAR: 0.3,
            MarketRegime.SIDEWAYS: 0.3,
            MarketRegime.VOLATILE: 0.4,
            MarketRegime.CRASH: 0.1,
        },
        'position': {
            MarketRegime.BULL: 0.7,
            MarketRegime.BEAR: 0.6,
            MarketRegime.SIDEWAYS: 0.7,
            MarketRegime.VOLATILE: 0.5,
            MarketRegime.CRASH: 0.3,
        },
    }

    @classmethod
    def get_strategy_score(cls, strategy_name: str, regime: MarketRegime) -> float:
        """获取策略在当前市场环境下的评分"""
        return cls.STRATEGY_PERFORMANCE.get(strategy_name, {}).get(regime, 0.5)

    @classmethod
    def adjust_position_weight(cls, base_weight: float, regime: MarketRegime) -> float:
        """根据市场环境调整仓位权重"""
        adjustments = {
            MarketRegime.BULL: 1.0,      # 不调整
            MarketRegime.BEAR: 0.6,      # 降低40%
            MarketRegime.SIDEWAYS: 0.8,  # 降低20%
            MarketRegime.VOLATILE: 0.5,  # 降低50%
            MarketRegime.CRASH: 0.2,     # 降低80%
        }
        return base_weight * adjustments.get(regime, 0.8)

    @classmethod
    def adjust_stop_loss(cls, base_stop_loss: float, regime: MarketRegime) -> float:
        """根据市场环境调整止损线"""
        adjustments = {
            MarketRegime.BULL: base_stop_loss * 1.2,      # 放宽20%
            MarketRegime.BEAR: base_stop_loss * 0.7,      # 收紧30%
            MarketRegime.SIDEWAYS: base_stop_loss,         # 不变
            MarketRegime.VOLATILE: base_stop_loss * 0.6,  # 收紧40%
            MarketRegime.CRASH: base_stop_loss * 0.5,     # 收紧50%
        }
        return adjustments.get(regime, base_stop_loss)


# ============================================================
# 使用示例
# ============================================================

def analyze_market(market_data: Dict) -> RegimeAnalysis:
    """分析市场环境"""
    detector = MarketRegimeDetector()
    return detector.detect(market_data)


def print_regime_analysis(analysis: RegimeAnalysis):
    """打印市场环境分析"""
    print("=" * 60)
    print("市场环境分析")
    print("=" * 60)

    regime_names = {
        MarketRegime.BULL: "🐂 牛市",
        MarketRegime.BEAR: "🐻 熊市",
        MarketRegime.SIDEWAYS: "↔️ 震荡市",
        MarketRegime.VOLATILE: "📈📉 高波动市",
        MarketRegime.CRASH: "💥 暴跌市",
    }

    print(f"\n当前市场：{regime_names.get(analysis.regime, '未知')}")
    print(f"置信度：{analysis.confidence:.0%}")
    print(f"风险等级：{analysis.risk_level}")

    print(f"\n各维度得分：")
    for name, score in analysis.indicators.items():
        bar = "+" * int(max(0, score * 10)) + "-" * int(max(0, -score * 10))
        print(f"  {name:12}: {score:+.2f} [{bar}]")

    print(f"\n推荐策略：")
    for strategy in analysis.recommended_strategies:
        print(f"  - {strategy}")

    print(f"\n仓位建议：{analysis.position_advice}")
    print(f"\n综合描述：{analysis.description}")
    print("=" * 60)


if __name__ == '__main__':
    # 测试用的模拟数据
    np.random.seed(42)

    # 模拟熊市数据
    bear_market = {}
    for i in range(50):
        code = f"{600000 + i:06d}"
        bear_market[code] = {
            'close': 50 * (1 - np.random.uniform(0, 0.3)),
            'ma5': 50 * (1 - np.random.uniform(0, 0.2)),
            'ma10': 50 * (1 - np.random.uniform(0, 0.15)),
            'ma20': 50 * (1 - np.random.uniform(0, 0.1)),
            'ma60': 50 * (1 - np.random.uniform(0, 0.05)),
            'return_1d': np.random.normal(-0.02, 0.03),
            'return_20d': np.random.normal(-0.15, 0.1),
            'return_60d': np.random.normal(-0.25, 0.15),
            'volatility': np.random.uniform(0.3, 0.5),
        }

    print("\n=== 熊市环境测试 ===")
    analysis = analyze_market(bear_market)
    print_regime_analysis(analysis)

    # 模拟牛市数据
    bull_market = {}
    for i in range(50):
        code = f"{600000 + i:06d}"
        bull_market[code] = {
            'close': 50 * (1 + np.random.uniform(0, 0.5)),
            'ma5': 50 * (1 + np.random.uniform(0, 0.4)),
            'ma10': 50 * (1 + np.random.uniform(0, 0.3)),
            'ma20': 50 * (1 + np.random.uniform(0, 0.2)),
            'ma60': 50 * (1 + np.random.uniform(0, 0.1)),
            'return_1d': np.random.normal(0.02, 0.02),
            'return_20d': np.random.normal(0.15, 0.1),
            'return_60d': np.random.normal(0.30, 0.15),
            'volatility': np.random.uniform(0.15, 0.25),
        }

    print("\n=== 牛市环境测试 ===")
    analysis = analyze_market(bull_market)
    print_regime_analysis(analysis)