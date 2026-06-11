"""
参数优化算法
支持网格搜索、随机搜索、贝叶斯优化
"""

import numpy as np
import pandas as pd
from itertools import product
from typing import List, Dict, Callable, Tuple
from dataclasses import dataclass, field


@dataclass
class ParamRange:
    """参数范围定义"""
    name: str
    min_val: float
    max_val: float
    step: float
    param_type: str = 'float'

    def generate_values(self) -> list:
        if self.param_type == 'int':
            return list(range(int(self.min_val), int(self.max_val) + 1, int(self.step)))
        return list(np.arange(self.min_val, self.max_val + self.step / 2, self.step))


@dataclass
class ParamSpace:
    """参数空间"""
    params: List[ParamRange] = field(default_factory=list)

    def add_param(self, name: str, min_val: float, max_val: float,
                  step: float, param_type: str = 'float'):
        self.params.append(ParamRange(name, min_val, max_val, step, param_type))
        return self

    def get_grid(self) -> List[dict]:
        """生成网格搜索参数组合"""
        if not self.params:
            return [{}]
        param_values = [p.generate_values() for p in self.params]
        param_names = [p.name for p in self.params]
        return [dict(zip(param_names, combo)) for combo in product(*param_values)]

    def get_random_sample(self, n_samples: int) -> List[dict]:
        """随机采样参数组合"""
        samples = []
        for _ in range(n_samples):
            sample = {}
            for p in self.params:
                if p.param_type == 'int':
                    sample[p.name] = np.random.randint(p.min_val, p.max_val + 1)
                else:
                    sample[p.name] = np.random.uniform(p.min_val, p.max_val)
            samples.append(sample)
        return samples

    @property
    def size(self) -> int:
        total = 1
        for p in self.params:
            total *= len(p.generate_values())
        return total


class ObjectiveFunction:
    """目标函数 —— 使用真实回测引擎评估参数"""

    def __init__(self, market_data: dict, base_config: dict = None):
        self.market_data = market_data
        self.base_config = base_config or {}
        self._cache = {}  # 缓存回测结果，避免重复计算

    def _params_key(self, params: dict) -> str:
        """生成参数缓存键"""
        return str(sorted(params.items()))

    def evaluate(self, params: dict) -> float:
        """评估参数组合，返回综合得分"""
        key = self._params_key(params)
        if key not in self._cache:
            self._cache[key] = self.run_backtest(params)
        metrics = self._cache[key]

        score = metrics.get('sharpe_ratio', 0)

        turnover_penalty = max(0, (metrics.get('annual_turnover', 0) - 15) * 0.01)
        score -= turnover_penalty

        complexity_penalty = len(params) * 0.005
        score -= complexity_penalty

        drawdown_penalty = max(0, abs(metrics.get('max_drawdown', 0)) - 0.15) * 2
        score -= drawdown_penalty

        return score

    def run_backtest(self, params: dict) -> dict:
        """
        使用真实回测引擎评估参数（结果会被缓存）
        """
        key = self._params_key(params)
        if key in self._cache:
            return self._cache[key]

        from backtest.engine import BacktestEngine, BacktestConfig
        from strategy.eight_factor import EightFactorStrategy
        from config.settings import FACTOR_WEIGHTS, BACKTEST_CONFIG
        import copy

        # 1. 构建因子权重
        custom_weights = copy.deepcopy(FACTOR_WEIGHTS)
        weight_map = {
            'w_EP': 'EP',
            'w_growth': 'profit_growth',
            'w_reversal': 'reversal_20d',
            'w_quality': 'ROE',
        }
        for param_key, weight_key in weight_map.items():
            if param_key in params:
                custom_weights[weight_key] = params[param_key]

        # 归一化权重
        total = sum(custom_weights.values())
        if total > 0:
            for k in custom_weights:
                custom_weights[k] /= total

        # 2. 构建回测配置
        bt_dict = copy.deepcopy(BACKTEST_CONFIG)
        if 'max_position_num' in params:
            bt_dict['max_position_num'] = int(params['max_position_num'])
        if 'stop_loss_rate' in params:
            bt_dict['stop_loss_rate'] = params['stop_loss_rate']
        bt_dict['rebalance_frequency'] = 'weekly'
        config = BacktestConfig.from_dict(bt_dict)

        # 3. 创建引擎和策略
        engine = BacktestEngine(config)
        strategy = EightFactorStrategy({
            'max_position_num': config.max_position_num,
            'max_single_weight': config.max_single_weight,
            'max_industry_weight': bt_dict.get('max_industry_weight', 0.30),
        })
        strategy.factor_engine.weights = custom_weights

        # 4. 运行回测（不打印每日报告，提高速度）
        result = engine.run(self.market_data, strategy, print_report=False)

        self._cache[key] = result['metrics']
        return result['metrics']


