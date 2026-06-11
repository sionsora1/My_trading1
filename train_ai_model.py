"""
AI 因子模型训练脚本

训练流程：
1. 获取历史行情数据（多只股票，长时间跨度）
2. 构建特征 + 标签（未来20日收益）
3. LightGBM 训练（时序交叉验证）
4. 保存模型

用法：
    python train_ai_model.py                          # 默认参数训练
    python train_ai_model.py --forward_days 10        # 预测未来10日收益
    python train_ai_model.py --stocks 20              # 用20只股票
    python train_ai_model.py --start 20240101         # 从2024年开始
"""

import sys
import os
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.fetcher import DataFetcher
from factors.ai_engine import AIFactorEngine


def get_training_stock_pool(n: int = 15) -> list:
    """
    获取训练用的股票池
    覆盖不同行业和市值规模，确保模型学习到多样化特征
    """
    stocks = [
        # 大市值蓝筹
        '600519', '000858', '601318', '600036', '000333',
        '600276', '600887', '601166', '600030', '000651',
        # 中盘成长
        '002415', '300750', '000725', '002371', '600809',
        '002475', '300015', '600585', '000568', '002714',
        # 中小盘
        '002230', '300124', '600570', '000063', '002049',
        '600763', '300274', '002008', '601012', '600584',
    ]
    return stocks[:n]


def main():
    parser = argparse.ArgumentParser(description='AI因子模型训练')
    parser.add_argument('--stocks', type=int, default=15, help='训练股票数')
    parser.add_argument('--start', type=str, default='20250101', help='训练起始日期')
    parser.add_argument('--end', type=str, default=None, help='训练结束日期（默认今天）')
    parser.add_argument('--forward_days', type=int, default=20, help='预测未来N日收益')
    parser.add_argument('--n_splits', type=int, default=3, help='交叉验证折数')
    parser.add_argument('--model_name', type=str, default='ai_factor_model', help='模型文件名')
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime('%Y%m%d')
    stock_codes = get_training_stock_pool(args.stocks)

    print("=" * 60)
    print("AI 因子模型训练")
    print("=" * 60)
    print(f"  股票池:  {len(stock_codes)} 只")
    print(f"  训练期:  {args.start} ~ {end_date}")
    print(f"  预测目标: 未来 {args.forward_days} 日收益")
    print(f"  交叉验证: {args.n_splits} 折")
    print(f"  模型名称: {args.model_name}")
    print("=" * 60)

    # ── Step 1: 获取数据 ──
    print("\n[1/4] 获取历史数据...")
    fetcher = DataFetcher()
    market_data = fetcher.build_market_data_by_date(
        stock_codes=stock_codes,
        start_date=args.start,
        end_date=end_date,
    )

    if not market_data:
        print("[ERROR] 未获取到任何数据，请检查网络和日期范围")
        return

    print(f"  [OK] 获取到 {len(market_data)} 个交易日")

    # ── Step 2: 构建训练数据 ──
    print("\n[2/4] 构建训练特征...")
    engine = AIFactorEngine()
    X, y, dates = engine.build_training_data(
        market_data,
        forward_days=args.forward_days,
    )

    if X is None:
        print("[ERROR] 样本不足，无法训练。尝试扩大股票池或延长训练期")
        return

    # ── Step 3: 训练 ──
    print("\n[3/4] 训练 LightGBM 模型...")
    result = engine.train(
        X, y, dates,
        n_splits=args.n_splits,
    )

    # ── Step 4: 保存 ──
    print("\n[4/4] 保存模型...")
    engine.save(args.model_name)

    print("\n" + "=" * 60)
    print("[OK] 训练完成!")
    print(f"   模型保存在: models/{args.model_name}.pkl")
    print(f"   特征数:     {len(engine.feature_names)}")
    print(f"   训练RMSE:   {result['train_rmse']:.4f}")
    print(f"   验证RMSE:   {result['val_rmse']:.4f}")
    print("=" * 60)
    print("\n下一步：在回测中使用 'ai_factor' 策略")


if __name__ == '__main__':
    main()
