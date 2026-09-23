"""
驗證個股資料完整性（台股 + 美股）

用基準股票建立交易日曆，逐股比對缺了哪些交易日，
並判斷缺日是否影響 MA / high 計算窗口。

用法:
    # 美股（預設）
    python scripts/verify_stock_gaps.py                    # 驗證篩選通過的股票
    python scripts/verify_stock_gaps.py --date 2026-04-02  # 指定日期
    python scripts/verify_stock_gaps.py --stock INVA       # 指定股票
    python scripts/verify_stock_gaps.py --all              # 驗證全部股票

    # 台股
    python scripts/verify_stock_gaps.py --tw               # 驗證篩選通過的股票
    python scripts/verify_stock_gaps.py --tw --stock 2330  # 指定股票
    python scripts/verify_stock_gaps.py --tw --all         # 驗證全部股票
"""

import argparse
import sqlite3
import subprocess
import sys
from pathlib import Path

# 計算窗口定義（影響判定用）
WINDOWS_VCP = {
    "MA50": 50,
    "MA150": 150,
    "MA200": 200,
    "high_5d": 5,
    "high_260d": 260,
}

WINDOWS_SANXIAN = {
    "MA8": 8,
    "MA21": 21,
    "MA55": 55,
    "high_55d": 55,
}

ALL_WINDOWS = {**WINDOWS_VCP, **WINDOWS_SANXIAN}

# 市場設定
MARKET_CONFIG = {
    "us": {
        "label": "美股",
        "db_remote": "zf_trend_us.db.gz",
        "db_local": "/tmp/zf_trend_us.db",
        "db_fallback": "data/zf_trend_us.db",
        "release_tag": "us-db-backup",
        "price_table": "us_daily_price",
        "filter_table": "us_filter_result",
        "ref_stock": "AAPL",
    },
    "tw": {
        "label": "台股",
        "db_remote": "zf_trend_full.db.gz",
        "db_local": "/tmp/zf_trend_full.db",
        "db_fallback": "data/zf_trend.db",
        "release_tag": "db-backup",
        "price_table": "daily_price",
        "filter_table": "filter_result",
        "ref_stock": "2330",
    },
}


