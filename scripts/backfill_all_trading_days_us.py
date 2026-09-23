"""
補齊美股所有交易日的篩選結果（只存 DB，不匯出 Google Sheet）

用法：
    source .venv/bin/activate
    python scripts/backfill_all_trading_days_us.py                # 從 2025-07-01 開始補齊
    python scripts/backfill_all_trading_days_us.py --since 2026-01-01  # 從指定日期開始
"""
import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from config.us_settings import US_SQLITE_DB_PATH
from data.us_database import USSQLiteDatabase
from scripts.backfill_fridays_us import (
    backfill_us_market_index,
    run_us_filters_for_date,
)


def get_missing_dates(since: str) -> list[str]:
    """找出有股價但缺少篩選結果的美股交易日"""
    conn = sqlite3.connect(US_SQLITE_DB_PATH)
    existing = set(
        r[0]
        for r in conn.execute("SELECT DISTINCT filter_date FROM us_filter_result")
    )
    price_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date FROM us_daily_price WHERE date >= ? ORDER BY date",
            (since,),
        )
    ]
    conn.close()
    return [d for d in price_dates if d not in existing]


def main():
    parser = argparse.ArgumentParser(description="補齊美股所有交易日的篩選結果")
    parser.add_argument(
        "--since",
        default="2025-07-01",
        help="從哪一天開始補齊 (預設 2025-07-01)",
    )
    args = parser.parse_args()

    db = USSQLiteDatabase()
    missing = get_missing_dates(args.since)

    if not missing:
        logger.info("美股沒有需要補齊的日期")
        return

    logger.info(f"美股需要補齊 {len(missing)} 天: {missing[0]} ~ {missing[-1]}")

    # 先補齊大盤指數
    start_d = date.fromisoformat(missing[0])
    end_d = date.fromisoformat(missing[-1])
    backfill_us_market_index(db, start_d, end_d)

    stock_info = db.get_stock_info_dict()
    logger.info(f"美股股票數: {len(stock_info)}")

    success = 0
    for i, date_str in enumerate(missing):
        td = date.fromisoformat(date_str)
        result = run_us_filters_for_date(db, td, stock_info)
        skipped = result.get("skipped")
        if not skipped:
            success += 1

        if (i + 1) % 10 == 0 or i == len(missing) - 1:
            logger.info(
                f"[{i + 1}/{len(missing)}] {date_str}: "
                f"VCP {result['vcp']}, 三線 {result['sanxian']}"
                f"{f' (跳過: {skipped})' if skipped else ''}"
            )

    logger.info(f"=== 美股補齊完成: {success}/{len(missing)} 天 ===")


if __name__ == "__main__":
    main()
