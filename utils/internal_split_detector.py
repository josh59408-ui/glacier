"""
DB 內部價格跳動偵測（第三層分割偵測）

不依賴 yfinance fresh 資料，直接掃描 DB 中股票的相鄰兩筆收盤價，
若跳動超過閾值（>1.5x 或 <0.67x），表示 DB 可能混合了分割前後的價格。

處理流程：
  1. 掃描所有股票最近 N 天，找出相鄰價格跳動異常
  2. 過濾掉白名單中已驗證為真實波動的股票/日期
  3. DELETE 該股票全部歷史 + 重新下載 365 天
  4. 再次掃描，若跳動仍存在 → 視為真實波動 → 加入白名單
"""

import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger


# 偵測門檻：價格比值
JUMP_UP_THRESHOLD = 1.5    # 上漲 50% 視為異常
JUMP_DOWN_THRESHOLD = 0.67  # 下跌 33% 視為異常

# 最低價門檻：< $1 的 penny stock 不偵測（買賣價差大，每日波動 30%+ 正常）
MIN_PRICE_FOR_DETECT = 1.0

# 一次最多處理檔數（避免 rate limit + 控制執行時間）
MAX_PROCESS_PER_RUN = 20

# 重新下載歷史天數
HISTORY_DAYS = 365

# 每檔下載間隔（避免 rate limit）
DOWNLOAD_INTERVAL_SEC = 2


def detect_and_fix_internal_splits(
    db_path: str,
    price_table: str = "us_daily_price",
    whitelist_table: str = "us_anomaly_whitelist",
    scan_days: int = 30,
) -> dict:
    """
    執行內部分割偵測

    Args:
        db_path: SQLite DB 路徑
        price_table: 股價表名
        whitelist_table: 白名單表名
        scan_days: 掃描最近幾天的價格（預設 30 天）

    Returns:
        {
          "scanned": 掃描股票數,
          "anomalies": 異常股票數,
          "skipped_whitelist": 白名單跳過數,
          "fixed": 重新下載數,
          "added_to_whitelist": 加入白名單數,
        }
    """
    logger.info(f"=== 內部分割偵測：掃描最近 {scan_days} 天 ===")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 確保白名單表存在
    _ensure_whitelist_table(conn, whitelist_table)

    # Step 1: 掃描異常
    anomalies = _scan_anomalies(conn, price_table, scan_days)
    logger.info(f"  掃描完成，發現 {len(anomalies)} 檔異常")

    if not anomalies:
        conn.close()
        return {
            "scanned": 1,
            "anomalies": 0,
            "skipped_whitelist": 0,
            "fixed": 0,
            "added_to_whitelist": 0,
        }

    # Step 2: 過濾白名單
    whitelist = _load_whitelist(conn, whitelist_table)
    to_process = []
    skipped = 0
    for a in anomalies:
        key = (a["stock_id"], a["anomaly_date"])
        if key in whitelist:
            skipped += 1
            continue
        to_process.append(a)

    logger.info(f"  白名單跳過 {skipped} 檔，待處理 {len(to_process)} 檔")

    # 限制處理數量
    if len(to_process) > MAX_PROCESS_PER_RUN:
        to_process.sort(key=lambda x: -abs(x["ratio"] - 1))  # 優先處理跳動最大的
        to_process = to_process[:MAX_PROCESS_PER_RUN]
        logger.info(f"  限制單次最多處理 {MAX_PROCESS_PER_RUN} 檔")

    # Step 3: 重新下載 + 驗證
    fixed = 0
    added_to_whitelist = 0
    for a in to_process:
        try:
            result = _refix_stock(conn, a, price_table, whitelist_table)
            if result == "fixed":
                fixed += 1
            elif result == "whitelisted":
                added_to_whitelist += 1
            time.sleep(DOWNLOAD_INTERVAL_SEC)
        except Exception as e:
            logger.warning(f"  {a['stock_id']}: 處理失敗 - {e}")

    conn.commit()
    conn.close()

    logger.info(
        f"=== 內部分割偵測完成：修復 {fixed} 檔，"
        f"新增白名單 {added_to_whitelist} 檔 ==="
    )

    return {
        "scanned": 1,
        "anomalies": len(anomalies),
        "skipped_whitelist": skipped,
        "fixed": fixed,
        "added_to_whitelist": added_to_whitelist,
    }


