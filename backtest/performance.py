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
    def _build_benchmark_nav(daily_nav: list, benchmark_code: str = '000300') -> list:
        """构建基准净值序列，用于图表叠加"""
        try:
            from data.database import SQLiteManager
            db = SQLiteManager()
            ts_code = f'{benchmark_code}.SH'
            start = daily_nav[0]['date']
            end = daily_nav[-1]['date']
            bars = db.get_daily_bars(ts_code, start, end)
            db.close()
            if not bars:
                return []
            return [{'date': b['trade_date'], 'value': b['close']} for b in bars]
        except Exception:
            return []

    @staticmethod
    def calculate_benchmark_metrics(daily_nav: list, benchmark_code: str = '000300') -> dict:
        """
        计算基准对比指标：Alpha, Beta, 信息比率, 超额收益, 超额回撤

        Args:
            daily_nav: 策略每日净值列表
            benchmark_code: 基准代码 ('000300' CSI300, '000905' CSI500)

        Returns:
            dict with benchmark metrics
        """
        try:
            from data.database import SQLiteManager
            db = SQLiteManager()
            ts_code = f'{benchmark_code}.SH'
            start = daily_nav[0]['date']
            end = daily_nav[-1]['date']
            bars = db.get_daily_bars(ts_code, start, end)
            db.close()

            if not bars:
                return {}

            # Build benchmark NAV aligned with strategy dates
            bm_prices = {b['trade_date']: b['close'] for b in bars}
            bm_nav = []
            for nav in daily_nav:
                date = nav['date']
                if date in bm_prices and bm_prices[date] > 0:
                    bm_nav.append(bm_prices[date])

            if len(bm_nav) < 10:
                return {}

            bm_nav0 = bm_nav[0]
            bm_returns = [(bm_nav[i] - bm_nav[i-1]) / bm_nav[i-1]
                          for i in range(1, len(bm_nav))]

            strategy_values = [nav['total_value'] for nav in daily_nav
                               if nav['date'] in bm_prices]
            if len(strategy_values) < 2:
                return {}
            st_returns = [(strategy_values[i] - strategy_values[i-1]) / strategy_values[i-1]
                          for i in range(1, len(strategy_values))]

            # Align lengths
            min_len = min(len(st_returns), len(bm_returns))
            st_returns = st_returns[-min_len:]
            bm_returns = bm_returns[-min_len:]

            import numpy as np
            st_arr = np.array(st_returns)
            bm_arr = np.array(bm_returns)

            # Beta: covariance / variance
            cov = np.cov(st_arr, bm_arr)[0][1]
            var = np.var(bm_arr)
            beta = cov / var if var > 0 else 1.0

            # Alpha: annualized excess return over risk-free rate
            risk_free_daily = 0.03 / 252
            excess_daily = st_arr - risk_free_daily - beta * (bm_arr - risk_free_daily)
            alpha = excess_daily.mean() * 252  # annualized

            # Information Ratio
            tracking_error = (st_arr - bm_arr).std() * np.sqrt(252)
            excess_return = (st_arr.mean() - bm_arr.mean()) * 252
            information_ratio = excess_return / tracking_error if tracking_error > 0 else 0

            # Benchmark metrics
            bm_total_return = (bm_nav[-1] - bm_nav0) / bm_nav0
            bm_peak = bm_nav[0]
            bm_max_dd = 0
            for p in bm_nav:
                if p > bm_peak:
                    bm_peak = p
                dd = (p - bm_peak) / bm_peak
                if dd < bm_max_dd:
                    bm_max_dd = dd

            # Excess max drawdown (strategy DD beyond benchmark DD)
            # Build aligned NAV series
            st_nav = [1.0]
            for r in st_returns:
                st_nav.append(st_nav[-1] * (1 + r))
            bm_nav_norm = [1.0]
            for r in bm_returns:
                bm_nav_norm.append(bm_nav_norm[-1] * (1 + r))

            excess_nav = [s / b for s, b in zip(st_nav, bm_nav_norm)]
            ex_peak = 1.0
            excess_max_dd = 0
            for v in excess_nav:
                if v > ex_peak:
                    ex_peak = v
                dd = (v - ex_peak) / ex_peak
                if dd < excess_max_dd:
                    excess_max_dd = dd

            return {
                'benchmark_code': benchmark_code,
                'benchmark_name': '沪深300' if benchmark_code == '000300' else '中证500',
                'benchmark_return': bm_total_return,
                'benchmark_max_drawdown': bm_max_dd,
                'alpha': alpha,
                'beta': beta,
                'information_ratio': information_ratio,
                'excess_return': excess_return,
                'excess_max_drawdown': excess_max_dd,
                'tracking_error': tracking_error,
                'is_outperforming': bm_total_return < (st_nav[-1] - 1) if st_nav else False,
            }
        except Exception as e:
            return {'error': str(e)}

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

        # Benchmark section
        bm = metrics.get('benchmark', {})
        if bm:
            print(f"\n【基准对比 ({bm.get('benchmark_name', '')})】")
            bm_ret = bm.get('benchmark_return', 0)
            strategy_ret = metrics.get('total_return', 0)
            print(f"  策略收益：{strategy_ret:.2%}")
            print(f"  基准收益：{bm_ret:.2%}")
            excess = strategy_ret - bm_ret
            tag = '[OUTPERFORM]' if excess > 0 else '[UNDERPERFORM]'
            print(f"  超额收益：{excess:.2%} {tag}")
            print(f"  Alpha(年化)：{bm.get('alpha', 0):.2%}")
            print(f"  Beta：{bm.get('beta', 0):.2f}")
            print(f"  信息比率：{bm.get('information_ratio', 0):.2f}")
            print(f"  超额最大回撤：{bm.get('excess_max_drawdown', 0):.2%}")
            print(f"  基准最大回撤：{bm.get('benchmark_max_drawdown', 0):.2%}")

        print("\n" + "=" * 60)

    @staticmethod
    def plot_equity_curve(daily_nav: List[dict], save_path: str = 'backtest_result.png',
                          benchmark_nav: List[dict] = None, benchmark_name: str = '沪深300'):
        """绘制净值曲线（可选叠加基准）"""
        try:
            import matplotlib.pyplot as plt

            df = pd.DataFrame(daily_nav)
            df['date'] = pd.to_datetime(df['date'])
            df['nav'] = df['total_value'] / df['total_value'].iloc[0]

            fig, axes = plt.subplots(2, 1, figsize=(14, 8))

            ax1 = axes[0]
            ax1.plot(df['date'], df['nav'], label='Strategy NAV', linewidth=1.5, color='#1f77b4')

            # Overlay benchmark if provided
            if benchmark_nav and len(benchmark_nav) > 0:
                bm_df = pd.DataFrame(benchmark_nav)
                bm_df['date'] = pd.to_datetime(bm_df['date'])
                bm_df['nav'] = bm_df['value'] / bm_df['value'].iloc[0]
                ax1.plot(bm_df['date'], bm_df['nav'], label=benchmark_name,
                         linewidth=1.0, linestyle='--', color='#ff7f0e', alpha=0.8)

            ax1.set_title('Strategy vs Benchmark NAV')
            ax1.set_ylabel('NAV')
            ax1.legend()
            ax1.grid(True, alpha=0.3)

            # Drawdown with benchmark
            ax2 = axes[1]
            df['peak'] = df['nav'].cummax()
            df['drawdown'] = (df['nav'] - df['peak']) / df['peak']
            ax2.plot(df['date'], df['drawdown'], label='Strategy DD', linewidth=1.2, color='red')

            if benchmark_nav and len(benchmark_nav) > 0:
                bm_df['peak'] = bm_df['nav'].cummax()
                bm_df['dd'] = (bm_df['nav'] - bm_df['peak']) / bm_df['peak']
                ax2.plot(bm_df['date'], bm_df['dd'], label=f'{benchmark_name} DD',
                         linewidth=1.0, linestyle='--', color='orange', alpha=0.8)

            ax2.set_title('Drawdown')
            ax2.set_ylabel('Drawdown')
            ax2.legend()
            ax2.grid(True, alpha=0.3)
            ax2.set_ylim(min(df['drawdown'].min() * 1.2, -0.05), 0.02)

            plt.tight_layout()
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            plt.show()
            print(f"Chart saved: {save_path}")

        except ImportError:
            print("需要安装matplotlib: pip install matplotlib")