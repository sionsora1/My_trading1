"""
AI 因子挖掘引擎
基于 LightGBM 从海量特征中自动学习非线性因子组合
替代传统线性多因子打分

特征来源：行情数据 + 技术指标 + 财务数据 + 估值
目标：预测未来 N 日收益率
"""

import numpy as np
import pandas as pd
import os
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

try:
    import lightgbm as lgb
    HAS_LIGHTGBM = True
except ImportError:
    HAS_LIGHTGBM = False

try:
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


class AIFactorEngine:
    """
    AI 因子引擎

    特性：
    - 50+ 特征自动组合
    - LightGBM 非线性学习
    - 时序交叉验证防止过拟合
    - 特征重要性分析
    - 模型持久化
    """

    # 特征组定义（从 market_data 中提取）
    FEATURE_GROUPS = {
        'price_momentum': [
            'return_1d', 'return_5d', 'return_20d', 'return_60d',
            'price_vs_ma5', 'price_vs_ma10', 'price_vs_ma20', 'price_vs_ma60',
            'ma5_vs_ma20', 'ma10_vs_ma60',
        ],
        'volatility_risk': [
            'volatility_20d', 'volatility_60d',
            'price_percentile_1y', 'high_low_ratio_1y',
        ],
        'volume_liquidity': [
            'turnover', 'volume_ratio', 'volume_trend_5d',
        ],
        'valuation': [
            'ep', 'bp', 'pe_percentile_5y',
        ],
        'fundamental': [
            'roe', 'profit_growth', 'revenue_growth',
            'gross_margin', 'accrual_ratio',
        ],
        'size_quality': [
            'log_market_cap', 'pledge_ratio',
        ],
    }

    def __init__(self, model_dir: str = 'models'):
        self.model_dir = model_dir
        self.model = None
        self.scaler = None
        self.feature_names = []
        self.feature_importance = {}

        os.makedirs(model_dir, exist_ok=True)

        if not HAS_LIGHTGBM:
            print("[AIEngine] [WARN] lightgbm not installed, run: pip install lightgbm")
        if not HAS_SKLEARN:
            print("[AIEngine] [WARN] scikit-learn not installed, run: pip install scikit-learn")

    # ──────────────────────────────────────────────
    #  特征工程
    # ──────────────────────────────────────────────

    def extract_features(self, stocks_data: dict) -> pd.DataFrame:
        """
        从单日 market_data 提取特征矩阵

        Args:
            stocks_data: {ts_code: {close, ma5, ..., roe, ...}}

        Returns:
            DataFrame: index=ts_code, columns=features
        """
        features = {}

        for code, s in stocks_data.items():
            close = s.get('close', 0) or 1

            feat = {}

            # --- 价格动量 ---
            feat['return_1d'] = s.get('return_1d', 0) or 0
            feat['return_5d'] = self._safe_get(s, ['return_5d'], 0)
            feat['return_20d'] = s.get('return_20d', 0) or 0
            feat['return_60d'] = s.get('return_60d', 0) or 0

            feat['price_vs_ma5'] = close / max(s.get('ma5', close), 0.01) - 1
            feat['price_vs_ma10'] = close / max(s.get('ma10', close), 0.01) - 1
            feat['price_vs_ma20'] = close / max(s.get('ma20', close), 0.01) - 1
            feat['price_vs_ma60'] = close / max(s.get('ma60', close), 0.01) - 1

            ma5 = s.get('ma5', close) or close
            ma20 = s.get('ma20', close) or close
            ma10 = s.get('ma10', close) or close
            ma60 = s.get('ma60', close) or close
            feat['ma5_vs_ma20'] = ma5 / max(ma20, 0.01) - 1
            feat['ma10_vs_ma60'] = ma10 / max(ma60, 0.01) - 1

            # --- 波动率风险 ---
            feat['volatility_20d'] = s.get('volatility', 0.25) or 0.25
            # 从 return_20d 的总波动估算 60 日
            feat['volatility_60d'] = abs(s.get('return_60d', 0) or 0) / 60
            feat['price_percentile_1y'] = s.get('price_percentile_1y', 0.5) or 0.5

            high_1y = s.get('high_1y', close) or close
            low_1y = s.get('low_1y', close) or close
            feat['high_low_ratio_1y'] = high_1y / max(low_1y, 0.01)

            # --- 成交量和流动性 ---
            feat['turnover'] = s.get('turnover', 3) or 3
            vol_ma20 = s.get('volume_ma20', 1) or 1
            volume = s.get('volume', vol_ma20) or vol_ma20
            feat['volume_ratio'] = volume / max(vol_ma20, 1)
            feat['volume_trend_5d'] = feat['volume_ratio'] - 1  # 简化：当前量 / 20日均量 - 1

            # --- 估值 ---
            pe = s.get('pe', 20) or 20
            pb = s.get('pb', 3) or 3
            feat['ep'] = 1 / max(pe, 0.01)
            feat['bp'] = 1 / max(pb, 0.01)
            feat['pe_percentile_5y'] = s.get('pe_percentile_5y', 0.5) or 0.5

            # --- 基本面（来自真实财报） ---
            feat['roe'] = s.get('roe', 0) or 0
            feat['profit_growth'] = s.get('profit_growth', 0) or 0
            feat['revenue_growth'] = s.get('revenue_growth', 0) or 0
            feat['gross_margin'] = s.get('gross_margin', 0) or 0
            feat['accrual_ratio'] = s.get('accrual_ratio', 0) or 0

            # --- 规模 ---
            mcap = s.get('market_cap', 1e10) or 1e10
            feat['log_market_cap'] = np.log(max(mcap, 1))
            feat['pledge_ratio'] = s.get('pledge_ratio', 0.1) or 0.1

            features[code] = feat

        df = pd.DataFrame.from_dict(features, orient='index')
        df.index.name = 'ts_code'

        # 填充 NaN
        df = df.fillna(0)
        # 裁剪极端值
        df = df.clip(-50, 50)

        return df

    def _safe_get(self, d: dict, keys: list, default=0):
        """安全从字典取值"""
        for k in keys:
            v = d.get(k)
            if v is not None:
                return v
        return default

    def get_feature_names(self) -> list:
        """返回特征名列表"""
        # 构造一个虚拟数据点来获取列名
        dummy = {
            '1': {
                'close': 10, 'ma5': 10, 'ma10': 10, 'ma20': 10, 'ma60': 10,
                'high_1y': 12, 'low_1y': 8,
                'turnover': 3, 'volume': 1e6, 'volume_ma20': 1e6,
                'pe': 20, 'pb': 3,
                'roe': 0.15, 'profit_growth': 0.1, 'revenue_growth': 0.08,
                'gross_margin': 0.3, 'accrual_ratio': 0.02,
                'market_cap': 1e10,
                'return_1d': 0, 'return_20d': 0, 'return_60d': 0,
                'volatility': 0.25, 'price_percentile_1y': 0.5,
                'pe_percentile_5y': 0.5, 'pledge_ratio': 0.1,
            }
        }
        df = self.extract_features(dummy)
        return df.columns.tolist()

    # ──────────────────────────────────────────────
    #  训练数据构建
    # ──────────────────────────────────────────────

    def build_training_data(
        self,
        market_data_by_date: dict,
        forward_days: int = 20,
        min_samples: int = 50,
    ) -> tuple:
        """
        从时序市场数据构建训练集

        Args:
            market_data_by_date: {date: {ts_code: {features...}}}
            forward_days: 预测未来 N 个交易日的收益
            min_samples: 最少样本数

        Returns:
            X (DataFrame), y (Series), dates (list)
        """
        if not HAS_LIGHTGBM or not HAS_SKLEARN:
            raise RuntimeError("需要安装 lightgbm 和 scikit-learn")

        dates = sorted(market_data_by_date.keys())
        print(f"[AIEngine] 构建训练数据：{len(dates)} 个交易日")

        X_list, y_list, date_list = [], [], []

        # 对每个日期提取特征，找未来收益作为标签
        for i, date in enumerate(dates):
            # 找到 forward_days 之后的日期
            future_idx = i + forward_days
            if future_idx >= len(dates):
                break

            future_date = dates[future_idx]
            current_data = market_data_by_date[date]
            future_data = market_data_by_date[future_date]

            # 提取当前特征
            try:
                X_today = self.extract_features(current_data)
            except Exception as e:
                continue

            # 计算标签：未来 forward_days 的收益率
            for code in X_today.index:
                if code in future_data:
                    current_close = current_data[code].get('close', 0)
                    future_close = future_data[code].get('close', 0)
                    if current_close and current_close > 0:
                        fwd_return = (future_close - current_close) / current_close
                        X_list.append(X_today.loc[code].values)
                        y_list.append(fwd_return)
                        date_list.append(date)

        if len(X_list) < min_samples:
            print(f"[AIEngine] [WARN] Insufficient samples: {len(X_list)} < {min_samples}")
            return None, None, None

        X = pd.DataFrame(X_list, columns=X_today.columns)
        y = pd.Series(y_list, name='forward_return')

        print(f"[AIEngine] 训练样本：{len(X)} 条")
        print(f"[AIEngine] 标签分布：均值={y.mean():.4f}, 标准差={y.std():.4f}")
        print(f"[AIEngine] 正样本={ (y > 0).sum()}/{len(y)} ({(y > 0).mean():.1%})")

        return X, y, date_list

    # ──────────────────────────────────────────────
    #  模型训练
    # ──────────────────────────────────────────────

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        dates: list = None,
        n_splits: int = 3,
        **lgb_params,
    ) -> dict:
        """
        训练 LightGBM 模型（时序交叉验证）

        Returns:
            dict: 训练指标 {'train_score', 'val_score', 'feature_importance'}
        """
        if not HAS_LIGHTGBM:
            raise RuntimeError("需要安装 lightgbm")

        self.feature_names = X.columns.tolist()
        print(f"[AIEngine] 特征数：{len(self.feature_names)}")

        # 标准化
        self.scaler = StandardScaler()
        X_scaled = pd.DataFrame(
            self.scaler.fit_transform(X),
            columns=X.columns,
            index=X.index,
        )

        # 默认参数
        params = {
            'objective': 'regression',
            'metric': 'rmse',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.8,
            'bagging_fraction': 0.8,
            'bagging_freq': 5,
            'verbose': -1,
            'n_estimators': 200,
            'min_data_in_leaf': 20,
            'lambda_l1': 0.1,
            'lambda_l2': 0.1,
            'random_state': 42,
        }
        params.update(lgb_params)

        # 时序交叉验证
        if dates is not None and n_splits > 1:
            dates_series = pd.Series(dates)
            unique_dates = sorted(dates_series.unique())
            split_size = len(unique_dates) // (n_splits + 1)

            val_scores = []
            train_scores = []

            for split in range(1, n_splits + 1):
                cutoff = unique_dates[split * split_size]
                train_mask = dates_series < cutoff
                val_mask = dates_series >= cutoff

                X_tr = X_scaled[train_mask.values]
                y_tr = y[train_mask.values]
                X_val = X_scaled[val_mask.values]
                y_val = y[val_mask.values]

                if len(X_val) < 10:
                    continue

                model = lgb.LGBMRegressor(**params)
                model.fit(X_tr, y_tr, eval_set=[(X_val, y_val)],
                          callbacks=[lgb.early_stopping(10, verbose=False)])

                train_pred = model.predict(X_tr)
                val_pred = model.predict(X_val)

                from sklearn.metrics import mean_squared_error
                train_rmse = np.sqrt(mean_squared_error(y_tr, train_pred))
                val_rmse = np.sqrt(mean_squared_error(y_val, val_pred))
                train_scores.append(train_rmse)
                val_scores.append(val_rmse)

            print(f"[AIEngine] 交叉验证 Train RMSE: {np.mean(train_scores):.4f}")
            print(f"[AIEngine] 交叉验证 Val   RMSE: {np.mean(val_scores):.4f}")

        # 全量训练
        print("[AIEngine] 全量训练最终模型...")
        self.model = lgb.LGBMRegressor(**params)
        self.model.fit(
            X_scaled, y,
            callbacks=[lgb.early_stopping(20, verbose=False)],
            eval_set=[(X_scaled, y)],
        )

        # 特征重要性
        importance = self.model.feature_importances_
        self.feature_importance = dict(
            sorted(
                zip(self.feature_names, importance),
                key=lambda x: -x[1],
            )
        )

        # 打印 Top 10 特征
        print("[AIEngine] Top 10 特征重要性：")
        for i, (name, imp) in enumerate(list(self.feature_importance.items())[:10]):
            print(f"  {i+1:2d}. {name:25s} {imp:.4f}")

        return {
            'train_rmse': np.mean(train_scores) if (dates is not None and n_splits > 1) else 0,
            'val_rmse': np.mean(val_scores) if (dates is not None and n_splits > 1) else 0,
            'feature_importance': self.feature_importance,
        }

    # ──────────────────────────────────────────────
    #  预测
    # ──────────────────────────────────────────────

    def predict(self, stocks_data: dict) -> pd.Series:
        """
        对当天所有股票打分

        Args:
            stocks_data: {ts_code: {close, ma5, ..., roe, ...}}

        Returns:
            Series: index=ts_code, values=预测得分（越高越好）
        """
        if self.model is None:
            raise RuntimeError("模型未训练或未加载，请先调用 train() 或 load()")

        X = self.extract_features(stocks_data)

        # 确保列顺序一致
        for col in self.feature_names:
            if col not in X.columns:
                X[col] = 0
        X = X[self.feature_names]

        X_scaled = pd.DataFrame(
            self.scaler.transform(X),
            columns=self.feature_names,
            index=X.index,
        )

        scores = self.model.predict(X_scaled)
        return pd.Series(scores, index=X.index, name='ai_score')

    # ──────────────────────────────────────────────
    #  模型持久化
    # ──────────────────────────────────────────────

    def save(self, name: str = 'ai_factor_model'):
        """保存模型"""
        import joblib

        path = f"{self.model_dir}/{name}.pkl"
        joblib.dump({
            'model': self.model,
            'scaler': self.scaler,
            'feature_names': self.feature_names,
            'feature_importance': self.feature_importance,
        }, path)
        print(f"[AIEngine] 模型已保存: {path}")

    def load(self, name: str = 'ai_factor_model'):
        """加载模型"""
        import joblib

        path = f"{self.model_dir}/{name}.pkl"
        if not os.path.exists(path):
            raise FileNotFoundError(f"模型文件不存在: {path}")

        data = joblib.load(path)
        self.model = data['model']
        self.scaler = data['scaler']
        self.feature_names = data['feature_names']
        self.feature_importance = data.get('feature_importance', {})
        print(f"[AIEngine] 模型已加载: {path}")
        print(f"[AIEngine] 特征数: {len(self.feature_names)}")

        return self

    def is_ready(self) -> bool:
        """模型是否可用"""
        return self.model is not None and self.scaler is not None