def _ensure_whitelist_table(conn: sqlite3.Connection, table: str) -> None:
    """確保白名單表存在（避免 daily task 還沒 create_tables 就跑）"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id VARCHAR(20) NOT NULL,
            anomaly_date DATE NOT NULL,
            prev_close NUMERIC(12,4),
            today_close NUMERIC(12,4),
            ratio NUMERIC(10,4),
            reason TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(stock_id, anomaly_date)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_stock ON {table}(stock_id)"
    )
    conn.commit()


def _scan_anomalies(
    conn: sqlite3.Connection, price_table: str, scan_days: int
) -> list[dict]:
    """掃描相鄰兩筆價格跳動異常"""
    cutoff = (date.today() - timedelta(days=scan_days)).isoformat()

    # 用 LAG 找出相鄰兩筆，計算 ratio
    rows = conn.execute(
        f"""
        WITH price_pairs AS (
            SELECT
                stock_id,
                date AS curr_date,
                close_price AS curr_close,
                LAG(date) OVER (PARTITION BY stock_id ORDER BY date) AS prev_date,
                LAG(close_price) OVER (PARTITION BY stock_id ORDER BY date) AS prev_close
            FROM {price_table}
            WHERE date >= ?
        )
        SELECT stock_id, curr_date, curr_close, prev_date, prev_close,
               (curr_close * 1.0 / prev_close) AS ratio
        FROM price_pairs
        WHERE prev_close IS NOT NULL
          AND prev_close >= ?
          AND curr_close >= ?
          AND ((curr_close * 1.0 / prev_close) > ?
            OR (curr_close * 1.0 / prev_close) < ?)
        ORDER BY ABS((curr_close * 1.0 / prev_close) - 1) DESC
        """,
        (cutoff, MIN_PRICE_FOR_DETECT, MIN_PRICE_FOR_DETECT, JUMP_UP_THRESHOLD, JUMP_DOWN_THRESHOLD),
    ).fetchall()

    return [
        {
            "stock_id": r["stock_id"],
            "anomaly_date": r["curr_date"],
            "prev_date": r["prev_date"],
            "prev_close": float(r["prev_close"]),
            "today_close": float(r["curr_close"]),
            "ratio": float(r["ratio"]),
        }
        for r in rows
    ]


def _load_whitelist(conn: sqlite3.Connection, table: str) -> set:
    """載入白名單為 (stock_id, anomaly_date) 集合"""
    rows = conn.execute(
        f"SELECT stock_id, anomaly_date FROM {table}"
    ).fetchall()
    return {(r["stock_id"], r["anomaly_date"]) for r in rows}


def _refix_stock(
    conn: sqlite3.Connection,
    anomaly: dict,
    price_table: str,
    whitelist_table: str,
) -> str:
    """
    重新下載單一股票歷史，若仍有跳動則加入白名單

    Returns:
        "fixed" — 修復後跳動消失
        "whitelisted" — 重新下載後仍有跳動，加入白名單
        "failed" — yfinance 下載失敗
    """
    stock_id = anomaly["stock_id"]
    logger.warning(
        f"  {stock_id}: 偵測到跳動 "
        f"{anomaly['prev_date']}={anomaly['prev_close']:.4f} → "
        f"{anomaly['anomaly_date']}={anomaly['today_close']:.4f} "
        f"(ratio={anomaly['ratio']:.2f})"
    )

    # 下載 365 天歷史
    end_date = date.today()
    start_date = end_date - timedelta(days=HISTORY_DAYS)

    try:
        ticker = yf.Ticker(stock_id)
        hist = ticker.history(start=start_date, end=end_date, auto_adjust=False)
    except Exception as e:
        logger.warning(f"    yfinance 下載失敗: {e}")
        return "failed"

    if hist.empty:
        logger.warning(f"    yfinance 無資料")
        return "failed"

    # 處理時區
    hist.index = hist.index.tz_localize(None)

    # 檢查新下載的資料中是否還有同樣的跳動
    new_close = hist["Close"]
    new_close_sorted = new_close.sort_index()
    ratios = new_close_sorted / new_close_sorted.shift(1)
    has_jump_in_new = (
        (ratios > JUMP_UP_THRESHOLD) | (ratios < JUMP_DOWN_THRESHOLD)
    ).any()

    if has_jump_in_new:
        # 新下載的資料中本來就有跳動 → 真實波動，加白名單
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {whitelist_table}
            (stock_id, anomaly_date, prev_close, today_close, ratio, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stock_id,
                anomaly["anomaly_date"],
                anomaly["prev_close"],
                anomaly["today_close"],
                anomaly["ratio"],
                "重新下載後仍有跳動，視為真實波動",
            ),
        )
        logger.info(f"    → 真實波動，加入白名單")
        return "whitelisted"

    # 新下載的資料中無跳動 → DB 確實有問題 → 刪除舊資料並寫入新資料
    conn.execute(
        f"DELETE FROM {price_table} WHERE stock_id = ?",
        (stock_id,),
    )

    # 寫入新資料
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = 0
    for idx, row in hist.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        try:
            conn.execute(
                f"""
                INSERT INTO {price_table}
                (stock_id, date, open_price, high_price, low_price,
                 close_price, volume, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stock_id,
                    date_str,
                    float(row["Open"]),
                    float(row["High"]),
                    float(row["Low"]),
                    float(row["Close"]),
                    int(row["Volume"]),
                    now_str,
                ),
            )
            inserted += 1
        except Exception:
            continue

    logger.info(f"    → DB 修復完成，重寫 {inserted} 筆")
    return "fixed"
