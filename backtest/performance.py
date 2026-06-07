"""
绩效评估模块
"""

import pandas as pd
import numpy as np
from typing import List


class PerformanceAnalyzer:
    """绩效评估器"""

    @staticmethod
    def calculate_metrics(daily_nav: List[dict], trade_records: list, initial_capital: float) -> dict:
        """计算回测绩效指标"""
        if not daily_nav:
            return {}

        df = pd.DataFrame(daily_nav)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
        df['daily_return'] = df['total_value'].pct_change()

        total_return = df['total_return'].iloc[-1]
        days = (df.index[-1] - df.index[0]).days
        annual_return = (1 + total_return) ** (365 / days) - 1 if days > 0 else 0

        df['peak'] = df['total_value'].cummax()
        df['drawdown'] = (df['total_value'] - df['peak']) / df['peak']
        max_drawdown = df['drawdown'].min()

        risk_free_rate = 0.03 / 252
        daily_excess_return = df['daily_return'] - risk_free_rate
        sharpe_ratio = (daily_excess_return.mean() / daily_excess_return.std() * np.sqrt(252)) \
            if daily_excess_return.std() > 0 else 0

        calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0

        win_days = (df['daily_return'] > 0).sum()
        total_days = len(df)
        win_rate = win_days / total_days if total_days > 0 else 0

        total_commission = sum(t.commission for t in trade_records) if trade_records else 0
        total_slippage = sum(t.slippage for t in trade_records) if trade_records else 0
        total_trades = len(trade_records)

        # 计算交易胜率和盈亏比
        sell_records = [t for t in trade_records if t.side == 'SELL']
        trade_profits = []
        for sell in sell_records:
            buy_records = [t for t in trade_records
                          if t.ts_code == sell.ts_code and t.side == 'BUY' and t.trade_date <= sell.trade_date]
            if buy_records:
                profit = (sell.price - buy_records[-1].price) * sell.quantity
                trade_profits.append(profit)

        if trade_profits:
            winning = [p for p in trade_profits if p > 0]
            losing = [p for p in trade_profits if p < 0]
            trade_win_rate = len(winning) / len(trade_profits)
            avg_win = np.mean(winning) if winning else 0
            avg_loss = abs(np.mean(losing)) if losing else 1
            profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        else:
            trade_win_rate = 0
            profit_loss_ratio = 0

        # 年化换手率
        if trade_records:
            buy_amount = sum(t.amount for t in trade_records if t.side == 'BUY')
            avg_capital = df['total_value'].mean()
            annual_turnover = (buy_amount / avg_capital) * (252 / days) if avg_capital > 0 and days > 0 else 0
        else:
            annual_turnover = 0

        return {
            'total_return': total_return,
            'annual_return': annual_return,
            'max_drawdown': max_drawdown,
            'annual_volatility': df['daily_return'].std() * np.sqrt(252),
            'sharpe_ratio': sharpe_ratio,
            'calmar_ratio': calmar_ratio,
            'win_rate': win_rate,
            'trade_win_rate': trade_win_rate,
            'profit_loss_ratio': profit_loss_ratio,
            'annual_turnover': annual_turnover,
            'total_trades': total_trades,
            'total_commission': total_commission,
            'total_slippage': total_slippage,
            'cost_ratio': (total_commission + total_slippage) / initial_capital,
            'start_date': df.index[0].strftime('%Y-%m-%d'),
            'end_date': df.index[-1].strftime('%Y-%m-%d'),
            'trading_days': days,
        }

    @staticmethod
    def print_report(metrics: dict):
        """打印回测报告"""
        print("\n" + "=" * 60)
        print("回测绩效报告")
        print("=" * 60)

        print(f"\n【回测区间】")
        print(f"  起止日期：{metrics.get('start_date', '')} ~ {metrics.get('end_date', '')}")
        print(f"  交易天数：{metrics.get('trading_days', 0)}天")

        print(f"\n【收益指标】")
        print(f"  总收益率：{metrics.get('total_return', 0):.2%}")
        print(f"  年化收益：{metrics.get('annual_return', 0):.2%}")

        print(f"\n【风险指标】")
        print(f"  最大回撤：{metrics.get('max_drawdown', 0):.2%}")
        print(f"  年化波动率：{metrics.get('annual_volatility', 0):.2%}")

        print(f"\n【风险调整收益】")
        print(f"  夏普比率：{metrics.get('sharpe_ratio', 0):.2f}")
        print(f"  卡玛比率：{metrics.get('calmar_ratio', 0):.2f}")

        print(f"\n【交易统计】")
        print(f"  总交易次数：{metrics.get('total_trades', 0)}")
        print(f"  日胜率：{metrics.get('win_rate', 0):.2%}")
        print(f"  交易胜率：{metrics.get('trade_win_rate', 0):.2%}")
        print(f"  盈亏比：{metrics.get('profit_loss_ratio', 0):.2f}")
        print(f"  年化换手率：{metrics.get('annual_turnover', 0):.2f}倍")

        print(f"\n【成本统计】")
        print(f"  总佣金：{metrics.get('total_commission', 0):.2f}")
        print(f"  总滑点：{metrics.get('total_slippage', 0):.2f}")
        print(f"  成本占比：{metrics.get('cost_ratio', 0):.2%}")

        print("\n" + "=" * 60)

    @staticmethod
    def plot_equity_curve(daily_nav: List[dict], save_path: str = 'backtest_result.png'):
        """绘制净值曲线"""
        try:
            import matplotlib.pyplot as plt

            df = pd.DataFrame(daily_nav)
            df['date'] = pd.to_datetime(df['date'])
            df['nav'] = df['total_value'] / df['total_value'].iloc[0]

            fig, axes = plt.subplots(2, 1, figsize=(14, 8))

            ax1 = axes[0]
            ax1.plot(df['date'], df['nav'], label='策略净值', linewidth=1.5)
            ax1.set_title('策略净值曲线')
            ax1.set_ylabel('净值')
            ax1.legend()
            ax1.grid(True, alpha=0.3)

            ax2 = axes[1]
            df['peak'] = df['nav'].cummax()
            df['drawdown'] = (df['nav'] - df['peak']) / df['peak']
            ax2.fill_between(df['date'], df['drawdown'], 0, color='red', alpha=0.3)
            ax2.set_title('回撤曲线')
            ax2.set_ylabel('回撤')
            ax2.grid(True, alpha=0.3)

            plt.tight_layout()
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            plt.show()
            print(f"图表已保存: {save_path}")

        except ImportError:
            print("需要安装matplotlib: pip install matplotlib")