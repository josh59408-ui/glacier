"""從 Google Sheet「自訂產業連結」分頁同步自訂產業/連結到 DB

供 deploy-site workflow 在匯出 JSON 前呼叫，讓 Sheet 上的編輯不必等到隔天
daily task 才反映到網站。邏輯與 tasks/daily_task.py 的 Step 1.5 相同。

設計取捨：
- Sheet 是唯一真實來源。本腳本只更新 runner 上的暫時 DB 副本，**不回傳 Release
  備份**，避免與 daily task 搶同一份 Release 造成競態覆蓋（見 CLAUDE.md）。
- 任何失敗都只記錄警告並以 exit code 0 結束，同步失敗不該擋住網站部署。

用法：
    source .venv/bin/activate
    python scripts/sync_custom_overrides.py          # 台股 + 美股
    python scripts/sync_custom_overrides.py --tw     # 只台股
    python scripts/sync_custom_overrides.py --us     # 只美股
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


def _sync(market: str) -> bool:
    """同步單一市場，回傳是否成功套用（失敗只記警告，不拋出）"""
    if market == "tw":
        from data.sqlite_database import SQLiteDatabase
        from exporters.google_sheet import GoogleSheetExporter

        db = SQLiteDatabase()
        exporter = GoogleSheetExporter()
    else:
        from data.us_database import USSQLiteDatabase
        from exporters.us_google_sheet import USGoogleSheetExporter

        db = USSQLiteDatabase()
        exporter = USGoogleSheetExporter()

    label = "台股" if market == "tw" else "美股"

    if not Path(db.db_path).exists():
        logger.warning(f"{label}：DB 不存在（{db.db_path}），略過同步")
        return False

    stock_info = db.get_stock_info_dict()
    if not stock_info:
        logger.warning(f"{label}：DB 無股票主檔，略過同步")
        return False

    master = {sid: (info.get("stock_name") or "") for sid, info in stock_info.items()}
    overrides = exporter.sync_custom_overrides(master)
    if overrides is None:
        logger.warning(f"{label}：自訂欄位讀取失敗，沿用 DB 既有值")
        return False

    count = db.replace_custom_overrides(overrides)
    logger.info(f"{label}：已套用 {len(overrides)} 檔自訂值（更新 {count} 列）")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="從 Google Sheet「自訂產業連結」分頁同步自訂產業/連結到 DB"
    )
    parser.add_argument("--tw", action="store_true", help="只同步台股")
    parser.add_argument("--us", action="store_true", help="只同步美股")
    args = parser.parse_args()

    # 皆未指定 → 兩個市場都同步
    markets = [m for m, on in (("tw", args.tw), ("us", args.us)) if on] or ["tw", "us"]

    for market in markets:
        try:
            _sync(market)
        except Exception as e:
            logger.warning(f"{market} 自訂欄位同步失敗（不影響部署）: {e}")

    logger.info("=== 自訂產業連結同步結束 ===")


if __name__ == "__main__":
    main()
