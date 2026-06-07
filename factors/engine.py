"""
8因子计算引擎
"""

import pandas as pd
import numpy as np
import statsmodels.api as sm

from config.settings import FACTOR_WEIGHTS


class FactorEngine:
    """8因子计算引擎"""

    def __init__(self, weights=None):
        self.weights = weights or FACTOR_WEIGHTS

    @staticmethod
    def calculate_raw_factors(stocks: list) -> pd.DataFrame:
        """计算8个原始因子"""
        data = []
        for s in stocks:
            data.append({
                'ts_code': s.get('ts_code', s.get('code', '')),
                'industry': s.get('industry', ''),
                'market_cap': s.get('market_cap', 0),
                'EP': s.get('ep', 0),
                'profit_growth': s.get('profit_growth', 0),
                'revenue_growth': s.get('revenue_growth', 0),
                'reversal_20d': -s.get('return_20d', 0),
                'turnover_neg': -s.get('turnover', 0),
                'volatility_neg': -s.get('volatility', 0),
                'ROE': s.get('roe', 0),
                'accrual_neg': -s.get('accrual_ratio', 0),
            })

        df = pd.DataFrame(data).set_index('ts_code')
        return df

    @staticmethod
    def mad_winsorize(series, n=3):
        """MAD去极值"""
        median = series.median()
        mad = (series - median).abs().median()
        upper = median + n * 1.4826 * mad
        lower = median - n * 1.4826 * mad
        return series.clip(lower, upper)

    @staticmethod
    def zscore_standardize(series):
        """Z-score标准化"""
        std = series.std()
        if std == 0 or pd.isna(std):
            return series * 0
        return (series - series.mean()) / std

    @classmethod
    def process_factor(cls, factor_series, industry_series):
        """处理单个因子：缺失值→去极值→标准化→行业中性化"""
        factor = factor_series.copy()

        # 缺失值：行业均值填充
        if industry_series is not None and not industry_series.empty:
            factor = factor.groupby(industry_series).transform(
                lambda x: x.fillna(x.mean())
            )
        factor = factor.fillna(0)

        # 去极值
        factor = cls.mad_winsorize(factor)

        # 标准化
        factor = cls.zscore_standardize(factor)

        # 行业中性化
        if industry_series is not None and not industry_series.empty:
            factor = factor.groupby(industry_series).transform(
                lambda x: (x - x.mean()) / x.std() if x.std() > 0 else x * 0
            )
            factor = factor.fillna(0)

        return factor

    @classmethod
    def calculate_factor_score(cls, raw_df: pd.DataFrame, weights: dict = None) -> pd.Series:
        """计算综合因子得分"""
        if weights is None:
            weights = FACTOR_WEIGHTS

        industry = raw_df['industry'] if 'industry' in raw_df.columns else None

        processed = pd.DataFrame(index=raw_df.index)
        for col in weights.keys():
            if col in raw_df.columns:
                processed[col] = cls.process_factor(raw_df[col], industry)

        score = pd.Series(0.0, index=raw_df.index)
        for col, weight in weights.items():
            if col in processed.columns:
                score += processed[col].fillna(0) * weight

        return score

    def get_factor_ranking(self, stocks: list, top_n: int = None) -> pd.DataFrame:
        """获取因子排名"""
        raw_df = self.calculate_raw_factors(stocks)
        score = self.calculate_factor_score(raw_df, self.weights)

        result = pd.DataFrame({
            'factor_score': score,
            'factor_rank': score.rank(ascending=False, method='min'),
            'factor_rank_pct': score.rank(pct=True)
        })

        result = result.sort_values('factor_score', ascending=False)

        if top_n:
            result = result.head(top_n)

        return result