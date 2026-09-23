"""
補齊個股缺漏股價資料（台股 + 美股）

用基準股票建立交易日曆，找出缺日的股票，從 yfinance 重新下載缺的資料。

用法:
    # 美股
    python scripts/backfill_missing_prices.py                     # 補齊篩選通過的股票
    python scripts/backfill_missing_prices.py --all               # 補齊全部股票
    python scripts/backfill_missing_prices.py --stock INVA        # 補齊指定股票
    python scripts/backfill_missing_prices.py --dry-run           # 只檢查不寫入

    # 台股
    python scripts/backfill_missing_prices.py --tw                # 補齊篩選通過的股票
    python scripts/backfill_missing_prices.py --tw --all          # 補齊全部股票
"""

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from loguru import logger

# 市場設定
MARKET_CONFIG = {
    "us": {
        "label": "美股",
        "db_path": "data/zf_trend_us.db",
        "price_table": "us_daily_price",
        "filter_table": "us_filter_result",
        "ref_stock": "AAPL",
        "suffix": "",
        "alt_suffix": None,
    },
    "tw": {
        "label": "台股",
        "db_path": "data/zf_trend.db",
        "price_table": "daily_price",
        "filter_table": "filter_result",
        "ref_stock": "2330",
        "suffix": ".TW",
        "alt_suffix": ".TWO",
    },
}


def get_reference_calendar(
    conn: sqlite3.Connection, cfg: dict
) -> list[str]:
    """用基準股票建立交易日曆"""
    rows = conn.execute(
        f"SELECT date FROM {cfg['price_table']} "
        f"WHERE stock_id = ? ORDER BY date",
        (cfg["ref_stock"],),
    ).fetchall()
    return [r[0] for r in rows]


def find_missing_dates(
    conn: sqlite3.Connection,
    stock_id: str,
    ref_calendar: list[str],
    cfg: dict,
) -> list[str]:
    """找出單一股票缺少的交易日"""
    rows = conn.execute(
        f"SELECT date FROM {cfg['price_table']} "
        f"WHERE stock_id = ? ORDER BY date",
        (stock_id,),
    ).fetchall()
    stock_dates = [r[0] for r in rows]

    if not stock_dates:
        return []

    first_date = stock_dates[0]
    last_date = stock_dates[-1]

    expected = set(d for d in ref_calendar if first_date <= d <= last_date)
    actual = set(stock_dates)

    return sorted(expected - actual)


def download_missing_prices(
    stock_id: str,
    missing_dates: list[str],
    cfg: dict,
) -> pd.DataFrame:
    """從 yfinance 下載缺漏的股價"""
    if not missing_dates:
        return pd.DataFrame()

    # 找出需要下載的日期範圍（合併連續區間減少 API 呼叫）
    min_date = missing_dates[0]
    max_date = missing_dates[-1]

    # yfinance end_date 是 exclusive，需要 +1 天
    start = datetime.strptime(min_date, "%Y-%m-%d").date()
    end = datetime.strptime(max_date, "%Y-%m-%d").date() + timedelta(days=1)

    suffix = cfg["suffix"]
    ticker_str = f"{stock_id}{suffix}"

    ticker = yf.Ticker(ticker_str)
    hist = ticker.history(start=start, end=end)

    # 台股可能需要嘗試 .TWO
    if hist.empty and cfg["alt_suffix"]:
        ticker_str = f"{stock_id}{cfg['alt_suffix']}"
        ticker = yf.Ticker(ticker_str)
        hist = ticker.history(start=start, end=end)

    if hist.empty:
        return pd.DataFrame()

    # 只取缺的那幾天
    missing_set = set(missing_dates)
    hist.index = hist.index.tz_localize(None)
    hist["date_str"] = hist.index.strftime("%Y-%m-%d")
    hist = hist[hist["date_str"].isin(missing_set)]

    if hist.empty:
        return pd.DataFrame()

    # 轉換為 DB 格式
    records = []
    for _, row in hist.iterrows():
        records.append(
            {
                "stock_id": stock_id,
                "date": row["date_str"],
                "open_price": float(row["Open"]),
                "high_price": float(row["High"]),
                "low_price": float(row["Low"]),
                "close_price": float(row["Close"]),
                "volume": int(row["Volume"]),
            }
        )

    return pd.DataFrame(records)


