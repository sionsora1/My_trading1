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
    """目标函数"""

    def __init__(self, market_data: dict, base_config: dict = None):
        self.market_data = market_data
        self.base_config = base_config or {}

    def evaluate(self, params: dict) -> float:
        """评估参数组合，返回综合得分"""
        metrics = self.run_backtest(params)

        score = metrics.get('sharpe_ratio', 0)

        turnover_penalty = max(0, (metrics.get('annual_turnover', 0) - 15) * 0.01)
        score -= turnover_penalty

        complexity_penalty = len(params) * 0.005
        score -= complexity_penalty

        drawdown_penalty = max(0, abs(metrics.get('max_drawdown', 0)) - 0.15) * 2
        score -= drawdown_penalty

        return score

    def run_backtest(self, params: dict) -> dict:
        """运行单次回测"""
        np.random.seed(hash(str(sorted(params.items()))) % 2**32)

        w_EP = params.get('w_EP', 0.15)
        w_growth = params.get('w_growth', 0.15)
        w_reversal = params.get('w_reversal', 0.15)
        w_quality = params.get('w_quality', 0.10)
        max_pos = params.get('max_position_num', 20)
        stop_loss = params.get('stop_loss_rate', -0.08)

        total_weight = w_EP + w_growth + w_reversal + w_quality
        if total_weight > 0:
            w_EP /= total_weight
            w_growth /= total_weight
            w_reversal /= total_weight
            w_quality /= total_weight

        base_return = 0.15
        return_adj = (
            (w_EP - 0.15) * 2 +
            (w_growth - 0.15) * 1.5 +
            (w_reversal - 0.15) * 3 +
            (w_quality - 0.10) * 1
        )
        pos_adj = -0.001 * (max_pos - 20) ** 2
        stop_adj = 2 * (stop_loss + 0.08) ** 2
        noise = np.random.normal(0, 0.02)

        total_return = base_return + return_adj + pos_adj + stop_adj + noise
        max_drawdown = -0.12 + np.random.normal(0, 0.02)
        sharpe = total_return / 0.15 + np.random.normal(0, 0.1)

        return {
            'total_return': total_return,
            'annual_return': total_return,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe,
            'calmar_ratio': total_return / abs(max_drawdown) if max_drawdown != 0 else 0,
            'win_rate': 0.52 + np.random.normal(0, 0.03),
            'profit_loss_ratio': 1.2 + np.random.normal(0, 0.1),
            'annual_turnover': 10 + np.random.normal(0, 2),
        }


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