"""
低波动防御策略（增强版）
Enhanced Low Volatility Strategy — DEFENSIVE

适用场景：熊市 / 高波动 / 崩盘市场
核心理念：在市场恐慌时持有低波动、高质量、估值合理的股票来防御

选股逻辑：
1. 过滤：波动率 < max_volatility（默认0.35），且 price_percentile_1y > 0.3（不在极端低位）
2. 评分（0-1）：
   - 低波动: (1 - volatility/0.5) * 0.4   — 偏好低波动
   - 质量:   roe * 0.3                     — 偏好盈利公司
   - 估值:   ep (1/PE) * 0.2               — 偏好便宜
   - 稳定性: (1 - abs(return_20d)) * 0.1   — 偏好价格稳定
3. 按总分选 top_N
4. 卖出条件：
   - 波动率飙升超过 volatility_spike（默认0.45）
   - 得分跌出 top_N
"""

import sys
import os as _os

# Support running as a standalone script (python strategy/low_volatility.py)
if __name__ == "__main__" and __package__ is None:
    _sys_path_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _sys_path_root not in sys.path:
        sys.path.insert(0, _sys_path_root)
    __package__ = "strategy"

from typing import List, Dict
from .base import BaseStrategy


class EnhancedLowVolatilityStrategy(BaseStrategy):
    """增强低波动防御策略

    在已有 LowVolatilityStrategy（仅按波动率排序）基础上增加：
    - 多因子评分（质量 + 估值 + 稳定性）
    - 波动率过滤器
    - 价格分位数过滤器（避免接飞刀）
    - 波动率飙升卖出
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n = self.config.get('top_n', 5)
        self.max_single_weight = self.config.get('max_single_weight', 0.12)
        self.max_volatility = self.config.get('max_volatility', 0.35)
        self.volatility_spike = self.config.get('volatility_spike', 0.45)
        self.min_price_percentile = self.config.get('min_price_percentile', 0.3)

    def _score_stock(self, stock: dict) -> float:
        """对单只股票进行防御性评分

        Returns:
            0-1 的综合得分，分数越高越适合防御性持有
        """
        volatility = stock.get('volatility', 0.5)
        roe = stock.get('roe', 0)
        ep = stock.get('ep', 0.05)
        return_20d = stock.get('return_20d', 0)

        # 低波动得分: 波动率越低分越高
        # vol=0.1 -> (1-0.2)*0.4 = 0.32; vol=0.35 -> (1-0.7)*0.4 = 0.12
        low_vol_score = max(0, (1 - volatility / 0.5)) * 0.4

        # 质量得分: ROE 越高越好
        quality_score = min(max(roe, 0), 0.5) * 0.3

        # 估值得分: EP 越高（PE越低）越好
        valuation_score = min(max(ep, 0), 0.25) * 0.2

        # 稳定性得分: 近期涨幅越接近0越稳定
        stability_score = max(0, (1 - min(abs(return_20d), 1))) * 0.1

        return low_vol_score + quality_score + valuation_score + stability_score

    def generate_signals(self, date: str, market_data: dict,
                         portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals: List[dict] = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # ================================================================
        # Step 1: 过滤 + 评分
        # ================================================================
        candidates: Dict[str, dict] = {}

        for code, stock in market_data.items():
            volatility = stock.get('volatility', 0.5)
            price_percentile = stock.get('price_percentile_1y', 0.5)

            # 过滤器
            if volatility >= self.max_volatility:
                continue
            if price_percentile <= self.min_price_percentile:
                continue

            score = self._score_stock(stock)
            candidates[code] = {
                'score': score,
                'volatility': volatility,
                'name': stock.get('name', code),
            }

        # ================================================================
        # Step 2: 按得分降序排序，选 top_N
        # ================================================================
        sorted_candidates = sorted(
            candidates.items(),
            key=lambda x: (-x[1]['score'], x[0])
        )
        selected_codes = set(
            code for code, _ in sorted_candidates[:self.top_n]
        )

        # ================================================================
        # Step 3: 生成买入信号
        # ================================================================
        for code in selected_codes:
            if code not in current_positions:
                info = candidates[code]
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': (
                        f'低波动防御（得分{info["score"]:.3f}，'
                        f'波动率{info["volatility"]:.2%}）'
                    ),
                })

        # ================================================================
        # Step 4: 生成卖出信号
        # ================================================================
        for code in current_positions:
            if code not in market_data:
                continue

            stock = market_data[code]
            volatility = stock.get('volatility', 0.5)
            should_sell = False
            reason = ''

            # 条件1: 波动率飙升
            if volatility > self.volatility_spike:
                should_sell = True
                reason = (
                    f'波动率飙升（{volatility:.2%} > '
                    f'{self.volatility_spike:.2%}）'
                )

            # 条件2: 得分跌出 top_N
            elif code not in selected_codes:
                should_sell = True
                info = candidates.get(code, {})
                score = info.get('score', 0)
                reason = f'防御得分下降（{score:.3f}，未进入前{self.top_n}）'

            if should_sell:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': reason,
                })

        return signals


# ======================================================================
# Quick manual verification (run: python strategy/low_volatility.py)
# ======================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("EnhancedLowVolatilityStrategy — verification")
    print("=" * 60)

    strategy = EnhancedLowVolatilityStrategy()

    # 1. Config defaults
    print("\n[1] Config defaults")
    print(f"    top_n={strategy.top_n}, max_single_weight={strategy.max_single_weight}")
    print(f"    max_volatility={strategy.max_volatility}, "
          f"volatility_spike={strategy.volatility_spike}")
    assert strategy.top_n == 5
    assert strategy.max_single_weight == 0.12
    assert strategy.max_volatility == 0.35
    assert strategy.volatility_spike == 0.45
    print("    Config defaults: OK")

    # 2. Custom config
    strategy2 = EnhancedLowVolatilityStrategy({
        'top_n': 8, 'max_volatility': 0.30, 'volatility_spike': 0.40
    })
    assert strategy2.top_n == 8
    assert strategy2.max_volatility == 0.30
    print(f"\n[2] Custom config: top_n={strategy2.top_n}, "
          f"max_volatility={strategy2.max_volatility}  OK")

    # 3. Scoring logic
    print("\n[3] Scoring logic")
    stock_a = {'volatility': 0.20, 'roe': 0.15, 'ep': 0.10, 'return_20d': 0.02}
    stock_b = {'volatility': 0.50, 'roe': 0.05, 'ep': 0.03, 'return_20d': -0.15}
    stock_c = {'volatility': 0.10, 'roe': 0.25, 'ep': 0.08, 'return_20d': 0.01}

    score_a = strategy._score_stock(stock_a)
    score_b = strategy._score_stock(stock_b)
    score_c = strategy._score_stock(stock_c)
    print(f"    Stock A (mid vol, ok quality): score={score_a:.4f}")
    print(f"    Stock B (high vol, poor quality): score={score_b:.4f}")
    print(f"    Stock C (low vol, great quality): score={score_c:.4f}")
    assert score_c > score_a > score_b, \
        f"Expected C > A > B, got {score_c:.4f} > {score_a:.4f} > {score_b:.4f}"
    print("    Scoring: OK (C > A > B)")

    # 4. Filter logic — high volatility should be excluded
    print("\n[4] Filter logic")
    mock_market = {
        '000001': {'ts_code': '000001', 'name': '平安银行',
                   'volatility': 0.20, 'roe': 0.12, 'ep': 0.09,
                   'return_20d': 0.03, 'price_percentile_1y': 0.55,
                   'close': 10.5, 'ma20': 10.0, 'volume': 1e7},
        '000002': {'ts_code': '000002', 'name': '万科A',
                   'volatility': 0.50, 'roe': 0.08, 'ep': 0.06,
                   'return_20d': -0.10, 'price_percentile_1y': 0.25,
                   'close': 15.0, 'ma20': 16.0, 'volume': 2e7},
        '000003': {'ts_code': '000003', 'name': '招行',
                   'volatility': 0.15, 'roe': 0.18, 'ep': 0.12,
                   'return_20d': 0.01, 'price_percentile_1y': 0.60,
                   'close': 35.0, 'ma20': 34.0, 'volume': 5e6},
        '000004': {'ts_code': '000004', 'name': '低分位股',
                   'volatility': 0.25, 'roe': 0.10, 'ep': 0.08,
                   'return_20d': 0.05, 'price_percentile_1y': 0.15,
                   'close': 5.0, 'ma20': 6.0, 'volume': 1e7},
    }
    portfolio = {'cash': 100000, 'positions': {}}

    signals = strategy.generate_signals('20250115', mock_market, portfolio)
    buys = [s for s in signals if s['signal'] == 'BUY']
    sells = [s for s in signals if s['signal'] == 'SELL']

    print(f"    BUY signals: {len(buys)}")
    for s in buys:
        print(f"      {s['ts_code']}: {s['reason']}")
    print(f"    SELL signals: {len(sells)}")

    # 000002 should be filtered (volatility 0.50 > 0.35)
    buy_codes = {s['ts_code'] for s in buys}
    assert '000002' not in buy_codes, "000002 (high vol) should be filtered out"
    # 000004 should be filtered (price_percentile 0.15 < 0.3)
    assert '000004' not in buy_codes, "000004 (low percentile) should be filtered out"
    # 000003 should be top pick (lowest vol, highest ROE)
    assert '000003' in buy_codes, "000003 should be selected"
    print("    Filter: OK (000002 and 000004 correctly filtered out)")

    # 5. Sell on volatility spike
    print("\n[5] Sell on volatility spike")
    portfolio_with_pos = {
        'cash': 80000,
        'positions': {
            '000001': {'ts_code': '000001', 'quantity': 1000,
                       'cost_price': 10.0, 'profit_rate': 0.05,
                       'highest_price': 11.0}
        }
    }
    mock_market_spike = {
        '000001': {'ts_code': '000001', 'name': '平安银行',
                   'volatility': 0.50, 'roe': 0.12, 'ep': 0.09,
                   'return_20d': -0.15, 'price_percentile_1y': 0.40,
                   'close': 9.0, 'ma20': 10.0, 'volume': 2e7},
    }
    signals2 = strategy.generate_signals('20250201', mock_market_spike,
                                          portfolio_with_pos)
    sell_sigs = [s for s in signals2 if s['signal'] == 'SELL']
    assert len(sell_sigs) >= 1, "Should trigger sell on volatility spike"
    assert 'volatility' in sell_sigs[0]['reason'].lower() or \
           '波动率飙升' in sell_sigs[0]['reason'], \
        f"Reason should mention volatility: {sell_sigs[0]['reason']}"
    print(f"    SELL reason: {sell_sigs[0]['reason']}  OK")

    # 6. Empty market data
    print("\n[6] Empty market data")
    signals3 = strategy.generate_signals('20250301', {}, {'cash': 100000, 'positions': {}})
    assert signals3 == []
    print("    Empty market: OK (returns empty list)")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
