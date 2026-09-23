"""
股價缺漏自動補齊工具

在 daily task 下載股價後、篩選前呼叫，
自動偵測並補齊歷史缺日。
"""

import sqlite3
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger


def fill_price_gaps(
    db_path: str,
    price_table: str,
    ref_stock: str,
    yf_suffix: str = "",
    yf_alt_suffix: str | None = None,
    max_stocks: int = 200,
) -> int:
    """
    偵測並補齊股價缺漏

    Args:
        db_path: SQLite DB 路徑
        price_table: 股價表名 (daily_price / us_daily_price)
        ref_stock: 基準股票代碼 (2330 / AAPL)
        yf_suffix: yfinance ticker 後綴 (.TW / 空字串)
        yf_alt_suffix: yfinance 備選後綴 (.TWO / None)
        max_stocks: 最多補幾檔（避免太久）

    Returns:
        補齊的總筆數
    """
    conn = sqlite3.connect(db_path)

    # 建立基準交易日曆
    ref_dates = [
        r[0]
        for r in conn.execute(
            f"SELECT date FROM {price_table} "
            f"WHERE stock_id = ? ORDER BY date",
            (ref_stock,),
        ).fetchall()
    ]

    if not ref_dates:
        logger.warning(f"基準股票 {ref_stock} 無資料，跳過補漏")
        conn.close()
        return 0

    ref_set = set(ref_dates)
    logger.info(
        f"補漏檢查：基準 {ref_stock} 共 {len(ref_dates)} 天 "
        f"({ref_dates[0]} ~ {ref_dates[-1]})"
    )

    # 只檢查 stock_info 中的正規股票（排除權證等）
    stock_info_table = "stock_info" if "us_" not in price_table else "us_stock_info"
    try:
        all_stocks = [
            r[0]
            for r in conn.execute(
                f"SELECT stock_id FROM {stock_info_table}"
            ).fetchall()
        ]
    except Exception:
        # fallback: 從 price_table 取
        all_stocks = [
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT stock_id FROM {price_table}"
            ).fetchall()
        ]
    logger.info(f"檢查股票數: {len(all_stocks)} 檔")

    # 逐股檢查缺日
    stocks_with_gaps = []
    for stock_id in all_stocks:
        rows = conn.execute(
            f"SELECT date FROM {price_table} "
            f"WHERE stock_id = ? ORDER BY date",
            (stock_id,),
        ).fetchall()
        stock_dates = [r[0] for r in rows]
        if not stock_dates:
            continue

        first = stock_dates[0]
        last = stock_dates[-1]
        expected = {d for d in ref_set if first <= d <= last}
        missing = sorted(expected - set(stock_dates))

        if missing:
            stocks_with_gaps.append((stock_id, missing))

    if not stocks_with_gaps:
        logger.info("補漏檢查完成：無缺漏 ✓")
        conn.close()
        return 0

    logger.info(f"發現 {len(stocks_with_gaps)} 檔有缺漏，開始補齊...")

    # 限制數量
    if len(stocks_with_gaps) > max_stocks:
        # 按缺日數排序，優先補嚴重的
        stocks_with_gaps.sort(key=lambda x: -len(x[1]))
        stocks_with_gaps = stocks_with_gaps[:max_stocks]
        logger.info(f"  限制最多補 {max_stocks} 檔")

    # 台股：查 stock_type 決定 suffix
    stock_types = {}
    if yf_suffix == ".TW":
        try:
            rows = conn.execute(
                "SELECT stock_id, stock_type FROM stock_info"
            ).fetchall()
            stock_types = {r[0]: r[1] for r in rows}
        except Exception:
            pass

    total_filled = 0

    for stock_id, missing_dates in stocks_with_gaps:
        # 台股根據 stock_type 決定 suffix
        actual_suffix = yf_suffix
        actual_alt = yf_alt_suffix
        if stock_types:
            st = stock_types.get(stock_id, "twse")
            if st == "tpex":
                actual_suffix = ".TWO"
                actual_alt = ".TW"
            else:
                actual_suffix = ".TW"
                actual_alt = ".TWO"

        try:
            filled = _download_and_insert(
                conn,
                stock_id,
                missing_dates,
                price_table,
                actual_suffix,
                actual_alt,
            )
            if filled > 0:
                total_filled += filled
                logger.info(f"  {stock_id}: 補齊 {filled} 筆")
            # 避免 rate limit
            time.sleep(0.2)
        except Exception as e:
            logger.warning(f"  {stock_id}: 補齊失敗 - {e}")

    conn.close()
    logger.info(f"補漏完成：共補齊 {total_filled} 筆")
    return total_filled


def _download_and_insert(
    conn: sqlite3.Connection,
    stock_id: str,
    missing_dates: list[str],
    price_table: str,
    yf_suffix: str,
    yf_alt_suffix: str | None,
) -> int:
    """下載缺漏日期的股價並寫入 DB"""
    start = datetime.strptime(missing_dates[0], "%Y-%m-%d").date()
    end = (
        datetime.strptime(missing_dates[-1], "%Y-%m-%d").date()
        + timedelta(days=1)
    )

    ticker_str = f"{stock_id}{yf_suffix}"
    ticker = yf.Ticker(ticker_str)
    hist = ticker.history(start=start, end=end)

    if hist.empty and yf_alt_suffix:
        ticker_str = f"{stock_id}{yf_alt_suffix}"
        ticker = yf.Ticker(ticker_str)
        hist = ticker.history(start=start, end=end)

    if hist.empty:
        return 0

    missing_set = set(missing_dates)
    hist.index = hist.index.tz_localize(None)
    hist["date_str"] = hist.index.strftime("%Y-%m-%d")
    hist = hist[hist["date_str"].isin(missing_set)]

    if hist.empty:
        return 0

    count = 0
    for _, row in hist.iterrows():
        existing = conn.execute(
            f"SELECT id FROM {price_table} "
            f"WHERE stock_id = ? AND date = ?",
            (stock_id, row["date_str"]),
        ).fetchone()

        if existing:
            continue

        conn.execute(
            f"INSERT INTO {price_table} "
            f"(stock_id, date, open_price, high_price, low_price, "
            f"close_price, volume, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stock_id,
                row["date_str"],
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                int(row["Volume"]),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        count += 1

    conn.commit()
    return count
