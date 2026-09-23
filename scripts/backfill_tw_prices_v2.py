"""
台股歷史股價回補 v2 — 直接用 FinMind 全市場查詢（逐日）

FinMind 全市場查詢（不指定 data_id）有行數限制，約只回傳一天。
所以改成逐日查詢，確保每天都有完整的全市場資料。
"""
import os
import sys
from datetime import date, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from loguru import logger
from data.sqlite_database import SQLiteDatabase
from utils.trading_calendar import TradingCalendar


def fetch_finmind_day(target_date: str, token: str) -> pd.DataFrame:
    """從 FinMind 取得單日全市場股價"""
    resp = requests.get(
        "https://api.finmindtrade.com/api/v4/data",
        params={
            "dataset": "TaiwanStockPrice",
            "start_date": target_date,
            "end_date": target_date,
            "token": token,
        },
        timeout=60,
    )
    data = resp.json()
    if data.get("msg") != "success" or not data.get("data"):
        return pd.DataFrame()

    df = pd.DataFrame(data["data"])
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[["stock_id", "date", "open", "high", "low", "close", "volume"]]


def main():
    token = os.getenv("FINMIND_TOKEN", "")
    if not token:
        logger.error("FINMIND_TOKEN not set")
        return

    db = SQLiteDatabase()

    import sqlite3
    from config.settings import SQLITE_DB_PATH
    conn = sqlite3.connect(SQLITE_DB_PATH)

    # 找出 2024-01 ~ 2024-05 中缺少完整資料的交易日
    target_start = date(2024, 1, 2)
    target_end = date(2024, 5, 15)

    d = target_start
    dates_to_fill = []
    while d <= target_end:
        if TradingCalendar.is_trading_day(d):
            existing = conn.execute(
                "SELECT COUNT(DISTINCT stock_id) FROM daily_price WHERE date = ?",
                (d.isoformat(),),
            ).fetchone()[0]
            if existing < 1500:
                dates_to_fill.append(d)
        d += timedelta(days=1)

    conn.close()
    logger.info(f"需要補齊 {len(dates_to_fill)} 個交易日")

    if not dates_to_fill:
        logger.info("所有交易日已有完整資料")
        return

    total = 0
    for i, td in enumerate(dates_to_fill):
        df = fetch_finmind_day(td.isoformat(), token)
        if df.empty:
            logger.warning(f"  [{i+1}/{len(dates_to_fill)}] {td}: 無資料")
            continue

        # 過濾掉產業指數（stock_id 含英文字母且不是 4 碼數字）
        df = df[df["stock_id"].str.match(r"^\d")]

        count = db.upsert_daily_price(df)
        total += count
        stocks = df["stock_id"].nunique()
        logger.info(f"  [{i+1}/{len(dates_to_fill)}] {td}: {stocks} 檔, {count} 筆")

    # 驗證
    conn = sqlite3.connect(SQLITE_DB_PATH)
    r = conn.execute(
        "SELECT COUNT(*) FROM daily_price WHERE stock_id='2330' AND date <= '2025-02-14'"
    ).fetchone()
    logger.info(f"\n2330 在 2025-02-14 前: {r[0]} 天 (需要 ≥200)")

    for m in ['2024-01', '2024-02', '2024-03', '2024-04', '2024-05']:
        r = conn.execute(
            f"SELECT COUNT(DISTINCT stock_id), COUNT(DISTINCT date) FROM daily_price "
            f"WHERE date >= '{m}-01' AND date < '{m}-32'"
        ).fetchone()
        logger.info(f"  {m}: {r[0]} 檔, {r[1]} 天")
    conn.close()

    logger.info(f"完成，共寫入 {total} 筆")


if __name__ == "__main__":
    main()
