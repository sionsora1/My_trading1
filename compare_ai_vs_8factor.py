"""
AI vs 8因子 策略对比回测
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine, BacktestConfig
from strategy.eight_factor import EightFactorStrategy
from strategy.ai_strategy import AIStrategy
from data.fetcher import DataFetcher, DataCache
from config.settings import BACKTEST_CONFIG

# 默认股票池（和 run_backtest.py 一致）
STOCK_POOL = [
    # 大盘蓝筹（3只）
    '600519',  # 贵州茅台 - 白酒龙头
    '000858',  # 五粮液 - 白酒
    '601398',  # 工商银行 - 银行
    # 中盘成长（5只）
    '002415',  # 海康威视 - 科技
    '002230',  # 科大讯飞 - AI
    '300015',  # 爱尔眼科 - 医药
    '002304',  # 洋河股份 - 白酒
    '300274',  # 阳光电源 - 新能源
    # 小盘高alpha（7只）
    '002352',  # 顺丰控股 - 物流
    '002371',  # 北方华创 - 半导体
    '002382',  # 蓝帆医疗 - 医疗器械
    '002399',  # 海普瑞 - 医药
    '002400',  # 省广集团 - 传媒
    '002410',  # 广联达 - 软件
    '002421',  # 达实智能 - 智能建筑
]

# 回测参数
START = '20250901'
END = '20260611'
MAX_STOCKS = 15
INITIAL_CAPITAL = 1_000_000

def main():
    print("=" * 70)
    print("AI因子 vs 八因子 — 策略对比回测")
    print("=" * 70)
    print(f"  股票池: {MAX_STOCKS} 只")
    print(f"  区间:   {START} ~ {END}")
    print(f"  初始资金: {INITIAL_CAPITAL:,.0f}")
    print("=" * 70)

    # ── 获取数据 ──
    print("\n[Step 1] 获取历史数据...")
    fetcher = DataFetcher()
    cache = DataCache()
    cache_file = f'market_data_{START}_{END}_{MAX_STOCKS}stocks'
    market_data = cache.load_market_data(cache_file)

    if market_data:
        print(f"  从缓存加载 {len(market_data)} 个交易日")
    else:
        print(f"  从 AKShare 获取...")
        market_data = fetcher.build_market_data_by_date(STOCK_POOL[:MAX_STOCKS], START, END)
        if market_data:
            cache.save_market_data(market_data, cache_file)

    if not market_data:
        print("[ERROR] 获取数据失败")
        return

    print(f"  [OK] {len(market_data)} 个交易日")

    # ── 配置 ──
    config = BacktestConfig.from_dict({
        **BACKTEST_CONFIG,
        'initial_capital': INITIAL_CAPITAL,
        'start_date': START,
        'end_date': END,
        'max_position_num': 5,
        'rebalance_frequency': 'weekly',
    })

    results = {}

    # ── 八因子回测 ──
    print("\n" + "=" * 70)
    print("八因子策略回测")
    print("=" * 70)
    engine_ef = BacktestEngine(config)
    strategy_ef = EightFactorStrategy()
    result_ef = engine_ef.run(market_data, strategy_ef, print_report=False)
    results['8factor'] = result_ef

    # ── AI 因子回测 ──
    print("\n" + "=" * 70)
    print("AI 因子策略回测")
    print("=" * 70)
    engine_ai = BacktestEngine(config)
    strategy_ai = AIStrategy()
    result_ai = engine_ai.run(market_data, strategy_ai, print_report=False)
    results['ai'] = result_ai

    # ── 对比 ──
    print("\n" + "=" * 70)
    print("策略对比")
    print("=" * 70)

    metrics = {
        '8factor': results['8factor']['metrics'],
        'ai': results['ai']['metrics'],
    }

    print(f"\n{'指标':<16} {'八因子':>14} {'AI因子':>14} {'优劣':>10}")
    print("-" * 58)

    comparisons = [
        ('total_return',   '总收益率',   '{:.2%}', True),
        ('annual_return',  '年化收益',   '{:.2%}', True),
        ('max_drawdown',   '最大回撤',   '{:.2%}', False),
        ('sharpe_ratio',   '夏普比率',   '{:.2f}', True),
        ('trade_win_rate', '交易胜率',   '{:.2%}', True),
        ('total_trades',   '总交易次数', '{}',     True),
    ]

    for key, label, fmt, higher_better in comparisons:
        v8 = metrics['8factor'].get(key, 0)
        va = metrics['ai'].get(key, 0)

        # 判断优劣
        if higher_better:
            winner = 'AI' if va > v8 else '8F' if v8 > va else '--'
        else:
            winner = 'AI' if va < v8 else '8F' if v8 < va else '--'

        print(f"{label:<16} {fmt.format(v8):>14} {fmt.format(va):>14} {winner:>10}")

    # ── 详细指标 ──
    print(f"\n{'='*70}")
    print("详细对比")
    print(f"{'='*70}")

    for name in ['8factor', 'ai']:
        m = metrics[name]
        label = '八因子' if name == '8factor' else 'AI因子'
        print(f"\n--- {label} ---")
        print(f"  初始资金:    {m.get('initial_capital', 0):,.0f}")
        print(f"  最终资产:    {m.get('final_value', 0):,.0f}")
        print(f"  总收益率:    {m.get('total_return', 0):.2%}")
        print(f"  年化收益:    {m.get('annual_return', 0):.2%}")
        print(f"  最大回撤:    {m.get('max_drawdown', 0):.2%}")
        print(f"  夏普比率:    {m.get('sharpe_ratio', 0):.2f}")
        print(f"  交易胜率:    {m.get('trade_win_rate', 0):.2%}")
        print(f"  总交易次数:  {m.get('total_trades', 0)}")
        print(f"  日均换手率:  {m.get('avg_daily_turnover', 0):.2%}")
        print(f"  最大连续亏损:{m.get('max_consecutive_loss', 0)}")

    # ── 导出结果 ──
    print(f"\n{'='*70}")
    print("导出报告...")
    for name in ['8factor', 'ai']:
        engine = engine_ef if name == '8factor' else engine_ai
        engine.export_daily_report(f'compare_{name}_trades.csv')
        print(f"  compare_{name}_trades.csv")

    print(f"\n[OK] 对比完成!")


if __name__ == '__main__':
    main()
