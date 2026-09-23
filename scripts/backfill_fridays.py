"""
批次補齊過去兩年每週五的篩選結果（含指標值）

用法：
    python scripts/backfill_fridays.py          # 補齊缺漏的星期五
    python scripts/backfill_fridays.py --force   # 強制重算所有星期五
"""
import json
import math
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger

from calculators.moving_average import MovingAverageCalculator
from config.settings import VCP_PARAMS
from data.sqlite_database import SQLiteDatabase
from utils.trading_calendar import TradingCalendar

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")


def get_all_fridays(start: date, end: date) -> list[date]:
    d = start
    while d.weekday() != 4:
        d += timedelta(days=1)
    fridays = []
    while d <= end:
        fridays.append(d)
        d += timedelta(days=7)
    return fridays


def safe_round(val, digits=2):
    if val is None:
        return None
    try:
        f = float(val)
        if math.isinf(f) or math.isnan(f):
            return None
        return round(f, digits)
    except (ValueError, TypeError):
        return None


def backfill_market_index(db: SQLiteDatabase, start: date, end: date):
    logger.info(f"補齊大盤指數: {start} ~ {end}")
    existing_df = db.get_market_index(start, end)
    existing_dates = set()
    if not existing_df.empty:
        existing_dates = set(pd.to_datetime(existing_df["date"]).dt.date)
    logger.info(f"已有 {len(existing_dates)} 天大盤資料")

    ticker = yf.Ticker("^TWII")
    hist = ticker.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat())
    if hist.empty:
        logger.warning("無法下載大盤指數")
        return

    records = []
    for idx, row in hist.iterrows():
        d = idx.date()
        if d not in existing_dates:
            records.append({"date": d, "taiex": float(row["Close"])})
    if records:
        market_df = pd.DataFrame(records)
        count = db.upsert_market_index(market_df)
        logger.info(f"新增 {count} 天大盤指數")


def _extract_vcp_indicators(row, market_return: float) -> str:
    """從 prepare_vcp_data 的 DataFrame row 提取指標值為 JSON"""
    indicators = {
        "close": safe_round(row.get("close_price")),
        "ma50": safe_round(row.get("ma50")),
        "ma150": safe_round(row.get("ma150")),
        "ma200": safe_round(row.get("ma200")),
        "ma200_slope": safe_round(row.get("ma200_slope_20d")),
        "return_20d": safe_round(row.get("return_20d"), 4),
        "market_return": safe_round(market_return, 4),
        "high_5d": safe_round(row.get("high_5d")),
        "high_260d": safe_round(row.get("high_260d")),
        "high_250d": safe_round(row.get("high_250d")),
    }
    return json.dumps(indicators, ensure_ascii=False)


def _extract_sanxian_indicators(row) -> str:
    """從 prepare_sanxian_data 的 DataFrame row 提取指標值為 JSON"""
    indicators = {
        "close": safe_round(row.get("close_price")),
        "ma8": safe_round(row.get("ma8")),
        "ma21": safe_round(row.get("ma21")),
        "ma55": safe_round(row.get("ma55")),
        "high_55d": safe_round(row.get("high_55d")),
        "second_high": safe_round(row.get("second_high_55d")),
    }
    return json.dumps(indicators, ensure_ascii=False)


def calculate_market_return(market_df, target_date, lookback=20):
    if market_df.empty:
        return 0.0
    df = market_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    target_dt = pd.to_datetime(target_date)
    df = df.sort_values("date").reset_index(drop=True)
    df_before = df[df["date"] <= target_dt]
    if df_before.empty:
        return 0.0
    target_pos = len(df_before) - 1
    if target_pos < lookback:
        lookback = target_pos
    if lookback == 0:
        return 0.0
    current = df.iloc[target_pos]["taiex"]
    past = df.iloc[target_pos - lookback]["taiex"]
    if pd.isna(current) or pd.isna(past) or past == 0:
        return 0.0
    return float((current - past) / past)


