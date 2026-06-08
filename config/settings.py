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
# 实盘/模拟盘交易配置
# ============================================================

LIVE_TRADING_CONFIG = {
    # 券商选择: 'sim' (模拟盘) / 'qmt' (迅投QMT)
    'broker': 'sim',

    # 交易模式: 'auto' (全自动) / 'semi' (半自动，需确认信号)
    'mode': 'semi',

    # 模拟盘配置
    'sim': {
        'initial_capital': 100_000,          # 初始资金10万
        'commission_rate': 0.0003,
        'stamp_tax_rate': 0.0005,
        'slippage_rate': 0.002,
        'min_commission': 5.0,
        'data_dir': './data_cache',
    },

    # QMT 配置（需要先开支持 QMT 的券商账户）
    'qmt': {
        'account_id': '',
        'miniqmt_path': r'D:\QMT\userdata_mini',
    },

    # 风控配置
    'risk': {
        'max_daily_loss_rate': 0.02,         # 单日最大亏损2%
        'max_single_position_weight': 0.20,  # 单只最大仓位20%
        'max_total_positions': 5,            # 最大持仓数5只
        'max_single_order_amount': 50000,    # 单笔最大金额5万
        'require_confirm_large': True,       # 大额需确认
        'large_order_threshold': 20000,      # 大额阈值2万
    },

    # 信号扫描配置
    'scan': {
        'interval_seconds': 60,              # 扫描间隔(秒)
        'strategy': 'all',                   # 使用的策略: 'all'=全部 / 'eight_factor' / 'momentum' 等
        'stock_pool': [                      # 监控股票池
            '600519', '000858', '601398',
            '002415', '002230', '300015',
            '002371', '002410', '300750',
        ],
    },

    # 通知配置
    'notify': {
        'console_print': True,               # 控制台打印信号
        'file_save': True,                   # 文件保存信号
        'data_dir': './data_cache',
    },
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