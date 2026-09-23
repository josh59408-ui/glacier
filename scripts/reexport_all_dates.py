"""
重新計算並匯出台股 VCP/三線開花結果到 Google Sheet

功能：
  1. backfill：補齊本地 DB 缺少的股價和大盤資料
  2. 讀取 Sheet 上所有日期頁籤
  3. 對每個日期重新計算篩選並覆蓋匯出

使用方式：
    source .venv/bin/activate
    python scripts/reexport_all_dates.py              # 完整流程（backfill + 重跑）
    python scripts/reexport_all_dates.py --skip-fetch  # 跳過 backfill，只重跑匯出
"""
import argparse
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger

from api.hybrid_client import HybridClient
from calculators.moving_average import MovingAverageCalculator
from calculators.vcp_filter import VCPFilter, calculate_market_return
from calculators.sanxian_filter import SanxianFilter
from config.settings import SHEET_IDS
from data.sqlite_database import SQLiteDatabase
from exporters.google_sheet import GoogleSheetExporter
from utils.trading_calendar import TradingCalendar


# Google API 限流保護
DELAY_BETWEEN_EXPORTS = 8  # 每個日期間隔秒數


# ==================== Step 1: Backfill ====================

def backfill_prices(db: SQLiteDatabase, days: int = 90):
    """補齊本地 DB 缺少的股價資料"""
    import sqlite3
    from config.settings import SQLITE_DB_PATH

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cur = conn.execute("SELECT MAX(date) FROM daily_price")
    max_date_str = cur.fetchone()[0]
    conn.close()

    if max_date_str:
        max_date = date.fromisoformat(max_date_str)
        gap_days = (date.today() - max_date).days
        if gap_days <= 1:
            logger.info(f"DB 資料已是最新（{max_date}），跳過 backfill")
            return
        days = min(days, gap_days + 30)  # 多抓 30 天確保完整
        logger.info(f"DB 最新日期: {max_date}，需補齊 {gap_days} 天")
    else:
        logger.info("DB 無資料，補齊最近 90 天")

    client = HybridClient()
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    # 取得市場類型
    market_types = db.get_stock_market_types()
    stock_ids = list(market_types.keys())

    if not stock_ids:
        logger.warning("尚無股票清單，請先執行 'python main.py init'")
        return

    logger.info(f"開始下載 {start_date} ~ {end_date} 的股價（{len(stock_ids)} 檔）...")

    # 取得股價
    price_df = client.get_stock_price(
        start_date, end_date,
        stock_ids=stock_ids,
        market_types=market_types,
    )
    if not price_df.empty:
        # 修正零價後再存入 DB
        zero_mask = price_df["close"] == 0
        zero_count = zero_mask.sum()
        if zero_count > 0:
            logger.warning(f"發現 {zero_count} 筆零價資料，將在計算時 forward-fill 修正")

        count = db.upsert_daily_price(price_df)
        logger.info(f"已補齊 {count} 筆股價資料")

    # 取得大盤指數
    market_df = client.get_market_index(start_date, end_date)
    if not market_df.empty:
        db.upsert_market_index(market_df)
        logger.info(f"已補齊 {len(market_df)} 筆大盤指數")

    # 修復 DB 中的零價
    _fix_db_zero_prices(db)