def upsert_prices(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    cfg: dict,
) -> int:
    """寫入 DB（upsert）"""
    if df.empty:
        return 0

    table = cfg["price_table"]
    count = 0

    for _, row in df.iterrows():
        # 檢查是否已存在
        existing = conn.execute(
            f"SELECT id FROM {table} "
            f"WHERE stock_id = ? AND date = ?",
            (row["stock_id"], row["date"]),
        ).fetchone()

        if existing:
            continue

        conn.execute(
            f"INSERT INTO {table} "
            f"(stock_id, date, open_price, high_price, low_price, "
            f"close_price, volume, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["stock_id"],
                row["date"],
                row["open_price"],
                row["high_price"],
                row["low_price"],
                row["close_price"],
                row["volume"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        count += 1

    conn.commit()
    return count


def main():
    parser = argparse.ArgumentParser(description="補齊缺漏股價資料")
    parser.add_argument("--tw", action="store_true", help="台股（預設美股）")
    parser.add_argument("--date", default=None, help="篩選日期")
    parser.add_argument("--stock", default=None, help="指定股票代碼")
    parser.add_argument("--all", action="store_true", help="全部股票")
    parser.add_argument(
        "--dry-run", action="store_true", help="只檢查不寫入"
    )
    parser.add_argument("--db", default=None, help="自訂 DB 路徑")
    args = parser.parse_args()

    market = "tw" if args.tw else "us"
    cfg = MARKET_CONFIG[market]

    db_path = Path(args.db) if args.db else Path(cfg["db_path"])
    if not db_path.exists():
        logger.error(f"DB 不存在: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))

    # 基準日曆
    ref_calendar = get_reference_calendar(conn, cfg)
    logger.info(
        f"[{cfg['label']}] 基準日曆 ({cfg['ref_stock']}): "
        f"{len(ref_calendar)} 天, {ref_calendar[0]} ~ {ref_calendar[-1]}"
    )

    # 決定要檢查哪些股票
    if args.stock:
        stocks = [args.stock]
    elif args.all:
        rows = conn.execute(
            f"SELECT DISTINCT stock_id FROM {cfg['price_table']}"
        ).fetchall()
        stocks = [r[0] for r in rows]
    else:
        target_date = args.date
        if not target_date:
            row = conn.execute(
                f"SELECT MAX(filter_date) FROM {cfg['filter_table']}"
            ).fetchone()
            target_date = row[0]

        rows = conn.execute(
            f"SELECT DISTINCT stock_id FROM {cfg['filter_table']} "
            "WHERE filter_date = ?",
            (target_date,),
        ).fetchall()
        stocks = [r[0] for r in rows]
        logger.info(f"篩選日期: {target_date}, 共 {len(stocks)} 檔")

    logger.info(f"檢查股票: {len(stocks)} 檔")

    # 台股：查 stock_type 決定 suffix
    stock_types = {}
    if market == "tw":
        try:
            rows = conn.execute(
                "SELECT stock_id, stock_type FROM stock_info"
            ).fetchall()
            stock_types = {r[0]: r[1] for r in rows}
        except Exception:
            pass

    # 逐股檢查並補齊
    total_missing = 0
    total_filled = 0
    failed_stocks = []

    for stock_id in sorted(stocks):
        missing = find_missing_dates(conn, stock_id, ref_calendar, cfg)
        if not missing:
            continue

        total_missing += len(missing)
        missing_str = ", ".join(missing[:5])
        if len(missing) > 5:
            missing_str += f" ... (共 {len(missing)} 天)"

        if args.dry_run:
            logger.info(f"  {stock_id}: 缺 {len(missing)} 天 → {missing_str}")
            continue

        # 台股根據 stock_type 決定 suffix
        stock_cfg = dict(cfg)
        if stock_types:
            st = stock_types.get(stock_id, "twse")
            if st == "tpex":
                stock_cfg["suffix"] = ".TWO"
                stock_cfg["alt_suffix"] = ".TW"

        # 下載並寫入
        logger.info(f"  {stock_id}: 補齊 {len(missing)} 天...")
        try:
            df = download_missing_prices(stock_id, missing, stock_cfg)
            if df.empty:
                logger.warning(f"  {stock_id}: yfinance 無資料")
                failed_stocks.append((stock_id, "yfinance 無資料"))
                continue

            count = upsert_prices(conn, df, cfg)
            total_filled += count
            logger.info(f"  {stock_id}: 補齊 {count} 筆")

            # 避免 yfinance rate limit
            time.sleep(0.3)

        except Exception as e:
            logger.error(f"  {stock_id}: 失敗 - {e}")
            failed_stocks.append((stock_id, str(e)))

    conn.close()

    # 報告
    logger.info("=" * 50)
    if args.dry_run:
        logger.info(
            f"[{cfg['label']}] 共 {total_missing} 筆缺漏（dry-run，未寫入）"
        )
    else:
        logger.info(
            f"[{cfg['label']}] 缺漏: {total_missing} 筆, "
            f"已補齊: {total_filled} 筆"
        )

    if failed_stocks:
        logger.warning(f"失敗: {len(failed_stocks)} 檔")
        for s, reason in failed_stocks:
            logger.warning(f"  {s}: {reason}")


if __name__ == "__main__":
    main()
