"""
A股量化交易系统 - 可视化界面
基于Streamlit
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from backtest.engine import BacktestEngine, BacktestConfig
from strategy import get_strategy, get_all_strategies, STRATEGY_REGISTRY
from data.fetcher import DataFetcher, DataCache
from config.settings import BACKTEST_CONFIG


# ============================================================
# 页面配置
# ============================================================

st.set_page_config(
    page_title="A股量化交易系统",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 自定义CSS
st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
        text-align: center;
        margin-bottom: 1rem;
    }
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        margin-bottom: 0.5rem;
    }
    .buy-signal {
        color: #00c853;
        font-weight: bold;
    }
    .sell-signal {
        color: #ff1744;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)


# ============================================================
# 侧边栏配置
# ============================================================

def sidebar_config():
    """侧边栏配置"""
    st.sidebar.title("📈 A股量化交易系统")
    st.sidebar.markdown("---")

    # 股票池配置
    st.sidebar.subheader("📊 股票池配置")

    # 预设股票池
    preset_pools = {
        "沪深300龙头（5只）": ['600519', '000858', '601398', '000001', '600036'],
        "科技成长（5只）": ['002415', '002230', '300015', '300750', '300763'],
        "中小盘高alpha（5只）": ['002371', '002410', '002352', '002399', '002421'],
        "混合大小盘（5只）": ['600519', '002415', '002371', '002410', '300015'],
        "自定义": []
    }

    selected_pool = st.sidebar.selectbox(
        "选择预设股票池",
        list(preset_pools.keys()),
        index=3
    )

    if selected_pool == "自定义":
        custom_codes = st.sidebar.text_area(
            "输入股票代码（每行一个）",
            "600519\n000858\n002415\n300015\n002371",
            height=150
        )
        stock_pool = [code.strip() for code in custom_codes.split('\n') if code.strip()]
    else:
        stock_pool = preset_pools[selected_pool]
        st.sidebar.write("当前股票池：")
        for code in stock_pool:
            st.sidebar.write(f"  - {code}")

    st.sidebar.markdown("---")

    # 回测参数
    st.sidebar.subheader("⚙️ 回测参数")

    col1, col2 = st.sidebar.columns(2)
    with col1:
        start_date = st.date_input("开始日期", pd.to_datetime("2026-01-01"))
    with col2:
        end_date = st.date_input("结束日期", pd.to_datetime("2026-05-31"))

    start_date_str = start_date.strftime('%Y%m%d')
    end_date_str = end_date.strftime('%Y%m%d')

    st.sidebar.markdown("---")

    # 策略选择
    st.sidebar.subheader("🎯 策略选择")
    all_strategies = get_all_strategies()
    strategy_options = {v['name']: k for k, v in all_strategies.items()}
    strategy_options["全部运行"] = "all"
    strategy_display = list(strategy_options.keys())
    selected_strategy_name = st.sidebar.selectbox("选择策略", strategy_display)
    strategy_type = strategy_options[selected_strategy_name]

    st.sidebar.markdown("---")

    # 资金配置
    st.sidebar.subheader("💰 资金配置")
    initial_capital = st.sidebar.number_input(
        "初始资金（元）",
        min_value=10000,
        max_value=10000000,
        value=1000000,
        step=10000
    )

    max_position = st.sidebar.slider(
        "最大持仓数",
        min_value=1,
        max_value=20,
        value=5
    )

    st.sidebar.markdown("---")

    # 运行按钮
    run_backtest = st.sidebar.button("🚀 运行回测", type="primary", use_container_width=True)

    return {
        'stock_pool': stock_pool,
        'start_date': start_date_str,
        'end_date': end_date_str,
        'strategy_type': strategy_type,
        'initial_capital': initial_capital,
        'max_position': max_position,
        'run_backtest': run_backtest
    }


# ============================================================
# 数据获取
# ============================================================

@st.cache_data(ttl=3600, show_spinner="正在获取数据...")
def fetch_market_data(stock_pool, start_date, end_date):
    """获取市场数据（带缓存）"""
    fetcher = DataFetcher()
    cache = DataCache()
    cache_filename = f'market_data_{start_date}_{end_date}_{len(stock_pool)}stocks'

    # 尝试加载缓存
    market_data = cache.load_market_data(cache_filename)

    if market_data and isinstance(market_data, dict) and len(market_data) > 50:
        return market_data

    # 获取数据
    market_data = fetcher.build_market_data_by_date(stock_pool, start_date, end_date)

    if market_data and len(market_data) > 0:
        cache.save_market_data(market_data, cache_filename)

    return market_data


# ============================================================
# 运行回测
# ============================================================

def run_backtest_engine(market_data, config_dict, strategy_type):
    """运行回测引擎"""
    config = BacktestConfig.from_dict(config_dict)

    results = {}

    strategies_to_run = list(STRATEGY_REGISTRY.keys()) if strategy_type == "all" else [strategy_type]

    for strategy_name in strategies_to_run:
        engine = BacktestEngine(config)
        strategy = get_strategy(strategy_name)
        result = engine.run(market_data, strategy, print_report=False)
        results[strategy_name] = {
            'engine': engine,
            'result': result
        }

    return results


# ============================================================
# 图表绘制
# ============================================================

def plot_equity_curve(daily_nav, title="策略净值曲线"):
    """绘制净值曲线"""
    df = pd.DataFrame(daily_nav)
    df['date'] = pd.to_datetime(df['date'])
    df['nav'] = df['total_value'] / df['total_value'].iloc[0]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.7, 0.3],
        subplot_titles=(title, '回撤曲线')
    )

    # 净值曲线
    fig.add_trace(
        go.Scatter(
            x=df['date'],
            y=df['nav'],
            mode='lines',
            name='策略净值',
            line=dict(color='#1f77b4', width=2)
        ),
        row=1, col=1
    )

    # 回撤曲线
    df['peak'] = df['nav'].cummax()
    df['drawdown'] = (df['nav'] - df['peak']) / df['peak']

    fig.add_trace(
        go.Scatter(
            x=df['date'],
            y=df['drawdown'],
            fill='tozeroy',
            fillcolor='rgba(255, 0, 0, 0.3)',
            mode='lines',
            name='回撤',
            line=dict(color='red', width=1)
        ),
        row=2, col=1
    )

    fig.update_layout(
        height=600,
        showlegend=True,
        hovermode='x unified',
        template='plotly_white'
    )

    fig.update_yaxes(title_text="净值", row=1, col=1)
    fig.update_yaxes(title_text="回撤", row=2, col=1)

    return fig


def plot_daily_operations(engine):
    """绘制每日操作统计"""
    if not engine.daily_operations:
        return None

    dates = []
    buy_counts = []
    sell_counts = []
    position_counts = []

    for op in engine.daily_operations:
        dates.append(op.date)
        buy_counts.append(len(op.buys))
        sell_counts.append(len(op.sells))
        position_counts.append(op.position_count)

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=dates,
        y=buy_counts,
        name='买入',
        marker_color='green',
        opacity=0.7
    ))

    fig.add_trace(go.Bar(
        x=dates,
        y=[-x for x in sell_counts],
        name='卖出',
        marker_color='red',
        opacity=0.7
    ))

    fig.add_trace(go.Scatter(
        x=dates,
        y=position_counts,
        name='持仓数',
        yaxis='y2',
        line=dict(color='blue', width=2)
    ))

    fig.update_layout(
        title='每日操作统计',
        height=400,
        barmode='relative',
        yaxis=dict(title='买卖数量'),
        yaxis2=dict(title='持仓数', overlaying='y', side='right'),
        template='plotly_white'
    )

    return fig


# ============================================================
# 主页面
# ============================================================

def main():
    """主页面"""
    # 侧边栏配置
    config = sidebar_config()

    # 主标题
    st.markdown('<div class="main-header">📈 A股量化交易系统</div>', unsafe_allow_html=True)
    st.markdown("---")

    # 如果没有运行回测，显示欢迎页面
    if not config['run_backtest']:
        st.info("👈 请在左侧配置参数，然后点击「运行回测」按钮开始")

        st.subheader("系统功能")
        col1, col2, col3 = st.columns(3)

        with col1:
            st.markdown("""
            **📊 8因子选股策略**
            - 价值因子（EP）
            - 成长因子（增速）
            - 反转因子
            - 质量因子（ROE）
            - 低换手/低波动
            """)

        with col2:
            st.markdown("""
            **📍 位置判断策略**
            - 低位：看基本面变化
            - 高位：看趋势量价
            - 自动判断位置
            - 动态调整策略
            """)

        with col3:
            st.markdown("""
            **📋 每日操作报告**
            - 详细的建仓/调仓/清仓
            - 真实股票名称和价格
            - 止损止盈自动触发
            - 持仓明细和盈亏
            """)

        return

    # 运行回测
    st.subheader("🔄 正在运行回测...")

    # 获取数据
    with st.spinner("正在获取市场数据..."):
        market_data = fetch_market_data(
            config['stock_pool'],
            config['start_date'],
            config['end_date']
        )

    if not market_data or len(market_data) == 0:
        st.error("❌ 获取数据失败，请检查网络连接或股票代码")
        return

    st.success(f"✅ 成功获取 {len(market_data)} 个交易日数据")

    # 运行回测
    with st.spinner("正在运行策略回测..."):
        backtest_config = {
            'initial_capital': config['initial_capital'],
            'max_position_num': config['max_position'],
            'rebalance_frequency': 'weekly',
            'stop_loss_rate': -0.08,
            'move_stop_rate': -0.10,
        }

        results = run_backtest_engine(
            market_data,
            backtest_config,
            config['strategy_type']
        )

    st.success("✅ 回测完成！")

    # ============================================================
    # 显示结果
    # ============================================================

    st.markdown("---")
    st.subheader("📊 回测结果")

    # 显示绩效指标
    for strategy_name, data in results.items():
        metrics = data['result']['metrics']
        engine = data['engine']

        strategy_label = STRATEGY_REGISTRY.get(strategy_name, {}).get('name', strategy_name)

        st.markdown(f"### 🎯 {strategy_label}")

        # 指标卡片
        col1, col2, col3, col4, col5 = st.columns(5)

        with col1:
            st.metric("总收益率", f"{metrics['total_return']:.2%}")
        with col2:
            st.metric("年化收益", f"{metrics['annual_return']:.2%}")
        with col3:
            st.metric("最大回撤", f"{metrics['max_drawdown']:.2%}")
        with col4:
            st.metric("夏普比率", f"{metrics['sharpe_ratio']:.2f}")
        with col5:
            st.metric("交易胜率", f"{metrics['trade_win_rate']:.2%}")

        # 净值曲线
        fig = plot_equity_curve(engine.daily_nav, f"{strategy_label} - 净值曲线")
        st.plotly_chart(fig, use_container_width=True)

        # 每日操作统计
        fig_ops = plot_daily_operations(engine)
        if fig_ops:
            st.plotly_chart(fig_ops, use_container_width=True)

        # 每日操作明细
        st.markdown(f"### 📋 {strategy_label} - 每日操作明细")

        # 选择日期查看
        if engine.daily_operations:
            op_dates = [op.date for op in engine.daily_operations]
            selected_date = st.selectbox(
                "选择日期",
                op_dates,
                key=f"date_{strategy_name}"
            )

            # 找到对应的操作记录
            for op in engine.daily_operations:
                if op.date == selected_date:
                    # 账户概况
                    col1, col2, col3, col4 = st.columns(4)
                    with col1:
                        st.metric("总资产", f"{op.portfolio_value:,.2f}")
                    with col2:
                        st.metric("可用资金", f"{op.cash:,.2f}")
                    with col3:
                        st.metric("持仓数量", f"{op.position_count}只")
                    with col4:
                        st.metric("累计收益", f"{op.cumulative_return:+.2%}")

                    # 卖出操作
                    if op.sells:
                        st.markdown("**📤 卖出操作**")
                        sell_df = pd.DataFrame(op.sells)
                        if not sell_df.empty:
                            sell_df = sell_df[['ts_code', 'name', 'price', 'quantity', 'amount', 'reason']]
                            sell_df.columns = ['代码', '名称', '成交价', '数量', '金额', '原因']
                            st.dataframe(sell_df, use_container_width=True)

                    # 买入操作
                    if op.buys:
                        st.markdown("**📥 买入操作**")
                        buy_df = pd.DataFrame(op.buys)
                        if not buy_df.empty:
                            buy_df = buy_df[['ts_code', 'name', 'price', 'quantity', 'amount', 'reason']]
                            buy_df.columns = ['代码', '名称', '成交价', '数量', '金额', '原因']
                            st.dataframe(buy_df, use_container_width=True)

                    # 当前持仓
                    if op.holds:
                        st.markdown("**📦 当前持仓**")
                        holds_df = pd.DataFrame(op.holds)
                        if not holds_df.empty:
                            holds_df = holds_df[['ts_code', 'name', 'quantity', 'cost_price', 'current_price', 'profit_rate']]
                            holds_df.columns = ['代码', '名称', '数量', '成本价', '现价', '盈亏率']

                            # 格式化盈亏率
                            def color_profit(val):
                                if isinstance(val, (int, float)):
                                    if val > 0:
                                        return f'color: green'
                                    elif val < 0:
                                        return f'color: red'
                                return ''

                            st.dataframe(
                                holds_df.style.applymap(color_profit, subset=['盈亏率']),
                                use_container_width=True
                            )

                    break

        st.markdown("---")

    # 导出报告
    st.subheader("📥 导出报告")

    for strategy_name, data in results.items():
        strategy_label = STRATEGY_REGISTRY.get(strategy_name, {}).get('name', strategy_name)
        engine = data['engine']

        if st.button(f"导出 {strategy_label} 报告", key=f"export_{strategy_name}"):
            filename = f"{strategy_name}_daily_report.txt"
            export_report(engine, data['result'], filename)
            st.success(f"✅ 报告已导出: {filename}")


def export_report(engine, result, filename):
    """导出报告"""
    with open(filename, 'w', encoding='utf-8') as f:
        f.write("=" * 70 + "\n")
        f.write("A股量化交易系统 - 每日操作报告\n")
        f.write("数据源: AKShare（真实行情数据）\n")
        f.write("=" * 70 + "\n\n")

        metrics = result['metrics']
        f.write("【绩效摘要】\n")
        f.write(f"  回测区间: {metrics.get('start_date', '')} ~ {metrics.get('end_date', '')}\n")
        f.write(f"  总收益率: {metrics.get('total_return', 0):.2%}\n")
        f.write(f"  年化收益: {metrics.get('annual_return', 0):.2%}\n")
        f.write(f"  最大回撤: {metrics.get('max_drawdown', 0):.2%}\n")
        f.write(f"  夏普比率: {metrics.get('sharpe_ratio', 0):.2f}\n")
        f.write(f"  交易胜率: {metrics.get('trade_win_rate', 0):.2%}\n")
        f.write("\n" + "=" * 70 + "\n\n")

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
                for h in op.holds:
                    sign = "+" if h['profit_rate'] > 0 else "-" if h['profit_rate'] < 0 else " "
                    f.write(f"  {h['ts_code']:<12} {h['name']:<10} {h['quantity']:>8} "
                            f"{h['cost_price']:>10.2f} {h['current_price']:>10.2f} "
                            f"[{sign}]{h['profit_rate']:>+9.2%}\n")

            f.write("\n")


if __name__ == '__main__':
    main()