def _fix_db_zero_prices(db: SQLiteDatabase):
    """修復 DB 中 close_price=0 的資料"""
    import sqlite3
    from config.settings import SQLITE_DB_PATH

    conn = sqlite3.connect(SQLITE_DB_PATH)
    cur = conn.execute("""
        SELECT COUNT(*) FROM daily_price dp
        JOIN stock_info si ON dp.stock_id = si.stock_id
        WHERE dp.close_price = 0
    """)
    zero_count = cur.fetchone()[0]

    if zero_count == 0:
        conn.close()
        return

    logger.info(f"修復 DB 中 {zero_count} 筆零價資料...")

    cur = conn.execute("""
        SELECT dp.rowid, dp.stock_id, dp.date
        FROM daily_price dp
        JOIN stock_info si ON dp.stock_id = si.stock_id
        WHERE dp.close_price = 0
        ORDER BY dp.stock_id, dp.date
    """)

    fixed = 0
    for rowid, stock_id, dt in cur.fetchall():
        prev = conn.execute("""
            SELECT close_price FROM daily_price
            WHERE stock_id = ? AND date < ? AND close_price > 0
            ORDER BY date DESC LIMIT 1
        """, (stock_id, dt)).fetchone()

        if prev:
            conn.execute("""
                UPDATE daily_price
                SET open_price=?, high_price=?, low_price=?, close_price=?
                WHERE rowid=?
            """, (prev[0], prev[0], prev[0], prev[0], rowid))
            fixed += 1

    conn.commit()
    conn.close()
    logger.info(f"已修復 {fixed} 筆零價資料")


# ==================== Step 2: 讀取 Sheet 日期 ====================

def get_sheet_dates(exporter: GoogleSheetExporter) -> list[date]:
    """從 VCP Sheet 讀取所有日期頁籤"""
    sheet_id = SHEET_IDS.get("tw_vcp")
    if not sheet_id:
        logger.error("未設定 tw_vcp Sheet ID")
        return []

    sheet = exporter._get_sheet(sheet_id)
    if not sheet:
        return []

    worksheets = sheet.worksheets()
    dates = []
    for ws in worksheets:
        # 匹配 YYMMDD 格式（不含 _VCP / _三線 等後綴）
        m = re.match(r"^(\d{6})$", ws.title)
        if m:
            try:
                yy = int(m.group(1)[:2])
                mm = int(m.group(1)[2:4])
                dd = int(m.group(1)[4:6])
                year = 2000 + yy  # 民國轉西元: 26 -> 2026? 不對，看起來就是 YY
                # 但台灣用民國？看 260318 = 2026-03-18
                # 不是民國，是西元年後兩位 26=2026
                dates.append(date(year, mm, dd))
            except ValueError:
                continue

    dates.sort()
    logger.info(f"Sheet 上找到 {len(dates)} 個日期頁籤: {[d.isoformat() for d in dates]}")
    return dates


def get_db_filter_dates(db: SQLiteDatabase, since: str = "") -> list[date]:
    """從 DB 的 filter_result 讀取所有日期"""
    import sqlite3
    from config.settings import SQLITE_DB_PATH

    conn = sqlite3.connect(SQLITE_DB_PATH)
    query = "SELECT DISTINCT filter_date FROM filter_result"
    params = ()
    if since:
        query += " WHERE filter_date >= ?"
        params = (since,)
    query += " ORDER BY filter_date"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    dates = []
    for r in rows:
        try:
            dates.append(date.fromisoformat(r[0]))
        except ValueError:
            continue

    logger.info(f"DB 中找到 {len(dates)} 個篩選日期")
    return dates


# ==================== Step 3: 重新計算並匯出 ====================


def _get_recent_stock_ids(
    db: SQLiteDatabase, target_date: date, filter_type: str, lookback: int = 20
) -> set:
    """取得近 lookback 個交易日（不含當天）出現過的篩選結果股票代號聯集

    用於新/舊股票標記（lookback 單位為「交易日」）：
    - 在此集合內 → 近 lookback 交易日曾出現過（灰底，舊股）
    - 不在此集合 → 近 lookback 交易日首次出現（白底，新股）
    """
    # 20 交易日約 28 日曆天，往前抓 2 倍日曆範圍以確保湊滿 lookback 個交易日
    start = target_date - timedelta(days=lookback * 2)
    end = target_date - timedelta(days=1)
    recent_days = TradingCalendar.get_trading_days_in_range(start, end)[-lookback:]

    recent_ids: set = set()
    for d in recent_days:
        try:
            df = db.get_filter_results(filter_type, d)
            if not df.empty:
                recent_ids.update(df["stock_id"].tolist())
        except Exception as e:
            logger.warning(f"取得 {d} {filter_type} 結果失敗: {e}")
    return recent_ids


