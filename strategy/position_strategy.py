"""
位置判断策略
低位看基本面+消息面，高位看趋势+量价
"""

from typing import List
from .base import BaseStrategy
from config.settings import POSITION_CONFIG, BACKTEST_CONFIG


class PositionStrategy(BaseStrategy):
    """
    位置判断策略

    低位：基本面+消息面逻辑变化 → 买入
    高位：趋势+量价 → 持有/卖出
    中位：因子驱动
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.pos_config = POSITION_CONFIG
        self.max_position_num = self.config.get('max_position_num', BACKTEST_CONFIG['max_position_num'])
        self.max_single_weight = self.config.get('max_single_weight', BACKTEST_CONFIG['max_single_weight'])

    def judge_position(self, stock: dict) -> str:
        """判断股票所处位置"""
        # 低位信号：价格低于均线 + 近期下跌
        low_signals = 0
        low_signals += stock.get('close', 0) < stock.get('ma20', 0)  # 低于20日均线
        low_signals += stock.get('close', 0) < stock.get('ma60', 0)  # 低于60日均线
        low_signals += stock.get('return_20d', 0) < -0.05  # 近20日跌幅超5%
        low_signals += stock.get('return_60d', 0) < -0.10  # 近60日跌幅超10%
        low_signals += stock.get('return_1d', 0) < -0.02  # 今日跌幅超2%

        # 高位信号：价格高于均线 + 近期上涨
        high_signals = 0
        high_signals += stock.get('close', 0) > stock.get('ma20', 0) * 1.05  # 高于20日均线5%
        high_signals += stock.get('close', 0) > stock.get('ma60', 0) * 1.10  # 高于60日均线10%
        high_signals += stock.get('return_20d', 0) > 0.15  # 近20日涨幅超15%
        high_signals += stock.get('return_60d', 0) > 0.30  # 近60日涨幅超30%

        # 降低阈值：满足2个即判断
        if low_signals >= 2:
            return '低位'
        elif high_signals >= 2:
            return '高位'
        return '中位'

    def check_low_position_buy(self, stock: dict) -> dict:
        """低位买入条件检查"""
        reasons = []

        # 基本面逻辑变化
        if stock.get('profit_growth', 0) > self.pos_config['profit_growth_threshold']:
            reasons.append('业绩拐点')
        if stock.get('analyst_upgrade', False):
            reasons.append('分析师上调')
        if stock.get('policy_benefit', False):
            reasons.append('政策利好')
        if stock.get('insider_buying', False):
            reasons.append('内部人增持')
        if stock.get('buyback', False):
            reasons.append('公司回购')

        # 如果没有明确的消息面催化，低位本身就是买入理由
        if len(reasons) == 0:
            reasons.append('低位超跌反弹')

        # 排雷检查
        safe = True
        if stock.get('st_flag', False):
            safe = False
        if stock.get('pledge_ratio', 0) > self.pos_config['pledge_ratio_limit']:
            safe = False

        return {
            'qualified': safe,  # 低位本身就是买入理由
            'reasons': reasons,
            'safe': safe
        }

    def check_high_position_sell(self, stock: dict) -> dict:
        """高位卖出条件检查"""
        reasons = []

        if stock.get('close', 0) < stock.get('ma20', 0):
            reasons.append('跌破MA20')

        vol_ratio = stock.get('volume', 0) / max(stock.get('volume_ma20', 1), 1)
        if vol_ratio > 3.0 and stock.get('price_percentile_1y', 0) > 0.90:
            reasons.append('天量天价')

        if stock.get('main_force_net_3d', 0) < 0:
            reasons.append('主力连续流出')

        return {
            'should_sell': len(reasons) >= 1,
            'reasons': reasons
        }

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals = []
        current_positions = set(portfolio.get('positions', {}).keys())

        for ts_code, stock in market_data.items():
            position = self.judge_position(stock)

            if position == '低位':
                check = self.check_low_position_buy(stock)
                if check['qualified'] and ts_code not in current_positions:
                    if len(current_positions) + len([s for s in signals if s['signal'] == 'BUY']) < self.max_position_num:
                        signals.append({
                            'ts_code': ts_code,
                            'signal': 'BUY',
                            'weight': self.max_single_weight,
                            'reason': f'低位+{",".join(check["reasons"])}'
                        })

            elif position == '高位':
                if ts_code in current_positions:
                    check = self.check_high_position_sell(stock)
                    if check['should_sell']:
                        signals.append({
                            'ts_code': ts_code,
                            'signal': 'SELL',
                            'weight': 0,
                            'reason': f'高位+{",".join(check["reasons"])}'
                        })

        return signals