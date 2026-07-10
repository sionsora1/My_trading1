"""
ETF行情轮动策略 — 单元测试
覆盖 spec 第 8 节全部边界场景
"""
import pytest
from strategy.etf_rotation import EtfRotationStrategy


# ------------------------------------------------------------------
# Test helpers
# ------------------------------------------------------------------

def _make_market(close_ma_pairs, return_20d_map):
    """构建 mock market_data。

    close_ma_pairs: [(close, ma28), ...] 按 etf_codes 顺序
    return_20d_map: {code: return_20d}  只提供需要的，其余默认 0
    """
    codes = ['510300', '159915', '518880', '159941', '513330', '511260']
    data = {}
    for i, code in enumerate(codes):
        close, ma28 = close_ma_pairs[i]
        ret = return_20d_map.get(code, 0.0)
        data[code] = {
            'ts_code': code, 'close': close, 'ma28': ma28,
            'return_20d': ret, 'name': f'ETF_{code}',
        }
    return data


def _make_portfolio(positions=None, cash=100000):
    """构建 mock portfolio"""
    return {'cash': cash, 'positions': positions or {}}


def _pos(code):
    """构建一个简化的持仓条目"""
    return {code: {'ts_code': code, 'quantity': 1000, 'cost_price': 1.0,
                   'current_price': 1.0, 'profit_rate': 0.0}}


# ------------------------------------------------------------------
# Test cases
# ------------------------------------------------------------------

class TestEtfRotationInitialEntry:
    """初始建仓场景"""

    def test_buy_top_when_no_position(self):
        """无持仓，合格池非空 → 买入排名第1"""
        s = EtfRotationStrategy()
        # 全部在 MA28 上方，510300 涨幅最高
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (2.0, 1.8), (100, 95)],
            {'510300': 0.08, '159915': 0.03, '518880': 0.02,
             '159941': 0.05, '513330': -0.01, '511260': 0.01},
        )
        # 排名：510300(+8%), 159941(+5%), 159915(+3%), 518880(+2%), 511260(+1%), 513330(-1%)
        # 合格池全部，第1 = 510300
        signals = s.generate_signals('20260710', market, _make_portfolio())
        buys = [sig for sig in signals if sig['signal'] == 'BUY']
        assert len(buys) == 1
        assert buys[0]['ts_code'] == '510300'
        assert buys[0]['weight'] == 1.0
        assert '建仓' in buys[0]['reason']

    def test_all_below_ma28_no_buy(self):
        """合格池为空 → 无信号"""
        s = EtfRotationStrategy()
        # 全部跌破 MA28
        market = _make_market(
            [(3.0, 3.5), (2.0, 2.5), (3.8, 4.0), (1.2, 1.5), (1.8, 2.0), (95, 100)],
            {'510300': 0.08},
        )
        signals = s.generate_signals('20260710', market, _make_portfolio())
        assert signals == []

    def test_only_one_qualified_buy_it(self):
        """只有1只合格 → 买入该ETF（即使涨幅不是整体最高）"""
        s = EtfRotationStrategy()
        # 只有 510300 在 MA28 上方，其他都跌破
        market = _make_market(
            [(3.5, 3.0), (2.0, 2.5), (3.8, 4.0), (1.2, 1.5), (1.8, 2.0), (95, 100)],
            {'510300': 0.02, '518880': 0.15},  # 518880 涨幅最高但跌破 MA28
        )
        signals = s.generate_signals('20260710', market, _make_portfolio())
        buys = [sig for sig in signals if sig['signal'] == 'BUY']
        assert len(buys) == 1
        assert buys[0]['ts_code'] == '510300'  # 唯一合格


class TestEtfRotationHold:
    """继续持有场景"""

    def test_hold_when_still_number_one(self):
        """持仓仍是合格池#1 → 无信号"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (2.0, 1.8), (100, 95)],
            {'510300': 0.08, '159915': 0.03, '518880': 0.02,
             '159941': 0.05, '513330': -0.01, '511260': 0.01},
        )
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('510300')))
        assert signals == []

    def test_hold_when_only_one_qualified(self):
        """只有1只合格且已持有 → 无信号"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.5, 3.0), (2.0, 2.5), (3.8, 4.0), (1.2, 1.5), (1.8, 2.0), (95, 100)],
            {'510300': 0.02},
        )
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('510300')))
        assert signals == []