def export_from_db(
    target_date: date,
    db: SQLiteDatabase,
    exporter: GoogleSheetExporter,
):
    """直接從 DB 讀取篩選結果匯出到 Sheet（不重算）"""
    logger.info(f"=== 匯出 {target_date}（從 DB）===")

    vcp_df = db.get_filter_results("vcp", target_date)
    sanxian_df = db.get_filter_results("sanxian", target_date)

    if vcp_df.empty and sanxian_df.empty:
        logger.warning(f"{target_date}: DB 無篩選結果，跳過")
        return False

    # 轉成 export_vcp / export_sanxian 需要的 dict 格式
    vcp_results = _df_to_export_dicts(vcp_df, "vcp") if not vcp_df.empty else []
    sanxian_results = _df_to_export_dicts(sanxian_df, "sanxian") if not sanxian_df.empty else []

    # 取得近 20 交易日出現過的篩選結果（用於新/舊標記）
    prev_vcp_ids = _get_recent_stock_ids(db, target_date, "vcp")
    prev_sanxian_ids = _get_recent_stock_ids(db, target_date, "sanxian")

    # 匯出到 Google Sheet（帶重試）
    for attempt in range(3):
        try:
            if vcp_results:
                exporter.export_vcp(
                    vcp_results, target_date, prev_stock_ids=prev_vcp_ids
                )
            if sanxian_results:
                exporter.export_sanxian(
                    sanxian_results, target_date, prev_stock_ids=prev_sanxian_ids
                )
            break
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 30 * (attempt + 1)
                logger.warning(f"API 限流，等待 {wait} 秒後重試...")
                time.sleep(wait)
            else:
                logger.error(f"匯出失敗: {e}")
                return False

    logger.info(
        f"{target_date} 完成: VCP {len(vcp_results)} 檔, "
        f"三線開花 {len(sanxian_results)} 檔"
    )
    return True


def _df_to_export_dicts(df: pd.DataFrame, filter_type: str) -> list[dict]:
    """將 DB 的 filter_result DataFrame 轉成 exporter 需要的 dict 格式"""
    results = []
    for _, row in df.iterrows():
        d = {}
        d["stock_id"] = row.get("stock_id", "")
        d["stock_name"] = row.get("stock_name", "")
        d["company_name"] = row.get("stock_name", "")
        d["industry_category"] = row.get("industry_category", "-") or "-"
        d["industry_category2"] = "-"
        d["product_mix"] = "-"

        if filter_type == "vcp":
            val = row.get("return_20d")
            d["return_20d"] = float(val) if val is not None and not pd.isna(val) else None
            d["is_strong"] = bool(row.get("is_strong_list"))
            d["is_new_high"] = bool(row.get("is_new_high_list"))
        else:
            for col in ("today_price", "second_high_55d", "gap_ratio"):
                val = row.get(col)
                d[col] = float(val) if val is not None and not pd.isna(val) else None

        results.append(d)
    return results


