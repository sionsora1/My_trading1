"""
参数优化运行脚本
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import numpy as np

from optimizer.search import ParamSpace, ObjectiveFunction, GridSearchOptimizer, RandomSearchOptimizer, BayesianOptimizer
from optimizer.validator import OverfittingValidator, ResultAnalyzer
from config.settings import OPTIMIZER_CONFIG


def generate_simulated_market_data(n_stocks: int = 50, n_days: int = 300, start_date: str = '20220104'):
    """生成模拟市场数据"""
    from datetime import datetime, timedelta

    np.random.seed(42)

    dates = []
    current = datetime.strptime(start_date, '%Y%m%d')
    for _ in range(n_days):
        while current.weekday() >= 5:
            current += timedelta(days=1)
        dates.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)

    stock_codes = [f"{600000 + i:06d}" for i in range(n_stocks)]
    market_data = {}
    prices = {code: np.random.uniform(10, 100) for code in stock_codes}

    for date in dates:
        daily_data = {}
        for code in stock_codes:
            daily_return = np.clip(np.random.normal(0.0003, 0.02), -0.098, 0.098)
            prev_price = prices[code]
            new_price = prev_price * (1 + daily_return)
            prices[code] = new_price

            daily_data[code] = {
                'ts_code': f"{code}.SH",
                'close': round(new_price, 2),
                'open': round(new_price * (1 + np.random.uniform(-0.01, 0.01)), 2),
                'volume': int(np.random.uniform(1e6, 1e8)),
                'prev_close': round(prev_price, 2),
                'trade_date': date,
                'industry': np.random.choice(['银行', '白酒', '科技', '医药', '新能源']),
            }
        market_data[date] = daily_data

    return market_data


def define_param_space() -> ParamSpace:
    """定义参数空间"""
    space = ParamSpace()

    # 因子权重
    space.add_param('w_EP', 0.10, 0.25, 0.05, 'float')
    space.add_param('w_growth', 0.10, 0.25, 0.05, 'float')
    space.add_param('w_reversal', 0.10, 0.25, 0.05, 'float')
    space.add_param('w_quality', 0.05, 0.20, 0.05, 'float')

    # 持仓参数
    space.add_param('max_position_num', 10, 30, 5, 'int')
    space.add_param('stop_loss_rate', -0.12, -0.05, 0.01, 'float')

    return space


def run_optimizer_demo():
    """运行参数优化演示"""
    print("=" * 60)
    print("A股量化策略参数优化系统")
    print("=" * 60)

    # 1. 生成模拟数据
    print("\n[1] 生成模拟市场数据...")
    market_data = generate_simulated_market_data(n_stocks=50, n_days=300)
    print(f"    共 {len(market_data)} 个交易日")

    # 2. 定义参数空间
    print("\n[2] 定义参数空间...")
    param_space = define_param_space()
    print(f"    参数组合总数: {param_space.size}")
    for p in param_space.params:
        print(f"    {p.name}: [{p.min_val}, {p.max_val}], step={p.step}")

    # 3. 创建目标函数
    objective = ObjectiveFunction(market_data)

    # 4. 运行优化
    method = OPTIMIZER_CONFIG.get('method', 'bayesian')

    print(f"\n[3] 运行{method}优化...")

    if method == 'grid':
        optimizer = GridSearchOptimizer(objective)
        search_result = optimizer.optimize(param_space, verbose=True)
    elif method == 'random':
        optimizer = RandomSearchOptimizer(objective)
        search_result = optimizer.optimize(param_space, n_iter=OPTIMIZER_CONFIG.get('random_n_iter', 100), verbose=True)
    else:
        optimizer = BayesianOptimizer(objective)
        search_result = optimizer.optimize(
            param_space,
            n_init=OPTIMIZER_CONFIG.get('bayesian_n_init', 15),
            n_iter=OPTIMIZER_CONFIG.get('bayesian_n_iter', 60),
            verbose=True
        )

    best_params = search_result['best_params']

    # 5. 过拟合验证
    print("\n[4] 过拟合验证...")
    validator = OverfittingValidator(market_data)
    validation_result = validator.walk_forward_validation(
        best_params, n_splits=OPTIMIZER_CONFIG.get('validation_splits', 5)
    )

    if validation_result['is_overfit']:
        print("    [!] 检测到过拟合风险，建议调整参数或增加数据")
    else:
        print("    [OK] 过拟合风险较低")

    # 6. 参数稳定性测试
    print("\n[5] 参数稳定性测试...")
    stability_result = validator.parameter_stability_test(param_space)

    if stability_result['is_stable']:
        print("    [OK] 参数稳定性良好")
    else:
        print("    [!] 参数稳定性较差，建议使用稳健参数")

    # 7. 寻找稳健参数
    print("\n[6] 寻找稳健参数...")
    robust_result = ResultAnalyzer.find_robust_parameters(search_result['all_results'])

    # 8. 生成报告
    print("\n[7] 生成报告...")
    report = ResultAnalyzer.generate_report(search_result, validation_result)
    print(report)

    # 9. 输出最终建议
    print("\n" + "=" * 60)
    print("最终建议")
    print("=" * 60)

    if stability_result['stability_score'] < 0.7:
        print("\n建议使用稳健参数（中位数）：")
        for name, value in robust_result['robust_params'].items():
            print(f"  {name}: {value:.4f}")
    else:
        print("\n建议使用最优参数：")
        for name, value in best_params.items():
            print(f"  {name}: {value:.4f}")

    print(f"\n参数稳定性得分: {stability_result['stability_score']:.2f}")
    print(f"过拟合得分: {validation_result['overfit_score']:.2f}")

    # 10. 保存结果
    print("\n保存优化结果...")

    recommendation = {
        'optimal_params': best_params,
        'robust_params': robust_result['robust_params'],
        'use_robust': stability_result['stability_score'] < 0.7,
        'best_score': search_result['best_score'],
        'best_metrics': search_result['best_metrics'],
        'validation': {
            'is_overfit': validation_result['is_overfit'],
            'return_decay': validation_result['return_decay'],
            'overfit_score': validation_result['overfit_score'],
        },
        'stability': {
            'score': stability_result['stability_score'],
            'is_stable': stability_result['is_stable']
        }
    }

    with open('optimal_params.json', 'w', encoding='utf-8') as f:
        json.dump(recommendation, f, indent=2, default=str, ensure_ascii=False)
    print("最优参数已保存: optimal_params.json")

    return recommendation


if __name__ == '__main__':
    run_optimizer_demo()