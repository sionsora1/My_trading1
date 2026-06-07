"""
动量策略
买入近期涨幅大的股票，卖出涨幅小的股票
逻辑：强者恒强，趋势延续
"""

from typing import List
from .base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """
    动量策略

    核心逻辑：
    - 买入过去N日涨幅最大的股票
    - 卖出涨幅落后或转负的股票
    - 适合趋势行情
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.lookback = self.config.get('lookback', 20)  # 回看天数
        self.top_n = self.config.get('top_n', 5)  # 选股数量
        self.max_single_weight = self.config.get('max_single_weight', 0.05)

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 计算所有股票的动量得分
        momentum_scores = {}
        for code, stock in market_data.items():
            # 动量得分 = 近20日收益
            score = stock.get('return_20d', 0)
            momentum_scores[code] = score

        # 按动量排序
        sorted_stocks = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)

        # 选前N只
        selected_codes = set(code for code, score in sorted_stocks[:self.top_n] if score > 0)

        # 买入信号
        for code in selected_codes:
            if code not in current_positions:
                score = momentum_scores[code]
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': f'动量策略（20日涨幅{score:.2%}）'
                })

        # 卖出信号
        for code in current_positions:
            if code not in selected_codes:
                score = momentum_scores.get(code, 0)
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': f'动量减弱（20日涨幅{score:.2%}）'
                })

        return signals


class MeanReversionStrategy(BaseStrategy):
    """
    均值回归策略

    核心逻辑：
    - 买入近期跌幅大的股票（超跌反弹）
    - 卖出涨幅过大的股票（获利了结）
    - 适合震荡行情
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n = self.config.get('top_n', 5)
        self.max_single_weight = self.config.get('max_single_weight', 0.05)
        self.oversold_threshold = self.config.get('oversold_threshold', -0.10)  # 超跌阈值

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 计算超跌得分（跌幅越大，得分越高）
        reversion_scores = {}
        for code, stock in market_data.items():
            # 超跌得分 = -近20日收益（跌得越多，分越高）
            return_20d = stock.get('return_20d', 0)
            if return_20d < self.oversold_threshold:
                reversion_scores[code] = -return_20d

        # 按超跌程度排序
        sorted_stocks = sorted(reversion_scores.items(), key=lambda x: x[1], reverse=True)

        # 选前N只
        selected_codes = set(code for code, score in sorted_stocks[:self.top_n])

        # 买入信号
        for code in selected_codes:
            if code not in current_positions:
                score = reversion_scores[code]
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': f'超跌反弹（20日跌幅{-score:.2%}）'
                })

        # 卖出信号：持仓中涨幅超过10%的
        for code in list(current_positions):
            if code in market_data:
                stock = market_data[code]
                profit_rate = portfolio['positions'].get(code, {}).get('profit_rate', 0)
                if profit_rate > 0.10:
                    signals.append({
                        'ts_code': code,
                        'signal': 'SELL',
                        'weight': 0,
                        'reason': f'获利了结（盈利{profit_rate:.2%}）'
                    })

        return signals


