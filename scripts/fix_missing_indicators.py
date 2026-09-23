"""
修復缺少 indicator_json 的篩選結果

用法：
    source .venv/bin/activate
    python scripts/fix_missing_indicators.py         # 台股
    python scripts/fix_missing_indicators.py --us    # 美股
"""
import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


def fix_tw():
    """修復台股 indicator_json"""
    from config.settings import SQLITE_DB_PATH
    from data.sqlite_database import SQLiteDatabase
    from scripts.backfill_fridays import run_filters_for_date

    db = SQLiteDatabase()
    conn = sqlite3.connect(SQLITE_DB_PATH)

    # 找出缺少 indicator_json 的日期
    missing_dates = [
        r[0]
        for r in conn.execute("""
            SELECT DISTINCT filter_date FROM filter_result
            WHERE indicator_json IS NULL OR indicator_json = ''
            ORDER BY filter_date
        """)
    ]
    conn.close()

    if not missing_dates:
        logger.info("台股所有日期都有 indicator_json")
        return

    logger.info(f"台股需修復 {len(missing_dates)} 天: {missing_dates[0]} ~ {missing_dates[-1]}")

    stock_info = db.get_stock_info_dict()

    for i, date_str in enumerate(missing_dates):
        td = date.fromisoformat(date_str)
        result = run_filters_for_date(db, td, stock_info)
        skipped = result.get("skipped")

        if (i + 1) % 10 == 0 or i == len(missing_dates) - 1:
            skip_msg = f" (跳過: {skipped})" if skipped else ""
            logger.info(
                f"[{i + 1}/{len(missing_dates)}] {date_str}: "
                f"VCP {result['vcp']}, 三線 {result['sanxian']}{skip_msg}"
            )

    logger.info(f"=== 台股修復完成: {len(missing_dates)} 天 ===")


def fix_us():
    """修復美股 indicator_json"""
    from config.us_settings import US_SQLITE_DB_PATH
    from data.us_database import USSQLiteDatabase
    from scripts.backfill_fridays_us import run_us_filters_for_date

    db = USSQLiteDatabase()
    conn = sqlite3.connect(US_SQLITE_DB_PATH)

    missing_dates = [
        r[0]
        for r in conn.execute("""
            SELECT DISTINCT filter_date FROM us_filter_result
            WHERE indicator_json IS NULL OR indicator_json = ''
            ORDER BY filter_date
        """)
    ]
    conn.close()

    if not missing_dates:
        logger.info("美股所有日期都有 indicator_json")
        return

    logger.info(f"美股需修復 {len(missing_dates)} 天: {missing_dates[0]} ~ {missing_dates[-1]}")

    stock_info = db.get_stock_info_dict()

    for i, date_str in enumerate(missing_dates):
        td = date.fromisoformat(date_str)
        result = run_us_filters_for_date(db, td, stock_info)
        skipped = result.get("skipped")

        if (i + 1) % 10 == 0 or i == len(missing_dates) - 1:
            skip_msg = f" (跳過: {skipped})" if skipped else ""
            logger.info(
                f"[{i + 1}/{len(missing_dates)}] {date_str}: "
                f"VCP {result['vcp']}, 三線 {result['sanxian']}{skip_msg}"
            )

    logger.info(f"=== 美股修復完成: {len(missing_dates)} 天 ===")


def main():
    parser = argparse.ArgumentParser(description="修復缺少 indicator_json 的篩選結果")
    parser.add_argument("--us", action="store_true", help="修復美股（預設台股）")
    parser.add_argument("--all", action="store_true", help="同時修復台股和美股")
    args = parser.parse_args()

    if args.all:
        fix_tw()
        fix_us()
    elif args.us:
        fix_us()
    else:
        fix_tw()


if __name__ == "__main__":
    main()
