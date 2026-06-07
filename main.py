"""
A股量化交易系统 - 主程序入口
数据源: AKShare（完全免费，无需注册）
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from run_backtest import run_backtest


def main():
    """主程序"""
    print("=" * 60)
    print("A股量化交易系统 v1.1")
    print("数据源: AKShare（免费，无需注册）")
    print("=" * 60)
    print("\n功能说明：")
    print("  - 8因子选股策略（价值+成长+反转+质量+流动性）")
    print("  - 位置判断策略（低位看基本面，高位看趋势）")
    print("  - 每日操作报告（建仓/调仓/清仓明细）")
    print("  - 止损止盈系统")
    print("  - 参数优化")
    print("\n" + "=" * 60)

    # 选择模式
    print("\n请选择运行模式：")
    print("  1. 快速回测（默认股票池，1年数据）")
    print("  2. 自定义回测（自选股票和区间）")
    print("  0. 退出")

    choice = input("\n请输入选项 (0-2): ").strip()

    if choice == '1':
        # 快速回测
        print("\n启动快速回测...")
        run_backtest(
            stock_pool=None,  # 使用默认池
            start_date='20260101',
            end_date='20260531',
            max_stocks=5  # 默认5只股票测试
        )

    elif choice == '2':
        # 自定义回测
        print("\n自定义回测设置：")

        # 股票池
        print("\n请输入股票代码（用逗号分隔，如 600519,000001,000858）：")
        print("直接回车使用默认股票池")
        stock_input = input("股票代码: ").strip()

        if stock_input:
            stock_pool = [code.strip() for code in stock_input.split(',')]
        else:
            stock_pool = None

        # 回测区间
        print("\n请输入回测区间（格式: YYYYMMDD）：")
        start_date = input("开始日期 (默认20230101): ").strip() or '20230101'
        end_date = input("结束日期 (默认20231231): ").strip() or '20231231'

        # 股票数量
        max_stocks_input = input("\n最大股票数量 (默认40): ").strip()
        max_stocks = int(max_stocks_input) if max_stocks_input else 40

        print(f"\n启动自定义回测...")
        print(f"  股票池: {stock_pool or '默认'}")
        print(f"  区间: {start_date} ~ {end_date}")
        print(f"  最大股票数: {max_stocks}")

        run_backtest(
            stock_pool=stock_pool,
            start_date=start_date,
            end_date=end_date,
            max_stocks=max_stocks
        )

    elif choice == '0':
        print("退出系统")
        return

    else:
        print("无效选项")


if __name__ == '__main__':
    main()