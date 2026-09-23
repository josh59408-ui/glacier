"""
回填三線開花篩選結果的 20 日漲幅（return_20d）

背景：三線篩選器原本沒計算 return_20d，導致 filter_result 中
filter_type='sanxian' 的 return_20d 全為 NULL。本腳本從 daily_price 計算
return_20d = 收盤.pct_change(20)（20 個交易日前收盤算漲幅，與 VCP 完全一致），
回填這些列，讓前端能替三線股顯示 20 日漲幅。

用法：
    python scripts/backfill_sanxian_return.py         # 台股
    python scripts/backfill_sanxian_return.py --us    # 美股
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent


def _norm_date(series: pd.Series) -> pd.Series:
    """統一日期為 'YYYY-MM-DD' 字串（兼容 '2026-03-24' 與含時間的格式）"""
    return series.astype(str).str.slice(0, 10)


def backfill(db_path: str, filter_table: str, price_table: str) -> int:
    if not Path(db_path).exists():
        print(f"⚠️ DB 不存在: {db_path}")
        return 0

    conn = sqlite3.connect(db_path)

    # 1. 取得所有三線列 (stock_id, filter_date)
    sx = pd.read_sql(
        f"SELECT stock_id, filter_date FROM {filter_table} WHERE filter_type='sanxian'",
        conn,
    )
    if sx.empty:
        print(f"⚠️ {filter_table} 無三線資料")
        conn.close()
        return 0

    sx["date_key"] = _norm_date(sx["filter_date"])
    stock_ids = sx["stock_id"].unique().tolist()
    print(f"三線列 {len(sx)} 筆，涉及 {len(stock_ids)} 檔股票")

    # 2. 載入這些股票的收盤價（分批 IN 查詢避免參數上限）
    frames = []
    CHUNK = 500
    for i in range(0, len(stock_ids), CHUNK):
        chunk = stock_ids[i:i + CHUNK]
        ph = ",".join("?" * len(chunk))
        frames.append(pd.read_sql(
            f"SELECT stock_id, date, close_price FROM {price_table} "
            f"WHERE stock_id IN ({ph})",
            conn, params=chunk,
        ))
    price_df = pd.concat(frames, ignore_index=True)
    price_df["date_key"] = _norm_date(price_df["date"])

    # 修正零價（與 fix_zero_prices 一致：0/NaN 用前一交易日補）
    price_df = price_df.sort_values(["stock_id", "date_key"])
    price_df["close_price"] = price_df["close_price"].replace(0, pd.NA)
    price_df["close_price"] = price_df.groupby("stock_id")["close_price"].ffill()

    # 3. 計算 20 日報酬率 = pct_change(20)（與 VCP calculate_returns 完全相同）
    price_df["return_20d"] = price_df.groupby("stock_id")["close_price"].transform(
        lambda x: x.pct_change(periods=20)
    )

    lookup = {
        (row.stock_id, row.date_key): row.return_20d
        for row in price_df.itertuples(index=False)
        if pd.notna(row.return_20d)
    }

    # 4. 逐筆 UPDATE
    updated = 0
    missing = 0
    cur = conn.cursor()
    for row in sx.itertuples(index=False):
        val = lookup.get((row.stock_id, row.date_key))
        if val is None or pd.isna(val):
            missing += 1
            continue
        cur.execute(
            f"UPDATE {filter_table} SET return_20d=? "
            f"WHERE stock_id=? AND filter_date=? AND filter_type='sanxian'",
            (float(val), row.stock_id, row.filter_date),
        )
        updated += cur.rowcount
    conn.commit()
    conn.close()

    print(f"✅ 回填完成：更新 {updated} 筆三線 return_20d"
          + (f"（{missing} 筆因價格資料不足 20 日略過）" if missing else ""))
    return updated


def main():
    ap = argparse.ArgumentParser(description="回填三線開花的 20 日漲幅")
    ap.add_argument("--us", action="store_true", help="回填美股（預設台股）")
    args = ap.parse_args()

    if args.us:
        backfill(str(BASE_DIR / "data" / "zf_trend_us.db"),
                 "us_filter_result", "us_daily_price")
    else:
        backfill(str(BASE_DIR / "data" / "zf_trend.db"),
                 "filter_result", "daily_price")


if __name__ == "__main__":
    main()