def reexport_date(
    target_date: date,
    db: SQLiteDatabase,
    vcp_filter: VCPFilter,
    sanxian_filter: SanxianFilter,
    exporter: GoogleSheetExporter,
):
    """重新計算並匯出單一日期"""
    logger.info(f"=== 重新計算 {target_date} ===")

    start_date = target_date - timedelta(days=365)
    price_df = db.get_daily_prices(start_date, target_date)
    market_df = db.get_market_index(start_date, target_date)

    if price_df.empty:
        logger.warning(f"{target_date}: 無歷史資料，跳過")
        return False

    # 計算大盤報酬率
    market_return = calculate_market_return(market_df, target_date, lookback=20)
    logger.info(f"大盤 20 日報酬率: {market_return:.2%}")

    # 過濾只保留 stock_info 中的股票
    stock_info = db.get_stock_info_dict()
    valid_stock_ids = set(stock_info.keys())
    price_df = price_df[price_df["stock_id"].isin(valid_stock_ids)]

    # VCP 篩選
    vcp_df = vcp_filter.filter(price_df, market_return, target_date)
    vcp_results = _enrich_results(vcp_df, stock_info)

    # 三線開花篩選
    sanxian_df = sanxian_filter.filter(price_df, target_date)
    sanxian_results = _enrich_results(sanxian_df, stock_info)

    # 儲存篩選結果到 DB
    db.save_filter_results(vcp_results, "vcp", target_date)
    db.save_filter_results(sanxian_results, "sanxian", target_date)

    # 取得近 20 交易日出現過的篩選結果（用於新/舊標記）
    prev_vcp_ids = _get_recent_stock_ids(db, target_date, "vcp")
    prev_sanxian_ids = _get_recent_stock_ids(db, target_date, "sanxian")

    # 匯出到 Google Sheet（帶重試）
    for attempt in range(3):
        try:
            if vcp_results:
                exporter.export_vcp(
                    vcp_results, target_date, prev_stock_ids=prev_vcp_ids
                )
            if sanxian_results:
                exporter.export_sanxian(
                    sanxian_results, target_date, prev_stock_ids=prev_sanxian_ids
                )
            break
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait = 30 * (attempt + 1)
                logger.warning(f"API 限流，等待 {wait} 秒後重試...")
                time.sleep(wait)
            else:
                logger.error(f"匯出失敗: {e}")
                return False

    # 匯出驗證資料
    vcp_verification = _prepare_vcp_verification(
        price_df, market_return, target_date, vcp_filter
    )
    sanxian_verification = _prepare_sanxian_verification(price_df, target_date)

    if vcp_verification or sanxian_verification:
        for attempt in range(3):
            try:
                exporter.export_verification(
                    vcp_verification, sanxian_verification,
                    target_date, market_return
                )
                break
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    wait = 30 * (attempt + 1)
                    logger.warning(f"驗證匯出限流，等待 {wait} 秒後重試...")
                    time.sleep(wait)
                else:
                    logger.error(f"驗證匯出失敗: {e}")

    logger.info(
        f"{target_date} 完成: VCP {len(vcp_results)} 檔, "
        f"三線開花 {len(sanxian_results)} 檔"
    )
    return True


def _enrich_results(df, stock_info: dict) -> list[dict]:
    """補充股票基本資料"""
    if df.empty:
        return []

    def _safe_str(val, default="-"):
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return str(val)

    results = []
    for _, row in df.iterrows():
        stock_id = row["stock_id"]
        info = stock_info.get(stock_id, {})
        result = row.to_dict()
        result = {
            k: (v if not (isinstance(v, float) and pd.isna(v)) else None)
            for k, v in result.items()
        }
        result.update({
            "stock_name": _safe_str(info.get("stock_name"), ""),
            "company_name": _safe_str(info.get("stock_name"), ""),
            "industry_category": _safe_str(info.get("industry_category")),
            "industry_category2": _safe_str(info.get("industry_category2")),
            "product_mix": "-",
        })
        results.append(result)
    return results


def _prepare_vcp_verification(price_df, market_return, target_date, vcp_filter):
    """準備 VCP 驗證資料"""
    if price_df.empty:
        return []

    df = MovingAverageCalculator.prepare_vcp_data(price_df)
    if df.empty:
        return []

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["date"] == target_date].copy()
    if df.empty:
        return []

    close = df["close_price"].fillna(0)
    ma50 = df["ma50"].fillna(float("inf"))
    ma150 = df["ma150"].fillna(float("inf"))
    ma200 = df["ma200"].fillna(float("inf"))

    df["cond1"] = close > ma50
    df["cond2"] = ma50 > ma150
    df["cond3"] = ma150 > ma200
    df["cond4"] = df["ma200_slope_20d"].fillna(-1) > 0
    df["cond5"] = df["return_20d"].fillna(-float("inf")) > market_return
    df["is_strong"] = (
        df["cond1"] & df["cond2"] & df["cond3"] & df["cond4"] & df["cond5"]
    )

    high_5d = df["high_5d"].fillna(0)
    high_260d = df["high_260d"].fillna(1).replace(0, 1)
    df["gap_to_52w_high"] = abs(high_5d / high_260d - 1)
    df["is_new_high"] = (
        df["gap_to_52w_high"] <= vcp_filter.new_high_tolerance
    ) & df["cond5"]
    df["is_vcp"] = df["is_strong"] | df["is_new_high"]

    return df.to_dict("records")


