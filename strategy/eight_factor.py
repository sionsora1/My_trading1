"""
8因子选股策略
改进：增加止损止盈逻辑、更频繁的调仓、修复ts_code匹配问题
"""

from typing import List
from .base import BaseStrategy
from factors.engine import FactorEngine
from config.settings import FACTOR_WEIGHTS, BACKTEST_CONFIG


class EightFactorStrategy(BaseStrategy):
    """
    8因子选股策略

    改进：
    1. 支持每周/每日调仓
    2. 增加止损止盈信号
    3. 增加行业分散约束
    4. 修复ts_code匹配问题
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.factor_engine = FactorEngine()
        self.max_position_num = self.config.get('max_position_num', BACKTEST_CONFIG['max_position_num'])
        self.max_single_weight = self.config.get('max_single_weight', BACKTEST_CONFIG['max_single_weight'])
        self.max_industry_weight = self.config.get('max_industry_weight', BACKTEST_CONFIG.get('max_industry_weight', 0.20))

    def normalize_ts_code(self, ts_code: str) -> str:
        """标准化ts_code格式，去掉.SH/.SZ后缀"""
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    def check_sell_conditions(self, ts_code: str, stock: dict, portfolio: dict) -> dict:
        """检查卖出条件"""
        positions = portfolio.get('positions', {})
        if ts_code not in positions:
            return {'should_sell': False, 'reason': ''}

        pos = positions[ts_code]
        reasons = []

        # 1. 止损（使用配置值）
        stop_loss = BACKTEST_CONFIG.get('stop_loss_rate', -0.08)
        if pos['profit_rate'] < stop_loss:
            reasons.append(f"止损（亏损{pos['profit_rate']:.1%}）")

        # 2. 移动止盈（使用配置值）
        if pos.get('highest_price', 0) > 0:
            drawdown = (stock.get('close', 0) - pos['highest_price']) / pos['highest_price']
            move_stop = BACKTEST_CONFIG.get('move_stop_rate', -0.10)
            if drawdown < move_stop and pos['profit_rate'] > 0:
                reasons.append(f"移动止盈（从最高点回撤{drawdown:.1%}）")

        # 3. 趋势破坏
        if stock.get('close', 0) < stock.get('ma20', 0) and stock.get('ma5', 0) < stock.get('ma20', 0):
            reasons.append("趋势破坏（MA5下穿MA20）")

        # 4. 量价背离
        vol_ratio = stock.get('volume', 0) / max(stock.get('volume_ma20', 1), 1)
        if vol_ratio > 3.0 and stock.get('price_percentile_1y', 0) > 0.90:
            reasons.append("天量天价")

        return {
            'should_sell': len(reasons) > 0,
            'reason': '；'.join(reasons) if reasons else ''
        }

    def generate_signals(self, date: str, market_data: dict, portfolio: dict) -> List[dict]:
        """生成调仓信号"""
        signals = []

        if not market_data:
            return signals

        current_positions = portfolio.get('positions', {})

        # 1. 检查持仓的卖出条件
        for ts_code in list(current_positions.keys()):
            if ts_code in market_data:
                stock = market_data[ts_code]
                sell_check = self.check_sell_conditions(ts_code, stock, portfolio)
                if sell_check['should_sell']:
                    signals.append({
                        'ts_code': ts_code,
                        'signal': 'SELL',
                        'weight': 0,
                        'reason': sell_check['reason']
                    })

        # 2. 计算因子得分
        stocks = list(market_data.values())
        raw_df = self.factor_engine.calculate_raw_factors(stocks)
        factor_scores = self.factor_engine.calculate_factor_score(raw_df)

        if factor_scores.empty:
            return signals

        # 3. 将因子得分的ts_code标准化（去掉.SH/.SZ后缀）
        normalized_scores = {}
        for ts_code, score in factor_scores.items():
            normalized_code = self.normalize_ts_code(ts_code)
            normalized_scores[normalized_code] = score

        # 4. 过滤掉已经触发卖出信号的股票
        sell_codes = set(s['ts_code'] for s in signals if s['signal'] == 'SELL')
        current_hold_codes = set(current_positions.keys()) - sell_codes

        # 5. 按得分排序，选前N只
        sorted_stocks = sorted(normalized_scores.items(), key=lambda x: (-x[1], x[0]))
        target_num = self.max_position_num - len(current_hold_codes)
        target_num = max(0, target_num)

        # 6. 买入信号：选中但不在持仓中
        selected_codes = set()
        for ts_code, score in sorted_stocks:
            if len(selected_codes) >= target_num:
                break
            if ts_code not in current_hold_codes and ts_code not in sell_codes:
                if ts_code in market_data:
                    stock = market_data[ts_code]
                    # 行业分散约束：每个行业最多 max_position_num * 0.6 只
                    max_per_industry = max(1, int(self.max_position_num * 0.6))
                    industry = stock.get('industry', '')
                    industry_count = sum(1 for code in current_hold_codes
                                        if market_data.get(code, {}).get('industry', '') == industry)
                    if industry_count < max_per_industry:
                        selected_codes.add(ts_code)

        for ts_code in selected_codes:
            score = normalized_scores.get(ts_code, 0)
            stock = market_data.get(ts_code, {})
            signals.append({
                'ts_code': ts_code,
                'signal': 'BUY',
                'weight': self.max_single_weight,
                'reason': f'8因子选股（得分{score:.2f}，行业:{stock.get("industry", "")}）'
            })

        # 7. 卖出信号：在持仓中但未选中（且未触发止损止盈）
        for ts_code in current_hold_codes:
            if ts_code not in selected_codes and ts_code not in sell_codes:
                # 检查是否应该卖出（因子排名下降太多）
                if ts_code in normalized_scores:
                    # 计算排名（正确处理同分情况）
                    sorted_items = sorted(normalized_scores.items(), key=lambda x: (-x[1], x[0]))
                    rank = next((i + 1 for i, (code, _) in enumerate(sorted_items) if code == ts_code), len(sorted_items))

                    if rank > self.max_position_num * 1.5:  # 排名下降太多才卖出
                        signals.append({
                            'ts_code': ts_code,
                            'signal': 'SELL',
                            'weight': 0,
                            'reason': f'因子排名下降（排名{rank}）'
                        })

        return signals