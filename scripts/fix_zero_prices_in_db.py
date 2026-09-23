"""
修復資料庫中 close_price=0 的異常資料

策略：用同一股票前一個交易日的收盤價 forward-fill 補齊
僅針對 stock_info 中的股票（排除 ETF、權證等）

使用方式：
    source .venv/bin/activate
    python scripts/fix_zero_prices_in_db.py          # 預覽模式（不寫入）
    python scripts/fix_zero_prices_in_db.py --apply   # 實際寫入
"""
import argparse
import sqlite3
from pathlib import Path

from loguru import logger


DB_PATH = Path(__file__).parent.parent / "data" / "zf_trend.db"
PRICE_COLUMNS = ("open_price", "high_price", "low_price", "close_price")


def fix_zero_prices(db_path: Path, apply: bool = False) -> int:
    conn = sqlite3.connect(str(db_path))

    # 找出 stock_info 中的股票有 close_price=0 的記錄
    cursor = conn.execute("""
        SELECT dp.rowid, dp.stock_id, dp.date,
               dp.open_price, dp.high_price, dp.low_price, dp.close_price, dp.volume
        FROM daily_price dp
        JOIN stock_info si ON dp.stock_id = si.stock_id
        WHERE dp.close_price = 0
        ORDER BY dp.stock_id, dp.date
    """)
    zero_rows = cursor.fetchall()

    if not zero_rows:
        logger.info("無需修復：沒有 close_price=0 的資料")
        conn.close()
        return 0

    logger.info(f"找到 {len(zero_rows)} 筆 close_price=0 的資料")

    fixed_count = 0
    skipped_count = 0

    for rowid, stock_id, dt, open_p, high_p, low_p, close_p, volume in zero_rows:
        # 找前一個交易日（同一股票、日期更早、close_price > 0 的最近一筆）
        prev = conn.execute("""
            SELECT open_price, high_price, low_price, close_price
            FROM daily_price
            WHERE stock_id = ? AND date < ? AND close_price > 0
            ORDER BY date DESC
            LIMIT 1
        """, (stock_id, dt)).fetchone()

        if prev is None:
            logger.warning(f"  跳過 {stock_id} {dt}：無前日有效價格")
            skipped_count += 1
            continue

        prev_open, prev_high, prev_low, prev_close = prev

        if apply:
            conn.execute("""
                UPDATE daily_price
                SET open_price = ?, high_price = ?, low_price = ?, close_price = ?
                WHERE rowid = ?
            """, (prev_close, prev_close, prev_close, prev_close, rowid))

        logger.info(
            f"  {'修正' if apply else '預覽'} {stock_id} {dt}: "
            f"0 -> {prev_close} (前日收盤價, volume={volume})"
        )
        fixed_count += 1

    if apply:
        conn.commit()
        logger.info(f"已寫入 {fixed_count} 筆修正, 跳過 {skipped_count} 筆")
    else:
        logger.info(f"預覽模式：可修正 {fixed_count} 筆, 跳過 {skipped_count} 筆")
        logger.info("加上 --apply 參數以實際寫入")

    conn.close()
    return fixed_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="修復 DB 中 close_price=0 的異常資料")
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設為預覽模式）")
    args = parser.parse_args()

    fix_zero_prices(DB_PATH, apply=args.apply)