def _prepare_sanxian_verification(price_df, target_date):
    """準備三線開花驗證資料"""
    if price_df.empty:
        return []

    df = MovingAverageCalculator.prepare_sanxian_data(price_df)
    if df.empty:
        return []

    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["date"] == target_date].copy()

    return df.to_dict("records") if not df.empty else []


# ==================== Main ====================

def main():
    parser = argparse.ArgumentParser(
        description="補齊資料並重新匯出所有台股 Sheet 日期"
    )
    parser.add_argument(
        "--skip-fetch", action="store_true",
        help="跳過 backfill，只重跑匯出"
    )
    parser.add_argument(
        "--last", type=int, default=0,
        help="只重跑最近 N 個日期（0 = 全部）"
    )
    parser.add_argument(
        "--from-db", action="store_true",
        help="從 DB 的 filter_result 讀取日期（而非 Sheet 頁籤）"
    )
    parser.add_argument(
        "--since", type=str, default="",
        help="搭配 --from-db，只匯出此日期之後的資料（YYYY-MM-DD）"
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="跳過前 N 個日期（搭配 --last 分批用）"
    )
    args = parser.parse_args()

    db = SQLiteDatabase()
    exporter = GoogleSheetExporter()

    if not exporter.health_check():
        logger.error("Google Sheet 未連線，無法匯出")
        return

    # Step 1: Backfill
    if not args.skip_fetch:
        logger.info("=== Step 1: 補齊股價資料 ===")
        backfill_prices(db)
    else:
        logger.info("跳過 backfill")

    # Step 2: 取得日期清單
    if args.from_db:
        logger.info("=== Step 2: 從 DB 讀取篩選結果日期 ===")
        dates = get_db_filter_dates(db, args.since)
    else:
        logger.info("=== Step 2: 讀取 Sheet 日期頁籤 ===")
        dates = get_sheet_dates(exporter)

    if not dates:
        logger.error("沒有找到任何日期")
        return

    # 跳過前 N 個日期（分批用）
    if args.offset > 0:
        dates = dates[args.offset:]
        logger.info(f"跳過前 {args.offset} 個日期")

    # 如果指定 --last N，只取最近 N 個日期
    if args.last > 0:
        dates = dates[-args.last:]

    logger.info(
        f"即將處理 {len(dates)} 個日期: "
        f"{dates[0].isoformat()} ~ {dates[-1].isoformat()}"
    )

    # Step 3: 逐日匯出
    if args.from_db:
        logger.info(f"=== Step 3: 從 DB 直接匯出 {len(dates)} 個日期（不重算）===")
    else:
        logger.info(f"=== Step 3: 重新計算並匯出 {len(dates)} 個日期 ===")
    vcp_filter = VCPFilter()
    sanxian_filter = SanxianFilter()

    success = 0
    failed = 0
    for i, target in enumerate(dates):
        if args.from_db:
            ok = export_from_db(target, db, exporter)
        else:
            ok = reexport_date(target, db, vcp_filter, sanxian_filter, exporter)
        if ok:
            success += 1
        else:
            failed += 1

        if i < len(dates) - 1:
            logger.info(f"等待 {DELAY_BETWEEN_EXPORTS} 秒...")
            time.sleep(DELAY_BETWEEN_EXPORTS)

    logger.info(
        f"=== 全部完成：成功 {success} 個, 失敗 {failed} 個, "
        f"共 {len(dates)} 個日期 ==="
    )


if __name__ == "__main__":
    main()
