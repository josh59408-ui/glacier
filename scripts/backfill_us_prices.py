"""
補抓美股歷史股價並重算篩選結果

用法：
    source .venv/bin/activate
    python scripts/backfill_us_prices.py                     # 預設補到 2024-05-01
    python scripts/backfill_us_prices.py --since 2024-01-01  # 指定起始日期
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from api.us_stock_client_free import USStockClientFree
from data.us_database import USSQLiteDatabase
from scripts.backfill_fridays_us import (
    backfill_us_market_index,
    run_us_filters_for_date,
)


def main():
    parser = argparse.ArgumentParser(description="補抓美股歷史股價")
    parser.add_argument(
        "--since",
        default="2024-05-01",
        help="補抓起始日期 (預設 2024-05-01，對齊台股)",
    )
    parser.add_argument(
        "--skip-filter",
        action="store_true",
        help="只抓股價，不重算篩選",
    )
    args = parser.parse_args()

    db = USSQLiteDatabase()
    client = USStockClientFree()

    since = date.fromisoformat(args.since)

    # 取得目前 DB 最早的股價日期
    import sqlite3
    from config.us_settings import US_SQLITE_DB_PATH

    conn = sqlite3.connect(US_SQLITE_DB_PATH)
    row = conn.execute("SELECT MIN(date) FROM us_daily_price").fetchone()
    current_min = date.fromisoformat(row[0]) if row[0] else date.today()
    conn.close()

    if since >= current_min:
        logger.info(f"DB 已有 {current_min} 的資料，不需要補抓")
        return

    logger.info(f"目前 DB 最早: {current_min}")
    logger.info(f"需要補抓: {since} ~ {current_min - timedelta(days=1)}")

    # Step 1: 取得股票清單
    stock_info = db.get_stock_info_dict()
    stock_ids = list(stock_info.keys())
    logger.info(f"股票數: {len(stock_ids)}")

    # Step 2: 下載歷史股價
    end_date = current_min - timedelta(days=1)
    logger.info(f"=== Step 1: 下載股價 {since} ~ {end_date} ===")

    price_df = client.get_stock_price(since, end_date, stock_ids=stock_ids)

    if price_df.empty:
        logger.error("無法下載股價資料")
        return

    logger.info(f"下載到 {len(price_df)} 筆股價")

    # Step 3: 存入 DB
    count = db.upsert_daily_price(price_df)
    logger.info(f"已存入 {count} 筆股價")

    # Step 4: 補齊大盤指數
    logger.info(f"=== Step 2: 補齊大盤指數 ===")
    backfill_us_market_index(db, since, end_date)

    if args.skip_filter:
        logger.info("跳過篩選計算（--skip-filter）")
        return

    # Step 5: 計算缺少的篩選結果
    logger.info(f"=== Step 3: 計算篩選結果 ===")

    conn = sqlite3.connect(US_SQLITE_DB_PATH)
    existing_filters = set(
        r[0]
        for r in conn.execute("SELECT DISTINCT filter_date FROM us_filter_result")
    )
    # 需要 MA200 歷史，所以篩選起點 = since + 200 交易日 ≈ since + 10 個月
    filter_start = since + timedelta(days=300)
    price_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date FROM us_daily_price WHERE date >= ? ORDER BY date",
            (filter_start.isoformat(),),
        )
    ]
    conn.close()

    missing = [d for d in price_dates if d not in existing_filters]
    if not missing:
        logger.info("沒有需要計算的篩選日期")
        return

    logger.info(f"需要計算 {len(missing)} 天篩選結果: {missing[0]} ~ {missing[-1]}")

    success = 0
    for i, date_str in enumerate(missing):
        td = date.fromisoformat(date_str)
        result = run_us_filters_for_date(db, td, stock_info)
        if not result.get("skipped"):
            success += 1

        if (i + 1) % 10 == 0 or i == len(missing) - 1:
            skipped = result.get("skipped")
            skip_msg = f" (跳過: {skipped})" if skipped else ""
            logger.info(
                f"[{i + 1}/{len(missing)}] {date_str}: "
                f"VCP {result['vcp']}, 三線 {result['sanxian']}{skip_msg}"
            )

    logger.info(f"=== 完成: {success}/{len(missing)} 天 ===")


if __name__ == "__main__":
    main()