class TestEtfRotationSell:
    """卖出/切换场景"""

    def test_sell_when_below_ma28(self):
        """持仓跌破 MA28 → 无条件卖出，切换到合格池#1"""
        s = EtfRotationStrategy()
        # 513330 已持有，但跌破 MA28；合格池另有 510300
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (1.0, 1.5), (100, 95)],
            {'510300': 0.08, '159915': 0.03, '513330': 0.06},
        )
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('513330')))
        sells = [s for s in signals if s['signal'] == 'SELL']
        buys = [s for s in signals if s['signal'] == 'BUY']
        assert len(sells) == 1
        assert sells[0]['ts_code'] == '513330'
        assert 'MA28' in sells[0]['reason'] or '均线' in sells[0]['reason']
        assert len(buys) == 1
        assert buys[0]['ts_code'] == '510300'

    def test_sell_when_below_ma28_all_empty(self):
        """持仓跌破 MA28 且合格池为空 → 清仓不买入"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.0, 3.5), (2.0, 2.5), (3.8, 4.0), (1.2, 1.5), (1.0, 1.5), (95, 100)],
            {'513330': 0.06},
        )
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('513330')))
        sells = [s for s in signals if s['signal'] == 'SELL']
        buys = [s for s in signals if s['signal'] == 'BUY']
        assert len(sells) == 1
        assert sells[0]['ts_code'] == '513330'
        assert len(buys) == 0  # 没有可买入的

    def test_switch_when_rank_drops_with_buffer(self):
        """持仓排名降至#2且差值>1% → 切换"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (2.0, 1.8), (100, 95)],
            {'510300': 0.10, '159915': 0.08},  # 510300 > 159915 by 2% > 1%
        )
        # 持有 159915（排名第2，落后 510300 2%）
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('159915')))
        sells = [s for s in signals if s['signal'] == 'SELL']
        buys = [s for s in signals if s['signal'] == 'BUY']
        assert len(sells) == 1
        assert sells[0]['ts_code'] == '159915'
        assert len(buys) == 1
        assert buys[0]['ts_code'] == '510300'

    def test_no_switch_when_buffer_not_exceeded(self):
        """持仓排名降至#2但差值≤1% → 不切换"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (2.0, 1.8), (100, 95)],
            {'510300': 0.085, '159915': 0.08},  # 差值 0.5% < 1%
        )
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('159915')))
        assert signals == []

    def test_sell_when_holding_not_qualified_even_if_return_high(self):
        """持仓跌破MA28 → 必须卖出，即使涨幅仍然最高"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (1.0, 1.5), (100, 95)],
            {'513330': 0.15, '510300': 0.08},  # 513330 涨幅最高但跌破MA28
        )
        signals = s.generate_signals('20260710', market, _make_portfolio(_pos('513330')))
        sells = [s for s in signals if s['signal'] == 'SELL']
        assert len(sells) == 1
        assert sells[0]['ts_code'] == '513330'


class TestEtfRotationEdgeCases:
    """其他边界场景"""

    def test_missing_data_skipped(self):
        """某 ETF 无数据 → 跳过"""
        s = EtfRotationStrategy()
        market = _make_market(
            [(3.5, 3.0), (2.5, 2.0), (4.0, 3.5), (1.5, 1.2), (2.0, 1.8), (100, 95)],
            {'510300': 0.08, '159915': 0.03},
        )
        # 删除 518880 的数据
        del market['518880']
        # 不应崩溃，在剩余 5 只中正常选出 510300
        signals = s.generate_signals('20260710', market, _make_portfolio())
        buys = [sig for sig in signals if sig['signal'] == 'BUY']
        assert len(buys) == 1
        assert buys[0]['ts_code'] == '510300'

    def test_missing_ma28_falls_back_to_close(self):
        """ETF 缺少 ma28 字段 → 回退到 close（视为刚好站上均线）"""
        s = EtfRotationStrategy()
        market = {
            '510300': {'ts_code': '510300', 'close': 3.5, 'return_20d': 0.08, 'name': 'HS300'},
            '159915': {'ts_code': '159915', 'close': 2.5, 'return_20d': 0.03, 'name': 'CY'},
            # 没有 ma28 字段
        }
        # 不应崩溃，close==ma28 → 视为合格（close > ma28 为 False，不买入）
        # close 不小于 ma28（相等），但 close 也不大于 ma28，所以不合格
        # 两只都不合格 → 无信号
        signals = s.generate_signals('20260710', market, _make_portfolio())
        assert signals == []
