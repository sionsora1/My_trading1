"""
过拟合验证模块
"""

import numpy as np
import pandas as pd
from typing import List, Dict, Tuple

from .search import ObjectiveFunction, ParamSpace


class OverfittingValidator:
    """过拟合检测与验证"""

    def __init__(self, market_data: dict, base_config: dict = None):
        self.market_data = market_data
        self.base_config = base_config or {}

    def walk_forward_validation(self, params: dict, n_splits: int = 5) -> dict:
        """滚动窗口验证（Walk-Forward）"""
        dates = sorted(self.market_data.keys())
        total_days = len(dates)
        window_size = total_days // n_splits

        results = []

        for i in range(n_splits):
            test_start = i * window_size
            test_end = min((i + 1) * window_size, total_days)
            train_start = max(0, test_start - window_size)

            train_dates = dates[train_start:test_start]
            test_dates = dates[test_start:test_end]

            if len(train_dates) < 50 or len(test_dates) < 20:
                continue

            train_data = {d: self.market_data[d] for d in train_dates}
            test_data = {d: self.market_data[d] for d in test_dates}

            train_obj = ObjectiveFunction(train_data, self.base_config)
            train_metrics = train_obj.run_backtest(params)

            test_obj = ObjectiveFunction(test_data, self.base_config)
            test_metrics = test_obj.run_backtest(params)

            results.append({
                'fold': i,
                'train_period': f"{train_dates[0]}~{train_dates[-1]}",
                'test_period': f"{test_dates[0]}~{test_dates[-1]}",
                'train_return': train_metrics['annual_return'],
                'test_return': test_metrics['annual_return'],
                'train_sharpe': train_metrics['sharpe_ratio'],
                'test_sharpe': test_metrics['sharpe_ratio'],
                'train_drawdown': train_metrics['max_drawdown'],
                'test_drawdown': test_metrics['max_drawdown'],
            })

        if not results:
            return {'is_overfit': True, 'overfit_score': 1.0, 'folds': []}

        avg_train_return = np.mean([r['train_return'] for r in results])
        avg_test_return = np.mean([r['test_return'] for r in results])
        avg_train_sharpe = np.mean([r['train_sharpe'] for r in results])
        avg_test_sharpe = np.mean([r['test_sharpe'] for r in results])

        return_decay = (avg_train_return - avg_test_return) / abs(avg_train_return) if avg_train_return != 0 else 0
        sharpe_decay = (avg_train_sharpe - avg_test_sharpe) / abs(avg_train_sharpe) if avg_train_sharpe != 0 else 0

        return {
            'folds': results,
            'avg_train_return': avg_train_return,
            'avg_test_return': avg_test_return,
            'avg_train_sharpe': avg_train_sharpe,
            'avg_test_sharpe': avg_test_sharpe,
            'return_decay': return_decay,
            'sharpe_decay': sharpe_decay,
            'is_overfit': return_decay > 0.3 or sharpe_decay > 0.3,
            'overfit_score': (return_decay + sharpe_decay) / 2
        }

    def cross_validation(self, params: dict, n_folds: int = 5) -> dict:
        """交叉验证"""
        dates = sorted(self.market_data.keys())
        fold_size = len(dates) // n_folds

        results = []

        for i in range(n_folds):
            test_start = i * fold_size
            test_end = min((i + 1) * fold_size, len(dates))

            train_dates = dates[:test_start] + dates[test_end:]
            test_dates = dates[test_start:test_end]

            if len(train_dates) < 50 or len(test_dates) < 20:
                continue

            train_data = {d: self.market_data[d] for d in train_dates}
            test_data = {d: self.market_data[d] for d in test_dates}

            train_obj = ObjectiveFunction(train_data, self.base_config)
            train_metrics = train_obj.run_backtest(params)

            test_obj = ObjectiveFunction(test_data, self.base_config)
            test_metrics = test_obj.run_backtest(params)

            results.append({
                'fold': i,
                'train_return': train_metrics['annual_return'],
                'test_return': test_metrics['annual_return'],
                'train_sharpe': train_metrics['sharpe_ratio'],
                'test_sharpe': test_metrics['sharpe_ratio'],
            })

        if not results:
            return {'avg_test_return': 0, 'avg_test_sharpe': 0, 'std_test_return': 0}

        return {
            'folds': results,
            'avg_train_return': np.mean([r['train_return'] for r in results]),
            'avg_test_return': np.mean([r['test_return'] for r in results]),
            'std_test_return': np.std([r['test_return'] for r in results]),
            'avg_train_sharpe': np.mean([r['train_sharpe'] for r in results]),
            'avg_test_sharpe': np.mean([r['test_sharpe'] for r in results]),
        }

    def parameter_stability_test(self, param_space: ParamSpace, top_n: int = 10) -> dict:
        """参数稳定性测试"""
        from .search import RandomSearchOptimizer

        optimizer = RandomSearchOptimizer(ObjectiveFunction(self.market_data, self.base_config))
        result = optimizer.optimize(param_space, n_iter=200, verbose=False)

        best_params = result['best_params']
        best_score = result['best_score']

        neighbors = []
        for _ in range(50):
            neighbor = {}
            for name, value in best_params.items():
                if isinstance(value, int):
                    neighbor[name] = max(1, value + np.random.randint(-2, 3))
                else:
                    neighbor[name] = value + np.random.normal(0, 0.02)
            neighbors.append(neighbor)

        neighbor_scores = []
        for params in neighbors:
            score = ObjectiveFunction(self.market_data, self.base_config).evaluate(params)
            neighbor_scores.append(score)

        score_range = max(neighbor_scores) - min(neighbor_scores) if neighbor_scores else 0

        return {
            'best_params': best_params,
            'best_score': best_score,
            'neighbor_mean_score': np.mean(neighbor_scores) if neighbor_scores else 0,
            'neighbor_std_score': np.std(neighbor_scores) if neighbor_scores else 0,
            'neighbor_range_score': score_range,
            'is_stable': score_range < 0.2,
            'stability_score': 1 - min(1, score_range / 0.5)
        }


