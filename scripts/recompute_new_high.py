"""
全部重算篩選結果（套用新的新高邏輯：收盤價 > 前 250 交易日「不含當天」最高價）

針對 filter_result（或 us_filter_result）中「已存在的所有交易日」重新執行 VCP／三線開花
篩選，並重建 indicator_json（tooltip 資料，含 high_5d / high_250d）。

效能：一次載入全歷史股價，只 prepare（移動平均、rolling 高點）一次，之後每個日期直接
切片套用篩選條件，避免對每個交易日重複載入與重算相同 rolling 指標。

因新高邏輯比舊版嚴格，部分日期結果可能歸零；save_filter_results 在結果為空時不會刪除舊
列，故每個日期「先刪除舊結果再重算」，避免殘留過期資料。

用法：
    source .venv/bin/activate
    python scripts/recompute_new_high.py         # 台股
    python scripts/recompute_new_high.py --us    # 美股
"""
import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from loguru import logger

# 全歷史載入的日期邊界（涵蓋所有可能的篩選日與其 rolling 回看範圍）
_HISTORY_START = date(2023, 1, 1)
_HISTORY_END = date(2035, 1, 1)


def _distinct_dates(db_path: str, table: str) -> list[str]:
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT filter_date FROM {table} ORDER BY filter_date"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _delete_date(db_path: str, table: str, date_str: str) -> None:
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        conn.execute(f"DELETE FROM {table} WHERE filter_date = ?", (date_str,))
        conn.commit()
    finally:
        conn.close()


def _prepare_full(prepare_vcp, prepare_sanxian, price_df):
    """對全歷史股價各 prepare 一次，回傳 (vcp_data, sanxian_data)，date 欄轉為 date。"""
    vcp_data = prepare_vcp(price_df.copy())
    vcp_data["date"] = pd.to_datetime(vcp_data["date"]).dt.date
    sanxian_data = prepare_sanxian(price_df.copy())
    sanxian_data["date"] = pd.to_datetime(sanxian_data["date"]).dt.date
    return vcp_data, sanxian_data


def recompute_tw() -> None:
    from calculators.moving_average import MovingAverageCalculator
    from data.sqlite_database import SQLiteDatabase
    from scripts.backfill_fridays import run_filters_for_date

    db = SQLiteDatabase()
    stock_info = db.get_stock_info_dict()
    dates = _distinct_dates(db.db_path, "filter_result")
    if not dates:
        logger.info("台股：filter_result 無資料，無需重算")
        return

    logger.info(f"台股：載入全歷史股價並 prepare（一次）...")
    price_df = db.get_daily_prices(_HISTORY_START, _HISTORY_END)
    price_df = price_df[price_df["stock_id"].isin(set(stock_info.keys()))]
    market_df = db.get_market_index(_HISTORY_START, _HISTORY_END)
    vcp_data, sanxian_data = _prepare_full(
        MovingAverageCalculator.prepare_vcp_data,
        MovingAverageCalculator.prepare_sanxian_data,
        price_df,
    )
    # 量大強漲用「原始股價」（未 fix_zero_prices）——不可用 vcp_data；date 先轉一次
    price_raw = price_df.copy()
    price_raw["date"] = pd.to_datetime(price_raw["date"]).dt.date

    logger.info(f"台股：重算 {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")
    for i, ds in enumerate(dates):
        td = date.fromisoformat(ds)
        _delete_date(db.db_path, "filter_result", ds)
        res = run_filters_for_date(
            db, td, stock_info,
            vcp_data=vcp_data, sanxian_data=sanxian_data, market_df=market_df,
            price_raw=price_raw,
        )
        if (i + 1) % 30 == 0 or i == len(dates) - 1:
            logger.info(
                f"[{i + 1}/{len(dates)}] {ds}: VCP {res['vcp']}, 三線 {res['sanxian']}"
            )
    logger.info("=== 台股重算完成 ===")


def recompute_us() -> None:
    from calculators.us_moving_average import USMovingAverageCalculator
    from data.us_database import USSQLiteDatabase
    from scripts.backfill_fridays_us import run_us_filters_for_date

    db = USSQLiteDatabase()
    stock_info = db.get_stock_info_dict()
    dates = _distinct_dates(db.db_path, "us_filter_result")
    if not dates:
        logger.info("美股：us_filter_result 無資料，無需重算")
        return

    logger.info(f"美股：載入全歷史股價並 prepare（一次）...")
    price_df = db.get_daily_prices(_HISTORY_START, _HISTORY_END)
    price_df = price_df[price_df["stock_id"].isin(set(stock_info.keys()))]
    market_df = db.get_market_index(_HISTORY_START, _HISTORY_END)
    vcp_data, sanxian_data = _prepare_full(
        USMovingAverageCalculator.prepare_vcp_data,
        USMovingAverageCalculator.prepare_sanxian_data,
        price_df,
    )
    # 量大強漲用「原始股價」（未 fix_zero_prices）——不可用 vcp_data；date 先轉一次
    price_raw = price_df.copy()
    price_raw["date"] = pd.to_datetime(price_raw["date"]).dt.date

    logger.info(f"美股：重算 {len(dates)} 天 ({dates[0]} ~ {dates[-1]})")
    for i, ds in enumerate(dates):
        td = date.fromisoformat(ds)
        _delete_date(db.db_path, "us_filter_result", ds)
        res = run_us_filters_for_date(
            db, td, stock_info,
            vcp_data=vcp_data, sanxian_data=sanxian_data, market_df=market_df,
            price_raw=price_raw,
        )
        if (i + 1) % 30 == 0 or i == len(dates) - 1:
            logger.info(
                f"[{i + 1}/{len(dates)}] {ds}: VCP {res['vcp']}, 三線 {res['sanxian']}"
            )
    logger.info("=== 美股重算完成 ===")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="全部重算篩選結果（新高＝突破前 250 交易日最高價）"
    )
    parser.add_argument("--us", action="store_true", help="重算美股（預設台股）")
    args = parser.parse_args()

    if args.us:
        recompute_us()
    else:
        recompute_tw()


if __name__ == "__main__":
    main()
