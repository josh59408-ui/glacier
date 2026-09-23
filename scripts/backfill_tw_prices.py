"""
台股歷史股價回補腳本

將股價資料回補到 2024-01-02，確保 MA200 從 2024-10-23 起有完整資料。
使用 HybridClient（FinMind + yfinance 備援）分月下載。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger

from api.hybrid_client import HybridClient
from data.sqlite_database import SQLiteDatabase


def main():
    db = SQLiteDatabase()
    client = HybridClient()

    import sqlite3
    from config.settings import SQLITE_DB_PATH

    # 取得股票清單
    stock_info = db.get_stock_info_dict()
    stock_ids = list(stock_info.keys())
    market_types = db.get_stock_market_types()
    logger.info(f"股票數: {len(stock_ids)} 檔")

    # 分月下載：2024-01 ~ 2024-05
    months = [
        (date(2024, 1, 2), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 31)),
        (date(2024, 4, 1), date(2024, 4, 30)),
        (date(2024, 5, 1), date(2024, 5, 15)),
    ]

    total_count = 0
    for start, end in months:
        # 檢查這個月是否已有足夠資料
        conn = sqlite3.connect(SQLITE_DB_PATH)
        existing = conn.execute(
            "SELECT COUNT(DISTINCT stock_id) FROM daily_price WHERE date >= ? AND date <= ?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        conn.close()

        if existing >= 1500:
            logger.info(f"  {start} ~ {end}: 已有 {existing} 檔，跳過")
            continue

        logger.info(f"下載 {start} ~ {end} (目前 {existing} 檔)...")

        price_df = client.get_stock_price(
            start_date=start,
            end_date=end,
            stock_ids=stock_ids,
            market_types=market_types,
        )

        if price_df.empty:
            logger.warning(f"  {start} ~ {end}: 無資料")
            continue

        count = db.upsert_daily_price(price_df)
        total_count += count
        logger.info(f"  {start} ~ {end}: 寫入 {count} 筆")

    # 補大盤指數
    logger.info("補大盤指數 2024-01-02 ~ 2024-05-15...")
    market_df = client.get_market_index(date(2024, 1, 2), date(2024, 5, 15))
    if not market_df.empty:
        mkt_count = db.upsert_market_index(market_df)
        logger.info(f"大盤指數寫入: {mkt_count} 筆")

    # 驗證
    conn = sqlite3.connect(SQLITE_DB_PATH)
    new_min = conn.execute("SELECT MIN(date) FROM daily_price").fetchone()[0]
    total_days = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_price").fetchone()[0]

    for m in ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05']:
        r = conn.execute(
            f"SELECT COUNT(DISTINCT stock_id), COUNT(DISTINCT date) FROM daily_price "
            f"WHERE date >= '{m}-01' AND date < '{m}-32'"
        ).fetchone()
        logger.info(f"  {m}: {r[0]} 檔, {r[1]} 天")

    r = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_price WHERE date <= '2025-02-14'").fetchone()
    logger.info(f"2025-02-14 前: {r[0]} 個交易日 (MA200 {'OK' if r[0] >= 200 else 'NOT OK'})")
    conn.close()

    logger.info(f"補價完成: 股價起始 {new_min}, 共 {total_days} 個交易日, 本次新增 {total_count} 筆")


if __name__ == "__main__":
    main()