def download_db(market: str) -> Path:
    """從 GitHub Release 下載最新 DB"""
    cfg = MARKET_CONFIG[market]
    db_path = Path(cfg["db_local"])

    if db_path.exists():
        return db_path

    print(f"下載 {cfg['label']} DB...")
    result = subprocess.run(
        [
            "gh", "release", "download", cfg["release_tag"],
            "-p", cfg["db_remote"],
            "-D", "/tmp", "--clobber",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # 嘗試本地
        fallback = Path(cfg["db_fallback"])
        if fallback.exists():
            print(f"  線上下載失敗，使用本地 DB: {fallback}")
            return fallback
        print(f"❌ 下載失敗: {result.stderr}")
        sys.exit(1)

    subprocess.run(["gunzip", "-f", f"/tmp/{cfg['db_remote']}"])
    print(f"  已下載: {db_path}")
    return db_path


def get_reference_calendar(
    conn: sqlite3.Connection, market: str
) -> list[str]:
    """用基準股票建立交易日曆"""
    cfg = MARKET_CONFIG[market]
    ref_stock = cfg["ref_stock"]
    table = cfg["price_table"]

    rows = conn.execute(
        f"SELECT date FROM {table} WHERE stock_id = ? ORDER BY date",
        (ref_stock,),
    ).fetchall()
    return [r[0] for r in rows]


def check_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    ref_calendar: list[str],
    price_table: str,
) -> dict:
    """檢查單一股票的資料完整性"""
    rows = conn.execute(
        f"SELECT date FROM {price_table} WHERE stock_id = ? ORDER BY date",
        (stock_id,),
    ).fetchall()
    stock_dates = [r[0] for r in rows]

    if not stock_dates:
        return {"stock_id": stock_id, "total": 0, "status": "no_data"}

    first_date = stock_dates[0]
    last_date = stock_dates[-1]

    # 基準日曆中，該股票應有的交易日
    expected = [d for d in ref_calendar if first_date <= d <= last_date]
    actual = set(stock_dates)

    # 缺少的交易日
    missing = sorted(set(expected) - actual)

    # 判斷缺日是否影響計算窗口
    affected_windows = []
    if missing and len(stock_dates) > 0:
        latest = stock_dates[-1]
        latest_idx_in_ref = (
            ref_calendar.index(latest) if latest in ref_calendar else None
        )

        if latest_idx_in_ref is not None:
            for window_name, window_size in ALL_WINDOWS.items():
                if len(stock_dates) < window_size:
                    continue

                db_window_start = stock_dates[-window_size]

                correct_start_idx = latest_idx_in_ref - window_size + 1
                if correct_start_idx < 0:
                    continue
                correct_window_start = ref_calendar[correct_start_idx]

                missing_in_window = [
                    d
                    for d in missing
                    if correct_window_start <= d <= latest
                ]

                if missing_in_window:
                    affected_windows.append(
                        {
                            "window": window_name,
                            "size": window_size,
                            "db_start": db_window_start,
                            "correct_start": correct_window_start,
                            "missing_count": len(missing_in_window),
                        }
                    )

    return {
        "stock_id": stock_id,
        "total": len(stock_dates),
        "expected": len(expected),
        "missing_count": len(missing),
        "missing_dates": missing[:10],
        "first_date": first_date,
        "last_date": last_date,
        "affected_windows": affected_windows,
        "status": "gap" if missing else "ok",
    }


def print_report(
    stocks: list[str],
    gap_stocks: list[dict],
    affected_stocks: list[dict],
    market_label: str,
) -> None:
    """輸出報告"""
    print(f"{'=' * 60}")
    print(f"[{market_label}] 有缺日的股票: "
          f"{len(gap_stocks)} / {len(stocks)} 檔")
    print(f"[{market_label}] 缺日影響計算的: {len(affected_stocks)} 檔")
    print(f"{'=' * 60}")

    if gap_stocks:
        print()
        print("=== 有缺日的股票 ===")
        for r in sorted(gap_stocks, key=lambda x: -x["missing_count"]):
            dates_str = ", ".join(r["missing_dates"][:5])
            if r["missing_count"] > 5:
                dates_str += f" ... (共 {r['missing_count']} 天)"
            impact = " ⚠️ 影響計算" if r["affected_windows"] else ""
            print(
                f"  {r['stock_id']:8} "
                f"DB:{r['total']}筆 "
                f"應有:{r['expected']}筆 "
                f"缺:{r['missing_count']}天{impact}"
            )
            if len(r["missing_dates"]) <= 10:
                print(f"           缺: {dates_str}")

    if affected_stocks:
        print()
        print("=== ⚠️ 缺日影響計算窗口的股票 ===")
        for r in affected_stocks:
            print(f"\n  {r['stock_id']} (缺 {r['missing_count']} 天):")
            for w in r["affected_windows"]:
                print(
                    f"    {w['window']:12} "
                    f"DB起點={w['db_start']} "
                    f"正確起點={w['correct_start']} "
                    f"窗口內缺{w['missing_count']}天"
                )

    if not gap_stocks:
        print(f"\n✅ [{market_label}] 所有股票資料完整，無缺日")
    elif not affected_stocks:
        print(
            f"\n✅ [{market_label}] 有 {len(gap_stocks)} 檔缺日，"
            "但都不影響目前的計算窗口"
        )
    else:
        print(
            f"\n❌ [{market_label}] 有 {len(affected_stocks)} 檔的缺日"
            "影響了計算窗口，數值可能失真"
        )


def main():
    parser = argparse.ArgumentParser(description="驗證個股資料完整性")
    parser.add_argument("--tw", action="store_true", help="檢查台股（預設美股）")
    parser.add_argument("--date", default=None, help="篩選日期 (YYYY-MM-DD)")
    parser.add_argument("--stock", default=None, help="指定股票代碼")
    parser.add_argument("--all", action="store_true", help="檢查全部股票")
    parser.add_argument("--db", default=None, help="自訂 DB 路徑")
    args = parser.parse_args()

    market = "tw" if args.tw else "us"
    cfg = MARKET_CONFIG[market]

    # 取得 DB
    if args.db:
        db_path = Path(args.db)
    else:
        db_path = download_db(market)

    if not db_path.exists():
        print(f"❌ 找不到 DB: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))

    # 建立基準日曆
    ref_calendar = get_reference_calendar(conn, market)
    print(
        f"基準日曆 ({cfg['ref_stock']}): {len(ref_calendar)} 天, "
        f"{ref_calendar[0]} ~ {ref_calendar[-1]}"
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
        print(f"篩選日期: {target_date}, 共 {len(stocks)} 檔")

    print(f"檢查股票: {len(stocks)} 檔")
    print()

    # 逐股檢查
    gap_stocks = []
    affected_stocks = []

    for stock_id in sorted(stocks):
        result = check_stock(
            conn, stock_id, ref_calendar, cfg["price_table"]
        )

        if result["status"] == "no_data":
            continue

        if result["missing_count"] > 0:
            gap_stocks.append(result)

        if result["affected_windows"]:
            affected_stocks.append(result)

    conn.close()

    print_report(stocks, gap_stocks, affected_stocks, cfg["label"])


if __name__ == "__main__":
    main()