class ResultAnalyzer:
    """优化结果分析器"""

    @staticmethod
    def find_robust_parameters(results: List[dict], top_percentile: float = 0.1) -> dict:
        """寻找稳健参数"""
        sorted_results = sorted(results, key=lambda x: x['score'], reverse=True)
        top_n = max(10, int(len(sorted_results) * top_percentile))
        top_results = sorted_results[:top_n]

        param_stats = {}
        for r in top_results:
            for name, value in r['params'].items():
                if name not in param_stats:
                    param_stats[name] = []
                param_stats[name].append(value)

        robust_params = {}
        for name, values in param_stats.items():
            robust_params[name] = {
                'mean': np.mean(values),
                'std': np.std(values),
                'median': np.median(values),
                'min': min(values),
                'max': max(values)
            }

        best_robust = {name: stats['median'] for name, stats in robust_params.items()}

        return {
            'robust_params': best_robust,
            'param_distribution': robust_params,
            'top_n': top_n,
            'top_mean_score': np.mean([r['score'] for r in top_results]),
            'top_std_score': np.std([r['score'] for r in top_results])
        }

    @staticmethod
    def generate_report(search_result: dict, validation_result: dict = None) -> str:
        """生成优化报告"""
        report = []
        report.append("=" * 60)
        report.append("参数优化报告")
        report.append("=" * 60)

        report.append("\n【最优参数】")
        for name, value in search_result['best_params'].items():
            if isinstance(value, float):
                report.append(f"  {name}: {value:.4f}")
            else:
                report.append(f"  {name}: {value}")

        report.append(f"\n【最优表现】")
        metrics = search_result['best_metrics']
        report.append(f"  综合得分: {search_result['best_score']:.4f}")
        report.append(f"  年化收益: {metrics['annual_return']:.2%}")
        report.append(f"  最大回撤: {metrics['max_drawdown']:.2%}")
        report.append(f"  夏普比率: {metrics['sharpe_ratio']:.2f}")

        report.append(f"\n【搜索统计】")
        report.append(f"  总评估次数: {len(search_result['all_results'])}")

        all_scores = [r['score'] for r in search_result['all_results']]
        report.append(f"  得分范围: {min(all_scores):.4f} ~ {max(all_scores):.4f}")

        if validation_result:
            report.append(f"\n【过拟合检验】")
            report.append(f"  训练集平均收益: {validation_result['avg_train_return']:.2%}")
            report.append(f"  测试集平均收益: {validation_result['avg_test_return']:.2%}")
            report.append(f"  收益衰减率: {validation_result['return_decay']:.2%}")

            if validation_result['is_overfit']:
                report.append(f"  [!] 警告：检测到过拟合风险！")
            else:
                report.append(f"  [OK] 过拟合风险较低")

        report.append("\n" + "=" * 60)
        return '\n'.join(report)