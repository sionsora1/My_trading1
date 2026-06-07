"""
全局配置文件
使用AKShare数据源（完全免费，无需注册）
"""

# ============================================================
# 数据源配置
# ============================================================

# 数据缓存目录
DATA_CACHE_DIR = './data_cache'

# ============================================================
# 回测配置
# ============================================================

BACKTEST_CONFIG = {
    # 初始资金
    'initial_capital': 1_000_000,

    # 交易成本
    'commission_rate': 0.0003,     # 佣金费率万三
    'stamp_tax_rate': 0.0005,      # 印花税万五（卖出）
    'slippage_rate': 0.002,        # 滑点0.2%
    'min_commission': 5.0,         # 最低佣金5元

    # 持仓限制
    'max_position_num': 20,        # 最大持仓数
    'max_single_weight': 0.05,     # 单只最大仓位5%
    'max_industry_weight': 0.20,   # 单行业最大仓位20%

    # 调仓设置
    'rebalance_frequency': 'weekly',  # daily/weekly/monthly

    # 止损止盈
    'stop_loss_rate': -0.08,       # 止损线-8%
    'move_stop_rate': -0.10,       # 移动止盈回撤10%

    # 涨跌停限制
    'limit_up_rate': 0.10,         # 涨停10%
    'limit_down_rate': -0.10,      # 跌停-10%

    # T+1限制
    't_plus_1': True,
}

# ============================================================
# 8因子权重配置
# ============================================================

FACTOR_WEIGHTS = {
    'EP': 0.15,              # 价值因子：盈利收益率
    'profit_growth': 0.15,   # 成长因子：净利润增速
    'revenue_growth': 0.10,  # 成长补充：营收增速
    'reversal_20d': 0.15,    # 反转因子：-20日收益
    'turnover_neg': 0.10,    # 低换手：-换手率
    'volatility_neg': 0.10,  # 低波动：-波动率
    'ROE': 0.10,             # 质量因子：净资产收益率
    'accrual_neg': 0.05,     # 利润质量：-应计比率
}

# ============================================================
# 位置判断参数
# ============================================================

POSITION_CONFIG = {
    # 低位判断（满足3个以上为低位）
    'low_price_percentile': 0.30,    # 近1年价格分位
    'low_pe_percentile': 0.30,       # PE历史分位
    'low_return_60d': 0,             # 近60日收益
    'low_signals_threshold': 3,      # 信号阈值

    # 高位判断（满足3个以上为高位）
    'high_price_percentile': 0.70,
    'high_pe_percentile': 0.70,
    'high_return_60d': 0.30,
    'high_signals_threshold': 3,

    # 低位买入条件
    'profit_growth_threshold': 0.20,  # 净利润增速>20%
    'pledge_ratio_limit': 0.30,       # 质押比例上限
}

# ============================================================
# 参数优化配置
# ============================================================

OPTIMIZER_CONFIG = {
    # 优化方法：'grid', 'random', 'bayesian'
    'method': 'bayesian',

    # 随机搜索迭代次数
    'random_n_iter': 200,

    # 贝叶斯优化参数
    'bayesian_n_init': 15,
    'bayesian_n_iter': 60,

    # 过拟合验证
    'validation_splits': 5,
    'train_ratio': 0.6,
}