class ValueStrategy(BaseStrategy):
    """
    价值策略

    核心逻辑：
    - 买入低PE、低PB的股票
    - 卖出估值过高的股票
    - 适合长期持有
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n = self.config.get('top_n', 5)
        self.max_single_weight = self.config.get('max_single_weight', 0.05)

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 计算价值得分（EP越高越好）
        value_scores = {}
        for code, stock in market_data.items():
            ep = stock.get('ep', 0)
            roe = stock.get('roe', 0)
            # 综合价值得分
            score = ep * 0.6 + roe * 0.4
            value_scores[code] = score

        # 按价值排序
        sorted_stocks = sorted(value_scores.items(), key=lambda x: x[1], reverse=True)

        # 选前N只
        selected_codes = set(code for code, score in sorted_stocks[:self.top_n])

        # 买入信号
        for code in selected_codes:
            if code not in current_positions:
                score = value_scores[code]
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': f'价值策略（得分{score:.3f}）'
                })

        # 卖出信号
        for code in current_positions:
            if code not in selected_codes:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': '价值排名下降'
                })

        return signals


class QualityStrategy(BaseStrategy):
    """
    质量策略

    核心逻辑：
    - 买入高ROE、低负债、稳定盈利的公司
    - 卖出质量下降的公司
    - 适合长期持有
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n = self.config.get('top_n', 5)
        self.max_single_weight = self.config.get('max_single_weight', 0.05)

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 计算质量得分
        quality_scores = {}
        for code, stock in market_data.items():
            roe = stock.get('roe', 0)
            gross_margin = stock.get('gross_margin', 0)
            low_volatility = 1 - min(stock.get('volatility', 0.5), 1)

            # 综合质量得分
            score = roe * 0.5 + gross_margin * 0.3 + low_volatility * 0.2
            quality_scores[code] = score

        # 按质量排序
        sorted_stocks = sorted(quality_scores.items(), key=lambda x: x[1], reverse=True)

        # 选前N只
        selected_codes = set(code for code, score in sorted_stocks[:self.top_n])

        # 买入信号
        for code in selected_codes:
            if code not in current_positions:
                score = quality_scores[code]
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': f'质量策略（得分{score:.3f}）'
                })

        # 卖出信号
        for code in current_positions:
            if code not in selected_codes:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': '质量排名下降'
                })

        return signals


class LowVolatilityStrategy(BaseStrategy):
    """
    低波动策略

    核心逻辑：
    - 买入波动率低的股票
    - 卖出波动率升高的股票
    - 低波动股票长期往往跑赢高波动股票（低波动异象）
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n = self.config.get('top_n', 5)
        self.max_single_weight = self.config.get('max_single_weight', 0.05)

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 计算低波动得分（波动率越低，得分越高）
        low_vol_scores = {}
        for code, stock in market_data.items():
            volatility = stock.get('volatility', 0.5)
            # 排除波动率过高的
            if volatility < 0.5:
                low_vol_scores[code] = 1 - volatility

        # 按波动率排序（低的在前）
        sorted_stocks = sorted(low_vol_scores.items(), key=lambda x: x[1], reverse=True)

        # 选前N只
        selected_codes = set(code for code, score in sorted_stocks[:self.top_n])

        # 买入信号
        for code in selected_codes:
            if code not in current_positions:
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': '低波动策略'
                })

        # 卖出信号
        for code in current_positions:
            if code not in selected_codes:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': '波动率升高'
                })

        return signals


class TrendFollowingStrategy(BaseStrategy):
    """
    趋势跟随策略

    核心逻辑：
    - 买入站上均线的股票
    - 卖出跌破均线的股票
    - 简单有效的趋势跟踪
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n = self.config.get('top_n', 5)
        self.max_single_weight = self.config.get('max_single_weight', 0.05)

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # 筛选趋势向上的股票
        trend_stocks = []
        for code, stock in market_data.items():
            close = stock.get('close', 0)
            ma20 = stock.get('ma20', 0)
            ma60 = stock.get('ma60', 0)

            # 站上20日和60日均线
            if close > ma20 and close > ma60 and ma20 > ma60:
                # 趋势强度 = 距离均线的幅度
                trend_strength = (close - ma20) / ma20 if ma20 > 0 else 0
                trend_stocks.append((code, trend_strength))

        # 按趋势强度排序
        trend_stocks.sort(key=lambda x: x[1], reverse=True)

        # 选前N只
        selected_codes = set(code for code, _ in trend_stocks[:self.top_n])

        # 买入信号
        for code in selected_codes:
            if code not in current_positions:
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': '趋势跟随（站上均线）'
                })

        # 卖出信号：跌破20日均线
        for code in list(current_positions):
            if code in market_data:
                stock = market_data[code]
                close = stock.get('close', 0)
                ma20 = stock.get('ma20', 0)
                if close < ma20:
                    signals.append({
                        'ts_code': code,
                        'signal': 'SELL',
                        'weight': 0,
                        'reason': '跌破20日均线'
                    })

        return signals