def run_filters_for_date(
    db, target_date, stock_info, new_high_tolerance=0.01,
    vcp_data=None, sanxian_data=None, market_df=None, price_raw=None
):
    """對指定日期執行篩選，同時提取指標值。

    若提供已對「全歷史」prepare 過的 vcp_data／sanxian_data／market_df（且 date 欄已轉為
    datetime.date），則直接切片，跳過每日重複載入與 rolling 重算——供 recompute_new_high
    全量重算大幅加速（避免對每個日期重複計算相同的移動平均與高點）。

    price_raw：原始股價（未經 fix_zero_prices）。量大強漲須用原始股價（與每日任務一致），
    不可用 vcp_data（那已被零價修補過）。優化路徑須由呼叫端傳入；否則用本函式載入的 price_df。
    """
    if vcp_data is None or sanxian_data is None or market_df is None:
        start_date = target_date - timedelta(days=400)
        price_df = db.get_daily_prices(start_date, target_date)
        market_df = db.get_market_index(start_date, target_date)

        if price_df.empty:
            return {"date": target_date, "vcp": 0, "sanxian": 0, "skipped": "no_price"}

        valid_ids = set(stock_info.keys())
        price_df = price_df[price_df["stock_id"].isin(valid_ids)]
        if price_df.empty:
            return {"date": target_date, "vcp": 0, "sanxian": 0, "skipped": "no_valid_stock"}

        if price_raw is None:
            price_raw = price_df  # 非優化路徑：用剛載入的原始股價

        price_dates = pd.to_datetime(price_df["date"]).dt.date
        if target_date not in price_dates.values:
            return {"date": target_date, "vcp": 0, "sanxian": 0, "skipped": "no_data_on_date"}

        vcp_data = MovingAverageCalculator.prepare_vcp_data(price_df.copy())
        vcp_data["date"] = pd.to_datetime(vcp_data["date"]).dt.date
        sanxian_data = MovingAverageCalculator.prepare_sanxian_data(price_df.copy())
        sanxian_data["date"] = pd.to_datetime(sanxian_data["date"]).dt.date

    market_return = calculate_market_return(market_df, target_date, lookback=20)

    # === VCP: filter + extract indicators ===
    vcp_today = vcp_data[vcp_data["date"] == target_date].copy()

    vcp_results = []
    if not vcp_today.empty:
        close = vcp_today["close_price"].fillna(0)
        ma50 = vcp_today["ma50"].fillna(float("inf"))
        ma150 = vcp_today["ma150"].fillna(float("inf"))
        ma200 = vcp_today["ma200"].fillna(float("inf"))

        strong_mask = (
            (close > ma50) & (ma50 > ma150) & (ma150 > ma200)
            & (vcp_today["ma200_slope_20d"].fillna(-1) > 0)
        )
        # 打敗大盤（含防呆：排除 20 日報酬異常的分割/合股假訊號，與 VCPFilter 一致）
        ret = vcp_today["return_20d"].fillna(-float("inf"))
        sane_return = (ret > -0.9) & (ret < 5.0)
        beat_market = (ret > market_return) & sane_return

        # 新高：近 5 日最高價 == 近 250 交易日最高價（250 日高點落在最近 5 日內）
        new_high_mask = vcp_today["high_5d"] >= vcp_today["high_250d"]

        # 離 250 日盤中最低點 ≥ 30%（收盤 > low_250d × 1.30，強勢/新高共用；NaN 比較為 False）
        off_low = close > vcp_today["low_250d"] * VCP_PARAMS["low_250d_mult"]

        vcp_today = vcp_today.copy()
        vcp_today.loc[:, "is_strong"] = strong_mask & beat_market & off_low
        vcp_today.loc[:, "is_new_high"] = new_high_mask & beat_market & off_low

        vcp_filtered = vcp_today[vcp_today["is_strong"] | vcp_today["is_new_high"]]

        for _, row in vcp_filtered.iterrows():
            sid = row["stock_id"]
            info = stock_info.get(sid, {})
            result = {
                "stock_id": sid,
                "stock_name": info.get("stock_name", ""),
                "industry_category": info.get("industry_category", "-") or "-",
                "return_20d": row.get("return_20d"),
                "is_strong": bool(row["is_strong"]),
                "is_new_high": bool(row["is_new_high"]),
                "indicator_json": _extract_vcp_indicators(row, market_return),
            }
            vcp_results.append(result)

    # === Sanxian: filter + extract indicators ===
    sanxian_today = sanxian_data[sanxian_data["date"] == target_date].copy()

    sanxian_results = []
    if not sanxian_today.empty:
        close = sanxian_today["close_price"].fillna(0)
        ma8 = sanxian_today["ma8"].fillna(float("inf"))
        ma21 = sanxian_today["ma21"].fillna(float("inf"))
        ma55 = sanxian_today["ma55"].fillna(float("inf"))

        cond_arrange = (close > ma8) & (ma8 > ma21) & (ma21 > ma55)
        high_55d = sanxian_today["high_55d"].fillna(float("inf"))
        cond_high = close >= high_55d

        sanxian_filtered = sanxian_today[cond_arrange & cond_high].copy()

        if not sanxian_filtered.empty:
            second_high = sanxian_filtered["second_high_55d"].fillna(1).replace(0, 1)
            sanxian_filtered.loc[:, "gap_ratio"] = (sanxian_filtered["close_price"] / second_high - 1)
            sanxian_filtered.loc[:, "today_price"] = sanxian_filtered["close_price"]

            for _, row in sanxian_filtered.iterrows():
                sid = row["stock_id"]
                info = stock_info.get(sid, {})
                result = {
                    "stock_id": sid,
                    "stock_name": info.get("stock_name", ""),
                    "industry_category": info.get("industry_category", "-") or "-",
                    "today_price": row.get("today_price"),
                    "second_high_55d": row.get("second_high_55d"),
                    "gap_ratio": row.get("gap_ratio"),
                    "indicator_json": _extract_sanxian_indicators(row),
                }
                sanxian_results.append(result)

    # === Volume Surge (量大強漲)：與每日任務同一路徑重算 ===
    # 用「原始股價」（未 fix_zero_prices），與每日任務一致；取近窗口切片（lookback=20，150 日足夠）
    from calculators.volume_surge_filter import VolumeSurgeFilter

    pr = price_raw
    if pr is not None and not pr.empty and not isinstance(pr["date"].iloc[0], date):
        pr = pr.copy()
        pr["date"] = pd.to_datetime(pr["date"]).dt.date
    vol_src = pr[
        (pr["date"] > target_date - timedelta(days=150))
        & (pr["date"] <= target_date)
    ] if pr is not None else pd.DataFrame()
    vol_df = VolumeSurgeFilter().filter(vol_src, target_date)
    volume_surge_results = []
    for _, row in vol_df.iterrows():
        sid = row["stock_id"]
        info = stock_info.get(sid, {})
        r = {k: (None if isinstance(v, float) and pd.isna(v) else v)
             for k, v in row.to_dict().items()}
        r["stock_name"] = info.get("stock_name", "") or ""
        r["industry_category"] = info.get("industry_category", "-") or "-"
        volume_surge_results.append(r)

    # Save to DB
    if vcp_results:
        db.save_filter_results(vcp_results, "vcp", target_date)
    if sanxian_results:
        db.save_filter_results(sanxian_results, "sanxian", target_date)
    if volume_surge_results:
        db.save_filter_results(volume_surge_results, "volume_surge", target_date)

    return {
        "date": target_date,
        "vcp": len(vcp_results),
        "sanxian": len(sanxian_results),
        "volume_surge": len(volume_surge_results),
        "skipped": None,
    }


