"""
補齊所有交易日的篩選結果（只存 DB，不匯出 Google Sheet）

用法：
    source .venv/bin/activate
    python scripts/backfill_all_trading_days.py                # 從 2025-07-01 開始補齊
    python scripts/backfill_all_trading_days.py --since 2026-01-01  # 從指定日期開始
"""
import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from loguru import logger

from calculators.vcp_filter import VCPFilter, calculate_market_return
from calculators.sanxian_filter import SanxianFilter
from config.settings import SQLITE_DB_PATH
from data.sqlite_database import SQLiteDatabase


def get_missing_dates(db: SQLiteDatabase, since: str) -> list[str]:
    """找出有股價但缺少篩選結果的交易日"""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    existing = set(
        r[0] for r in conn.execute("SELECT DISTINCT filter_date FROM filter_result")
    )
    price_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT date FROM daily_price WHERE date >= ? ORDER BY date",
            (since,),
        )
    ]
    conn.close()
    return [d for d in price_dates if d not in existing]


def backfill_date(
    target_date: date,
    db: SQLiteDatabase,
    vcp_filter: VCPFilter,
    sanxian_filter: SanxianFilter,
    stock_info: dict,
):
    """計算並儲存單一日期的篩選結果"""
    start_date = target_date - timedelta(days=365)
    price_df = db.get_daily_prices(start_date, target_date)
    market_df = db.get_market_index(start_date, target_date)

    if price_df.empty:
        return 0, 0

    market_return = calculate_market_return(market_df, target_date, lookback=20)

    valid_stock_ids = set(stock_info.keys())
    price_df = price_df[price_df["stock_id"].isin(valid_stock_ids)]

    # VCP
    vcp_df = vcp_filter.filter(price_df, market_return, target_date)
    vcp_results = _enrich(vcp_df, stock_info)

    # 三線開花
    sanxian_df = sanxian_filter.filter(price_df, target_date)
    sanxian_results = _enrich(sanxian_df, stock_info)

    # 儲存
    db.save_filter_results(vcp_results, "vcp", target_date)
    db.save_filter_results(sanxian_results, "sanxian", target_date)

    return len(vcp_results), len(sanxian_results)


def _enrich(df, stock_info: dict) -> list[dict]:
    """補充股票基本資料"""
    if df.empty:
        return []
    results = []
    for _, row in df.iterrows():
        sid = row["stock_id"]
        info = stock_info.get(sid, {})
        result = {
            k: (v if not (isinstance(v, float) and pd.isna(v)) else None)
            for k, v in row.to_dict().items()
        }
        result.update(
            {
                "stock_name": info.get("stock_name", ""),
                "company_name": info.get("stock_name", ""),
                "industry_category": info.get("industry_category", "-"),
                "industry_category2": info.get("industry_category2", "-"),
                "product_mix": "-",
            }
        )
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description="補齊所有交易日的篩選結果")
    parser.add_argument(
        "--since",
        default="2025-07-01",
        help="從哪一天開始補齊 (預設 2025-07-01)",
    )
    args = parser.parse_args()

    db = SQLiteDatabase()
    missing = get_missing_dates(db, args.since)

    if not missing:
        logger.info("沒有需要補齊的日期")
        return

    logger.info(f"需要補齊 {len(missing)} 天: {missing[0]} ~ {missing[-1]}")

    vcp_filter = VCPFilter()
    sanxian_filter = SanxianFilter()
    stock_info = db.get_stock_info_dict()

    success = 0
    for i, date_str in enumerate(missing):
        td = date.fromisoformat(date_str)
        vcp_n, san_n = backfill_date(td, db, vcp_filter, sanxian_filter, stock_info)
        success += 1
        if (i + 1) % 10 == 0 or i == len(missing) - 1:
            logger.info(
                f"[{i + 1}/{len(missing)}] {date_str}: "
                f"VCP {vcp_n}, 三線 {san_n}"
            )

    logger.info(f"=== 補齊完成: {success}/{len(missing)} 天 ===")


if __name__ == "__main__":
    main()