class GridSearchOptimizer:
    """网格搜索优化器"""

    def __init__(self, objective_func: ObjectiveFunction):
        self.objective = objective_func
        self.results = []

    def optimize(self, param_space: ParamSpace, verbose: bool = True) -> dict:
        grid = param_space.get_grid()
        total = len(grid)

        if verbose:
            print(f"网格搜索：共 {total} 种参数组合")

        best_score = -np.inf
        best_params = None
        best_metrics = None

        for i, params in enumerate(grid):
            score = self.objective.evaluate(params)
            metrics = self.objective.run_backtest(params)

            self.results.append({'params': params, 'score': score, 'metrics': metrics})

            if score > best_score:
                best_score = score
                best_params = params
                best_metrics = metrics

            if verbose and (i + 1) % max(1, total // 10) == 0:
                print(f"进度: {i+1}/{total} | 当前最优得分: {best_score:.4f}")

        return {
            'best_params': best_params,
            'best_score': best_score,
            'best_metrics': best_metrics,
            'all_results': self.results
        }


class RandomSearchOptimizer:
    """随机搜索优化器"""

    def __init__(self, objective_func: ObjectiveFunction):
        self.objective = objective_func
        self.results = []

    def optimize(self, param_space: ParamSpace, n_iter: int = 100, verbose: bool = True) -> dict:
        samples = param_space.get_random_sample(n_iter)

        if verbose:
            print(f"随机搜索：共 {n_iter} 次迭代")

        best_score = -np.inf
        best_params = None
        best_metrics = None

        for i, params in enumerate(samples):
            score = self.objective.evaluate(params)
            metrics = self.objective.run_backtest(params)

            self.results.append({'params': params, 'score': score, 'metrics': metrics})

            if score > best_score:
                best_score = score
                best_params = params
                best_metrics = metrics

            if verbose and (i + 1) % max(1, n_iter // 10) == 0:
                print(f"进度: {i+1}/{n_iter} | 当前最优得分: {best_score:.4f}")

        return {
            'best_params': best_params,
            'best_score': best_score,
            'best_metrics': best_metrics,
            'all_results': self.results
        }


class BayesianOptimizer:
    """贝叶斯优化器"""

    def __init__(self, objective_func: ObjectiveFunction):
        self.objective = objective_func
        self.results = []
        self.X_observed = []
        self.y_observed = []

    def _params_to_vector(self, params: dict, param_names: list) -> np.ndarray:
        return np.array([params.get(name, 0) for name in param_names])

    def _surrogate_model(self, X_train, y_train, X_pred):
        from scipy.spatial.distance import cdist

        if len(X_train) == 0:
            return np.zeros(len(X_pred)), np.ones(len(X_pred))

        distances = cdist(X_pred, X_train)
        k = min(5, len(X_train))
        nearest_indices = distances.argsort(axis=1)[:, :k]

        means = np.array([np.mean(y_train[idx]) for idx in nearest_indices])
        stds = np.array([np.std(y_train[idx]) if len(idx) > 1 else 1.0 for idx in nearest_indices])

        return means, stds

    def _acquisition_function(self, means, stds, best_y):
        from scipy.stats import norm
        z = (means - best_y) / (stds + 1e-9)
        return (means - best_y) * norm.cdf(z) + stds * norm.pdf(z)

    def optimize(self, param_space: ParamSpace, n_init: int = 10,
                 n_iter: int = 50, verbose: bool = True) -> dict:
        param_names = [p.name for p in param_space.params]

        if verbose:
            print(f"贝叶斯优化：{n_init}次初始采样 + {n_iter}次迭代")

        init_samples = param_space.get_random_sample(n_init)

        for params in init_samples:
            score = self.objective.evaluate(params)
            metrics = self.objective.run_backtest(params)

            self.results.append({'params': params, 'score': score, 'metrics': metrics})
            self.X_observed.append(self._params_to_vector(params, param_names))
            self.y_observed.append(score)

        best_score = max(self.y_observed)
        best_idx = np.argmax(self.y_observed)
        best_params = self.results[best_idx]['params']
        best_metrics = self.results[best_idx]['metrics']

        for i in range(n_iter):
            candidates = param_space.get_random_sample(100)
            X_candidates = np.array([self._params_to_vector(c, param_names) for c in candidates])

            X_observed = np.array(self.X_observed)
            y_observed = np.array(self.y_observed)

            means, stds = self._surrogate_model(X_observed, y_observed, X_candidates)
            acq_values = self._acquisition_function(means, stds, best_score)
            next_idx = np.argmax(acq_values)
            next_params = candidates[next_idx]

            score = self.objective.evaluate(next_params)
            metrics = self.objective.run_backtest(next_params)

            self.results.append({'params': next_params, 'score': score, 'metrics': metrics})
            self.X_observed.append(self._params_to_vector(next_params, param_names))
            self.y_observed.append(score)

            if score > best_score:
                best_score = score
                best_params = next_params
                best_metrics = metrics

            if verbose and (i + 1) % 10 == 0:
                print(f"迭代 {i+1}/{n_iter} | 当前最优得分: {best_score:.4f}")

        return {
            'best_params': best_params,
            'best_score': best_score,
            'best_metrics': best_metrics,
            'all_results': self.results,
            'convergence': self.y_observed
        }