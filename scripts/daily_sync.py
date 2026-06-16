"""
每日数据同步脚本 — 收盘后自动更新日线数据
用法: python scripts/daily_sync.py
建议: Windows 任务计划程序，每个交易日下午 16:00 执行
"""

import sys
import os
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.database import SQLiteManager
from data.sync_service import DataSyncService
from data.sources import get_data_source
from utils.logger import get_logger

logger = get_logger('daily_sync', 'daily_sync.log')


def main():
    logger.info("=" * 50)
    logger.info(f"每日数据同步开始 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    db = SQLiteManager()
    source = get_data_source()
    svc = DataSyncService(db, source)

    try:
        # 1. 获取所有股票代码
        codes = db.get_all_stock_codes()
        ts_codes = [
            f"{c}.SH" if c.startswith('6') else f"{c}.SZ"
            for c in codes
        ]
        logger.info(f"股票数量: {len(ts_codes)}")

        # 2. 补齐最近 10 天的日线缺口
        result = svc.check_and_fill_gaps(ts_codes, days_back=10)
        logger.info(
            f"日线缺口补齐: 检查 {result['codes_checked']} 只, "
            f"补齐 {result['gaps_filled']} 只, "
            f"新增 {result['bars_added']} 条"
        )

        # 3. 同步除权除息信息（每周一次即可，周末做）
        if datetime.now().weekday() == 6:  # 周日
            xdxr_count = svc.sync_xdxr(ts_codes)
            logger.info(f"除权信息同步: {xdxr_count} 条")

        # 4. 检查最新数据日期
        sample_code = ts_codes[0]
        latest = db.get_latest_trade_date(sample_code)
        logger.info(f"最新数据日期: {latest}")

    except Exception as e:
        logger.error(f"同步失败: {e}", exc_info=True)
    finally:
        db.close()

    logger.info(f"每日数据同步结束 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
