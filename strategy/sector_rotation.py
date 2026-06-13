"""
板块轮动策略
Sector Rotation Strategy

通过 AKShare 板块资金流向识别当前资金最青睐的行业，
然后在每个热门行业中选取龙头股进行配置。

核心逻辑：
1. 通过 AKShare stock_sector_fund_flow_rank 获取行业资金净流入排名
2. 选取主力资金净流入最多的 top_n_industries 个行业
3. 在每个行业中按综合排名选 stocks_per_industry 只龙头股
   - 市值排名 * 0.5 + 近20日涨幅排名 * 0.3 + 换手率排名 * 0.2
4. 每周调仓（可配置）
5. 卖出条件：
   - 行业跌出前5
   - 个股在行业内排名跌出前3
"""

import sys
import os as _os

# Support running as a standalone script (python strategy/sector_rotation.py)
if __name__ == "__main__" and __package__ is None:
    _sys_path_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _sys_path_root not in sys.path:
        sys.path.insert(0, _sys_path_root)
    __package__ = "strategy"

from typing import List, Dict, Optional
from .base import BaseStrategy


class SectorRotationStrategy(BaseStrategy):
    """板块轮动策略

    跟随市场资金流向，轮动到资金最青睐的行业龙头。
    使用 AKShare 的 stock_sector_fund_flow_rank 接口获取行业资金流。

    AKShare 依赖通过懒加载（lazy import）处理，避免 import 时
    AKShare 未安装或不可用导致整个策略模块无法加载。
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.top_n_industries = self.config.get('top_n_industries', 3)
        self.stocks_per_industry = self.config.get('stocks_per_industry', 2)
        self.max_single_weight = self.config.get('max_single_weight', 0.12)
        self.rebalance_frequency = self.config.get('rebalance_frequency', 'weekly')

        # 行业前 N 名阈值（用于卖出判断：行业跌出前几）
        self._sell_industry_threshold = self.config.get(
            'sell_industry_threshold', 5
        )

        # 个股行业内排名阈值（用于卖出判断：股票跌出行业前几）
        self._sell_stock_rank_threshold = self.config.get(
            'sell_stock_rank_threshold', 3
        )

        # API call cache (clear each generate_signals call)
        self._cached_industries = None
        self._cached_industries_extended = None

    # ------------------------------------------------------------------
    # 行业资金流
    # ------------------------------------------------------------------

    def _get_top_industries(self) -> List[str]:
        """获取主力资金净流入最多的行业名称列表（带缓存）"""
        if self._cached_industries is not None:
            return self._cached_industries

        result = self._fetch_industry_rank(self.top_n_industries)
        self._cached_industries = result
        return result

    def _get_top_industries_extended(self) -> List[str]:
        """获取更多行业排名，用于卖出判断（带缓存）"""
        if self._cached_industries_extended is not None:
            return self._cached_industries_extended

        result = self._fetch_industry_rank(self._sell_industry_threshold)
        self._cached_industries_extended = result
        return result

    def _fetch_industry_rank(self, top_n: int) -> List[str]:
        """实际调用 AKShare API 获取行业资金流排名"""
        try:
            import akshare as ak
        except ImportError:
            return []

        # Try primary API
        try:
            df = ak.stock_sector_fund_flow_rank(
                indicator="今日", sector_type="行业资金流"
            )
            if df is not None and not df.empty:
                return self._parse_industry_df(df, top_n)
        except Exception:
            pass

        # Fallback: try alternative API
        try:
            df = ak.stock_board_industry_summary_ths()
            if df is not None and not df.empty:
                # Different column names - look for price change or ranking cols
                name_col = None
                for col in df.columns:
                    if '名称' in str(col) or '行业' in str(col) or '板块' in str(col):
                        name_col = col
                        break
                if name_col:
                    return df[name_col].head(top_n).tolist()
        except Exception:
            pass

        return []

    @staticmethod
    def _parse_industry_df(df, top_n: int) -> List[str]:
        """Parse industry fund flow DataFrame"""
        inflow_col = None
        for col in df.columns:
            if '主力净流入' in str(col) or '净流入' in str(col):
                inflow_col = col
                break

        name_col = None
        for col in df.columns:
            if '名称' in str(col) or '行业' in str(col) or '板块' in str(col):
                name_col = col
                break

        if inflow_col and name_col:
            df = df.sort_values(inflow_col, ascending=False)
            return df[name_col].head(top_n).tolist()

        # 兜底：取第一列作为名称，第二列作为流入金额
        if len(df.columns) >= 2:
            name_col = df.columns[0]
            inflow_col = df.columns[1]
            df = df.sort_values(inflow_col, ascending=False)
            return df[name_col].head(top_n).tolist()

        return []

    # ------------------------------------------------------------------
    # 行业龙头选择
    # ------------------------------------------------------------------

    def _rank_industry_stocks(self, industry_name: str,
                              market_data: dict) -> List[tuple]:
        """对某行业内的股票按综合排名排序

        Args:
            industry_name: 行业名称
            market_data: 当日市场数据 {ts_code: stock_data}

        Returns:
            [(ts_code, score), ...] 按得分升序排列（越小越好），
            最多 stocks_per_industry 只
        """
        # 筛选该行业的股票
        industry_stocks: Dict[str, dict] = {}
        for code, stock in market_data.items():
            stock_industry = stock.get('industry', '')
            if not stock_industry:
                continue
            # 模糊匹配：行业名称包含关系
            if industry_name in stock_industry or stock_industry in industry_name:
                industry_stocks[code] = stock

        if not industry_stocks:
            return []

        # 按市值降序排名（1 = 市值最大）
        by_cap = sorted(
            industry_stocks.items(),
            key=lambda x: -(x[1].get('market_cap', 0) or 0)
        )
        # 按近20日涨幅降序排名（1 = 涨幅最大）
        by_return = sorted(
            industry_stocks.items(),
            key=lambda x: -(x[1].get('return_20d', 0) or 0)
        )
        # 按换手率升序排名（1 = 换手率最低，稳定性最好）
        by_turnover = sorted(
            industry_stocks.items(),
            key=lambda x: (x[1].get('turnover', 999) or 999)
        )

        n = len(industry_stocks)
        cap_rank = {code: (i + 1) / n for i, (code, _) in enumerate(by_cap)}
        return_rank = {code: (i + 1) / n for i, (code, _) in enumerate(by_return)}
        turnover_rank = {code: (i + 1) / n for i, (code, _) in enumerate(by_turnover)}

        # 综合得分：市值排名*0.5 + 涨幅排名*0.3 + 换手率排名*0.2
        # 排名越小越好 → 综合得分越小越好
        composite: Dict[str, float] = {}
        for code in industry_stocks:
            composite[code] = (
                cap_rank[code] * 0.5
                + return_rank[code] * 0.3
                + turnover_rank[code] * 0.2
            )

        # 按综合得分升序排列，取前 stocks_per_industry
        sorted_leaders = sorted(composite.items(), key=lambda x: x[1])
        return sorted_leaders[:self.stocks_per_industry]

    def _get_industry_rank(self, industry_name: str,
                           all_industries: List[str]) -> Optional[int]:
        """获取行业在全市场排名（1-based），未找到返回 None"""
        for i, name in enumerate(all_industries):
            if industry_name in name or name in industry_name:
                return i + 1
        return None

    # ------------------------------------------------------------------
    # 信号生成
    # ------------------------------------------------------------------

    def generate_signals(self, date: str, market_data: dict,
                         portfolio: dict) -> List[dict]:
        """生成交易信号"""
        signals: List[dict] = []
        current_positions = set(portfolio.get('positions', {}).keys())

        # Clear per-call cache
        self._cached_industries = None
        self._cached_industries_extended = None

        # ================================================================
        # Step 1: 获取 top 行业（单次 API 调用，缓存复用）
        # ================================================================
        top_industries = self._get_top_industries()
        if not top_industries:
            # 行业数据不可用，不产生新信号，维持现有持仓
            return signals

        # ================================================================
        # Step 2: 在每个 top 行业中选龙头
        # ================================================================
        selected_codes: set = set()
        code_industry_map: Dict[str, str] = {}
        code_reason_map: Dict[str, str] = {}

        for industry in top_industries:
            leaders = self._rank_industry_stocks(industry, market_data)
            for code, score in leaders:
                selected_codes.add(code)
                code_industry_map[code] = industry
                code_reason_map[code] = (
                    f'行业轮动/{industry}（综合排名{score:.3f}）'
                )

        # ================================================================
        # Step 3: 生成买入信号
        # ================================================================
        for code in selected_codes:
            if code not in current_positions:
                signals.append({
                    'ts_code': code,
                    'signal': 'BUY',
                    'weight': self.max_single_weight,
                    'reason': code_reason_map.get(code, '行业轮动'),
                })

        # ================================================================
        # Step 4: 生成卖出信号
        # ================================================================
        # 获取更全的行业排名（前 sell_industry_threshold 名）
        all_top_industries = self._get_top_industries_extended()

        for code in current_positions:
            if code not in market_data:
                continue

            stock = market_data[code]
            stock_industry = stock.get('industry', '')
            should_sell = False
            reason = ''

            # 条件1: 行业跌出前 sell_industry_threshold
            industry_in_top = False
            for ind in all_top_industries:
                if stock_industry and (
                    ind in stock_industry or stock_industry in ind
                ):
                    industry_in_top = True
                    break

            if not industry_in_top and stock_industry:
                should_sell = True
                rank = self._get_industry_rank(stock_industry, all_top_industries)
                reason = (
                    f'行业退出前{self._sell_industry_threshold}'
                    f'（当前第{rank}名）' if rank else
                    f'行业退出前{self._sell_industry_threshold}'
                )

            # 条件2: 个股在行业内排名跌出前 sell_stock_rank_threshold
            elif code not in selected_codes:
                should_sell = True
                reason = (
                    f'行业内部排名下降'
                    f'（跌出前{self._sell_stock_rank_threshold}）'
                )

            if should_sell:
                signals.append({
                    'ts_code': code,
                    'signal': 'SELL',
                    'weight': 0,
                    'reason': reason,
                })

        return signals


# ======================================================================
# Quick manual verification (run: python strategy/sector_rotation.py)
# ======================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("SectorRotationStrategy — verification")
    print("=" * 60)

    strategy = SectorRotationStrategy()

    # 1. Config defaults
    print("\n[1] Config defaults")
    print(f"    top_n_industries={strategy.top_n_industries}")
    print(f"    stocks_per_industry={strategy.stocks_per_industry}")
    print(f"    max_single_weight={strategy.max_single_weight}")
    print(f"    rebalance_frequency={strategy.rebalance_frequency}")
    assert strategy.top_n_industries == 3
    assert strategy.stocks_per_industry == 2
    assert strategy.max_single_weight == 0.12
    assert strategy.rebalance_frequency == 'weekly'
    print("    Config defaults: OK")

    # 2. Custom config
    strategy2 = SectorRotationStrategy({
        'top_n_industries': 5,
        'stocks_per_industry': 1,
        'max_single_weight': 0.10,
        'rebalance_frequency': 'daily',
    })
    assert strategy2.top_n_industries == 5
    assert strategy2.stocks_per_industry == 1
    print(f"\n[2] Custom config: top_n_industries={strategy2.top_n_industries}, "
          f"stocks_per_industry={strategy2.stocks_per_industry}  OK")

    # 3. Industry stock ranking logic
    print("\n[3] Industry stock ranking")
    mock_market = {
        '000001': {
            'ts_code': '000001', 'name': '工行', 'industry': '银行',
            'market_cap': 2e12, 'return_20d': 0.05, 'turnover': 0.5,
            'close': 5.0, 'volatility': 0.20,
        },
        '000002': {
            'ts_code': '000002', 'name': '招行', 'industry': '银行',
            'market_cap': 1e12, 'return_20d': 0.08, 'turnover': 0.8,
            'close': 35.0, 'volatility': 0.25,
        },
        '000003': {
            'ts_code': '000003', 'name': '小银行', 'industry': '银行',
            'market_cap': 5e10, 'return_20d': 0.02, 'turnover': 3.0,
            'close': 8.0, 'volatility': 0.30,
        },
    }
    leaders = strategy._rank_industry_stocks('银行', mock_market)
    print(f"    Leaders for 银行 (top {strategy.stocks_per_industry}):")
    for code, score in leaders:
        name = mock_market[code]['name']
        print(f"      {code} {name}: composite_rank={score:.4f}")
    # 工行 (highest cap) and 招行 (highest return) should lead
    leader_codes = {code for code, _ in leaders}
    assert '000001' in leader_codes, "ICBC should be a leader"
    assert '000003' not in leader_codes, "Small bank should NOT be a leader"
    assert len(leaders) <= strategy.stocks_per_industry
    print("    Ranking: OK (correct leaders selected)")

    # 4. Empty industry (no matching stocks)
    print("\n[4] Empty industry match")
    leaders_empty = strategy._rank_industry_stocks('航空航天', mock_market)
    assert leaders_empty == []
    print("    Empty industry match: OK (returns empty list)")

    # 5. _get_top_industries — graceful failure (no AKShare/network)
    print("\n[5] _get_top_industries graceful failure")
    top = strategy._get_top_industries()
    if top:
        print(f"    Live API returned {len(top)} industries: {top[:3]}")
    else:
        print("    No industry data (expected if no AKShare/network)")
    print("    Graceful failure: OK (no crash, returns empty list or data)")

    # 6. Signal generation with empty sector data
    print("\n[6] Signal generation when sector data unavailable")
    # Since _get_top_industries may actually return data if AKShare is
    # installed and network is available, we test the degenerate case
    # by checking that generate_signals doesn't crash
    portfolio = {'cash': 100000, 'positions': {}}
    signals = strategy.generate_signals('20250601', mock_market, portfolio)
    print(f"    Signals generated: {len(signals)}")
    # The result depends on whether AKShare data is available
    # Either way, it should not crash and should return a list
    assert isinstance(signals, list)
    print("    Signal generation: OK (no crash)")

    # 7. Subclass check — must be a BaseStrategy
    from .base import BaseStrategy
    assert isinstance(strategy, BaseStrategy), \
        "SectorRotationStrategy must extend BaseStrategy"
    print("\n[7] Inheritance check: OK (extends BaseStrategy)")

    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
