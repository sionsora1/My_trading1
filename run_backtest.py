"""
回测运行脚本
使用AKShare真实数据（免费，无需注册）
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine, BacktestConfig
from strategy.eight_factor import EightFactorStrategy
from strategy.position_strategy import PositionStrategy
from data.fetcher import DataFetcher, DataCache
from config.settings import BACKTEST_CONFIG


# 默认股票池（混合大小盘，更适合8因子策略）
# 包含：大盘蓝筹 + 中盘成长 + 小盘高alpha股
DEFAULT_STOCK_POOL = [
    # 大盘蓝筹（3只）- 压舱石
    '600519',  # 贵州茅台 - 白酒龙头
    '000858',  # 五粮液 - 白酒
    '601398',  # 工商银行 - 银行
    # 中盘成长（5只）- 主力仓位
    '002415',  # 海康威视 - 科技
    '002230',  # 科大讯飞 - AI
    '300015',  # 爱尔眼科 - 医药
    '002304',  # 洋河股份 - 白酒
    '300274',  # 阳光电源 - 新能源
    # 小盘高alpha（7只）- 8因子策略重点
    '002352',  # 顺丰控股 - 物流
    '002371',  # 北方华创 - 半导体
    '002382',  # 蓝帆医疗 - 医疗器械
    '002399',  # 海普瑞 - 医药
    '002400',  # 省广集团 - 传媒
    '002410',  # 广联达 - 软件
    '002421',  # 达实智能 - 智能建筑
]


def run_backtest(stock_pool=None, start_date='20230101', end_date='20231231', max_stocks=40):
    """
    运行回测

    Args:
        stock_pool: 股票代码列表，None则使用默认池
        start_date: 回测开始日期
        end_date: 回测结束日期
        max_stocks: 最大股票数量
    """
    print("=" * 60)
    print("A股量化回测系统")
    print("数据源: AKShare（免费，无需注册）")
    print("=" * 60)

    if stock_pool is None:
        stock_pool = DEFAULT_STOCK_POOL[:max_stocks]

    # 1. 获取数据
    print(f"\n[1] 获取历史数据...")
    print(f"    股票池: {len(stock_pool)} 只股票")
    print(f"    区间: {start_date} ~ {end_date}")
    print(f"    （AKShare免费接口，请耐心等待...）")

    fetcher = DataFetcher()
    cache = DataCache()

    # 尝试加载缓存
    cache_filename = f'market_data_{start_date}_{end_date}_{len(stock_pool)}stocks'
    market_data = cache.load_market_data(cache_filename)

    if market_data and isinstance(market_data, dict) and len(market_data) > 100:
        print(f"    从缓存加载 {len(market_data)} 个交易日数据")
    else:
        print(f"    缓存不存在或不完整，从AKShare获取...")
        market_data = fetcher.build_market_data_by_date(stock_pool, start_date, end_date)

        if market_data and len(market_data) > 0:
            cache.save_market_data(market_data, cache_filename)
            print(f"    数据已缓存，下次运行将更快")

    if not market_data or len(market_data) == 0:
        print("[错误] 获取数据失败，请检查网络连接")
        return None, None

    print(f"    成功获取 {len(market_data)} 个交易日数据")

    # 2. 配置回测
    config = BacktestConfig.from_dict(BACKTEST_CONFIG)

    # 3. 运行8因子策略
    print("\n" + "=" * 60)
    print("[2] 运行8因子选股策略回测")
    print("=" * 60)

    engine1 = BacktestEngine(config)
    strategy1 = EightFactorStrategy()
    result1 = engine1.run(market_data, strategy1)

    # 4. 运行位置判断策略
    print("\n" + "=" * 60)
    print("[3] 运行位置判断策略回测")
    print("=" * 60)

    engine2 = BacktestEngine(config)
    strategy2 = PositionStrategy()
    result2 = engine2.run(market_data, strategy2)

    # 5. 策略对比
    print("\n" + "=" * 60)
    print("策略对比")
    print("=" * 60)

    m1 = result1['metrics']
    m2 = result2['metrics']

    print(f"\n{'指标':<15} {'8因子策略':>12} {'位置判断策略':>12}")
    print("-" * 40)
    print(f"{'总收益率':<15} {m1['total_return']:>11.2%} {m2['total_return']:>11.2%}")
    print(f"{'年化收益':<15} {m1['annual_return']:>11.2%} {m2['annual_return']:>11.2%}")
    print(f"{'最大回撤':<15} {m1['max_drawdown']:>11.2%} {m2['max_drawdown']:>11.2%}")
    print(f"{'夏普比率':<15} {m1['sharpe_ratio']:>11.2f} {m2['sharpe_ratio']:>11.2f}")
    print(f"{'交易胜率':<15} {m1['trade_win_rate']:>11.2%} {m2['trade_win_rate']:>11.2%}")

    # 6. 导出报告
    print("\n" + "=" * 60)
    print("[4] 导出报告")
    print("=" * 60)

    export_full_report(engine1, result1, 'eight_factor_daily_report.txt')
    export_full_report(engine2, result2, 'position_strategy_daily_report.txt')
    engine1.export_daily_report('eight_factor_operations.csv')
    engine2.export_daily_report('position_strategy_operations.csv')

    # 7. 绘制图表
    try:
        from backtest.performance import PerformanceAnalyzer
        PerformanceAnalyzer.plot_equity_curve(engine1.daily_nav, 'eight_factor_result.png')
        PerformanceAnalyzer.plot_equity_curve(engine2.daily_nav, 'position_strategy_result.png')
    except Exception as e:
        print(f"绘图失败: {e}")

    print("\n" + "=" * 60)
    print("回测完成！")
    print("=" * 60)
    print(f"\n生成的文件：")
    print(f"  - eight_factor_daily_report.txt       (8因子策略每日报告)")
    print(f"  - position_strategy_daily_report.txt   (位置判断策略每日报告)")
    print(f"  - eight_factor_operations.csv          (8因子策略操作汇总)")
    print(f"  - position_strategy_operations.csv     (位置判断策略操作汇总)")
    print(f"  - eight_factor_result.png              (8因子策略净值曲线)")
    print(f"  - position_strategy_result.png         (位置判断策略净值曲线)")

    return result1, result2


def export_full_report(engine, result, filename):
    """导出完整的每日操作报告"""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("A股量化交易系统 - 每日操作报告\n")
        f.write("数据源: AKShare（真实行情数据）\n")
        f.write("=" * 70 + "\n\n")

        # 绩效摘要
        metrics = result['metrics']
        f.write("【绩效摘要】\n")
        f.write(f"  回测区间: {metrics.get('start_date', '')} ~ {metrics.get('end_date', '')}\n")
        f.write(f"  总收益率: {metrics.get('total_return', 0):.2%}\n")
        f.write(f"  年化收益: {metrics.get('annual_return', 0):.2%}\n")
        f.write(f"  最大回撤: {metrics.get('max_drawdown', 0):.2%}\n")
        f.write(f"  夏普比率: {metrics.get('sharpe_ratio', 0):.2f}\n")
        f.write(f"  交易胜率: {metrics.get('trade_win_rate', 0):.2%}\n")
        f.write(f"  总交易次数: {metrics.get('total_trades', 0)}\n")
        f.write(f"  总佣金: {metrics.get('total_commission', 0):.2f}\n")
        f.write(f"  总滑点: {metrics.get('total_slippage', 0):.2f}\n")
        f.write("\n" + "=" * 70 + "\n\n")

        # 每日操作明细
        f.write("【每日操作明细】\n\n")

        for op in engine.daily_operations:
            f.write("=" * 70 + "\n")
            f.write(f"日期: {op.date}\n")
            f.write("=" * 70 + "\n\n")

            f.write("账户概况:\n")
            f.write(f"  总资产: {op.portfolio_value:,.2f}\n")
            f.write(f"  可用资金: {op.cash:,.2f}\n")
            f.write(f"  持仓数量: {op.position_count}只\n")
            f.write(f"  今日收益: {op.daily_return:+.2%}\n")
            f.write(f"  累计收益: {op.cumulative_return:+.2%}\n")

            if op.sells:
                f.write(f"\n卖出操作（{len(op.sells)}笔）:\n")
                for s in op.sells:
                    f.write(f"  [x] {s['ts_code']} {s['name']}\n")
                    f.write(f"      成交价: {s['price']:.2f} | 数量: {s['quantity']}股 | 金额: {s['amount']:,.0f}\n")
                    f.write(f"      原因: {s['reason']}\n")

            if op.buys:
                f.write(f"\n买入操作（{len(op.buys)}笔）:\n")
                for b in op.buys:
                    f.write(f"  [+] {b['ts_code']} {b['name']}\n")
                    f.write(f"      成交价: {b['price']:.2f} | 数量: {b['quantity']}股 | 金额: {b['amount']:,.0f}\n")
                    f.write(f"      原因: {b['reason']}\n")

            if op.holds:
                f.write(f"\n当前持仓（{len(op.holds)}只）:\n")
                f.write(f"  {'代码':<12} {'名称':<10} {'数量':>8} {'成本':>10} {'现价':>10} {'盈亏率':>10}\n")
                f.write(f"  {'-' * 64}\n")
                for h in op.holds:
                    sign = "+" if h['profit_rate'] > 0 else "-" if h['profit_rate'] < 0 else " "
                    f.write(f"  {h['ts_code']:<12} {h['name']:<10} {h['quantity']:>8} "
                            f"{h['cost_price']:>10.2f} {h['current_price']:>10.2f} "
                            f"[{sign}]{h['profit_rate']:>+9.2%}\n")

            f.write("\n")

        # 交易记录汇总
        f.write("\n" + "=" * 70 + "\n")
        f.write("【交易记录汇总】\n")
        f.write("=" * 70 + "\n\n")

        f.write(f"{'日期':<12} {'代码':<12} {'方向':<6} {'价格':>10} {'数量':>8} {'金额':>12} {'原因'}\n")
        f.write("-" * 90 + "\n")

        for t in engine.trade_records:
            f.write(f"{t.trade_date:<12} {t.ts_code:<12} {t.side:<6} "
                    f"{t.price:>10.2f} {t.quantity:>8} {t.amount:>12,.0f} {t.reason}\n")

        # 每日净值
        f.write("\n" + "=" * 70 + "\n")
        f.write("【每日净值】\n")
        f.write("=" * 70 + "\n\n")

        f.write(f"{'日期':<12} {'总资产':>14} {'现金':>12} {'持仓市值':>12} {'持仓数':>6} {'今日收益':>10} {'累计收益':>10}\n")
        f.write("-" * 80 + "\n")

        for nav in engine.daily_nav:
            f.write(f"{nav['date']:<12} {nav['total_value']:>14,.2f} {nav['cash']:>12,.2f} "
                    f"{nav['position_value']:>12,.2f} {nav['position_count']:>6} "
                    f"{nav.get('daily_return', 0):>+9.2%} {nav['total_return']:>+9.2%}\n")

    print(f"  报告已导出: {filename}")


if __name__ == '__main__':
    # 可以自定义股票池和回测区间
    run_backtest(
        stock_pool=None,  # None使用默认池，或传入自定义列表如 ['600519', '000001']
        start_date='20260101',
        end_date='20260531',
        max_stocks=5  # 默认使用5只股票测试
    )