def main():
    force = "--force" in sys.argv

    end_date = date(2026, 3, 21)
    start_date = date(2024, 3, 22)

    fridays = get_all_fridays(start_date, end_date)
    logger.info(f"共 {len(fridays)} 個星期五 ({start_date} ~ {end_date})")

    db = SQLiteDatabase()
    db.create_tables()

    # 加入 indicator_json 欄位（若不存在）
    try:
        from sqlalchemy import text as sa_text
        with db.get_session() as session:
            session.execute(sa_text("ALTER TABLE filter_result ADD COLUMN indicator_json TEXT"))
        logger.info("已新增 indicator_json 欄位")
    except Exception:
        pass  # 欄位已存在

    # 補齊大盤指數
    backfill_market_index(db, start_date - timedelta(days=30), end_date)

    # 檢查哪些星期五需要處理
    import sqlite3
    conn = sqlite3.connect(db.db_path)
    cursor = conn.cursor()

    trading_fridays = [f for f in fridays if TradingCalendar.is_trading_day(f)]

    if force:
        # 強制模式：刪除所有舊結果
        cursor.execute("DELETE FROM filter_result")
        conn.commit()
        logger.info("已清除所有舊篩選結果（--force 模式）")
        need_process = trading_fridays
    else:
        cursor.execute("SELECT DISTINCT filter_date FROM filter_result")
        existing_dates = {row[0] for row in cursor.fetchall()}
        need_process = [f for f in trading_fridays if f.isoformat() not in existing_dates]

    conn.close()

    logger.info(
        f"交易日星期五: {len(trading_fridays)} 天, "
        f"需處理: {len(need_process)} 天"
    )

    if not need_process:
        logger.info("無需處理")
        return

    stock_info = db.get_stock_info_dict()
    logger.info(f"股票主檔: {len(stock_info)} 檔")

    tolerance = VCP_PARAMS.get("new_high_tolerance", 0.10)
    total_vcp = 0
    total_sanxian = 0
    skipped = 0

    for i, friday in enumerate(need_process, 1):
        logger.info(f"[{i}/{len(need_process)}] 處理 {friday}...")
        result = run_filters_for_date(db, friday, stock_info, tolerance)
        if result["skipped"]:
            logger.warning(f"  跳過: {result['skipped']}")
            skipped += 1
        else:
            logger.info(f"  VCP: {result['vcp']} 檔, 三線開花: {result['sanxian']} 檔")
            total_vcp += result["vcp"]
            total_sanxian += result["sanxian"]

    logger.info("=" * 50)
    logger.info(f"完成！處理 {len(need_process)} 天, 跳過 {skipped} 天")
    logger.info(f"VCP: {total_vcp} 筆, 三線開花: {total_sanxian} 筆")

    logger.info("匯出 JSON...")
    from scripts.export_to_json import main as export_json
    export_json()
    logger.info("全部完成！")


if __name__ == "__main__":
    main()
