"""
AI 因子挖掘策略
用 LightGBM 模型预测替代传统线性因子打分

和八因子策略的区别：
- 八因子：人工定义8个因子 → 线性加权 → 得分 → 排名
- AI因子：50+特征 → LightGBM非线性模型 → 预测收益率 → 排名

用法：
1. 先运行 train_ai_model.py 训练模型
2. 在回测中指定 'ai_factor' 策略
"""

from typing import List
import sys
import os

from .base import BaseStrategy
from config.settings import FACTOR_WEIGHTS, BACKTEST_CONFIG


class AIStrategy(BaseStrategy):
    """
    AI 因子策略

    核心流程：
    1. 用训练好的 LightGBM 模型对每只股票打分
    2. 按得分排序选前 N 只
    3. 同样的止损止盈逻辑
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.max_position_num = self.config.get('max_position_num', BACKTEST_CONFIG['max_position_num'])
        self.max_single_weight = self.config.get('max_single_weight', BACKTEST_CONFIG['max_single_weight'])
        self.max_industry_weight = self.config.get('max_industry_weight', BACKTEST_CONFIG.get('max_industry_weight', 0.20))

        # 延迟加载 AI 引擎（避免导入时就加载模型）
        self._ai_engine = None

    @property
    def ai_engine(self):
        """延迟加载 AI 引擎（首次使用时加载模型）"""
        if self._ai_engine is None:
            from factors.ai_engine import AIFactorEngine
            self._ai_engine = AIFactorEngine()
            model_name = self.config.get('ai_model', 'ai_factor_model')
            try:
                self._ai_engine.load(model_name)
                print(f"[AIStrategy] [OK] AI model loaded: {model_name}")
            except FileNotFoundError:
                print(f"[AIStrategy] [WARN] Model file not found: models/{model_name}.pkl")
                print(f"[AIStrategy]    请先运行 train_ai_model.py 训练模型")
                print(f"[AIStrategy]    将回退到八因子策略")
                self._ai_engine = None
        return self._ai_engine

    def normalize_ts_code(self, ts_code: str) -> str:
        """标准化ts_code格式"""
        return ts_code.split('.')[0] if '.' in ts_code else ts_code

    def check_sell_conditions(self, ts_code: str, stock: dict, portfolio: dict) -> dict:
        """检查卖出条件（和八因子相同）"""
        positions = portfolio.get('positions', {})
        if ts_code not in positions:
            return {'should_sell': False, 'reason': ''}

        pos = positions[ts_code]
        reasons = []

        # 止损
        stop_loss = BACKTEST_CONFIG.get('stop_loss_rate', -0.08)
        if pos['profit_rate'] < stop_loss:
            reasons.append(f"止损（亏损{pos['profit_rate']:.1%}）")

        # 移动止盈
        if pos.get('highest_price', 0) > 0:
            drawdown = (stock.get('close', 0) - pos['highest_price']) / pos['highest_price']
            move_stop = BACKTEST_CONFIG.get('move_stop_rate', -0.10)
            if drawdown < move_stop and pos['profit_rate'] > 0:
                reasons.append(f"移动止盈（从最高点回撤{drawdown:.1%}）")

        # 趋势破坏
        if stock.get('close', 0) < stock.get('ma20', 0) and stock.get('ma5', 0) < stock.get('ma20', 0):
            reasons.append("趋势破坏（MA5下穿MA20）")

        # 天量天价
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

        # 2. AI 打分
        if self.ai_engine is None:
            # 回退到八因子
            from factors.engine import FactorEngine
            fe = FactorEngine()
            stocks = list(market_data.values())
            raw_df = fe.calculate_raw_factors(stocks)
            scores = fe.calculate_factor_score(raw_df)
            score_label = '8因子'
        else:
            try:
                scores = self.ai_engine.predict(market_data)
                score_label = 'AI'
            except Exception as e:
                print(f"[AIStrategy] AI 预测失败: {e}，回退到八因子")
                from factors.engine import FactorEngine
                fe = FactorEngine()
                stocks = list(market_data.values())
                raw_df = fe.calculate_raw_factors(stocks)
                scores = fe.calculate_factor_score(raw_df)
                score_label = '8因子(回退)'

        if scores.empty:
            return signals

        # 3. 标准化 ts_code
        normalized_scores = {}
        for ts_code, score in scores.items():
            normalized_code = self.normalize_ts_code(ts_code)
            normalized_scores[normalized_code] = score

        # 4. 过滤已触发卖出的
        sell_codes = set(s['ts_code'] for s in signals if s['signal'] == 'SELL')
        current_hold_codes = set(current_positions.keys()) - sell_codes

        # 5. 按得分排序选前 N 只
        sorted_stocks = sorted(normalized_scores.items(), key=lambda x: (-x[1], x[0]))
        target_num = self.max_position_num - len(current_hold_codes)
        target_num = max(0, target_num)

        # 6. 买入信号
        selected_codes = set()
        for ts_code, score in sorted_stocks:
            if len(selected_codes) >= target_num:
                break
            if ts_code not in current_hold_codes and ts_code not in sell_codes:
                if ts_code in market_data:
                    stock = market_data[ts_code]
                    # 行业分散
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
                'reason': f'AI选股（{score_label}得分{score:.4f}，行业:{stock.get("industry", "")}）'
            })

        # 7. 排名下降卖出
        for ts_code in current_hold_codes:
            if ts_code not in selected_codes and ts_code not in sell_codes:
                if ts_code in normalized_scores:
                    sorted_items = sorted(normalized_scores.items(), key=lambda x: (-x[1], x[0]))
                    rank = next((i + 1 for i, (code, _) in enumerate(sorted_items) if code == ts_code), len(sorted_items))
                    if rank > self.max_position_num * 1.5:
                        signals.append({
                            'ts_code': ts_code,
                            'signal': 'SELL',
                            'weight': 0,
                            'reason': f'AI排名下降（排名{rank}）'
                        })

        return signals
