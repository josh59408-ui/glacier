"""
驗證 Sheet 分頁清理

只保留最近 N 天的「驗證明細」分頁（YYMMDD_VCP / YYMMDD_三線），
超過保留窗口的舊分頁自動刪除，騰出 Google Sheet 儲存格空間，
避免驗證 Sheet 撞到 1000 萬格上限。

安全保證：
1. 只操作呼叫端傳入的「驗證 Sheet」——選股清單 Sheet 是不同的試算表 ID，
   本模組連碰都不會碰到。
2. 只刪符合 ^\\d{6}_(VCP|三線)$ 格式、且日期早於保留窗口的分頁。
3. 任何不符該格式的固定分頁（驗證日誌、公司主檔…）一律保留。
4. dry_run=True 時只列出「會刪哪些」，不實際刪除。
"""
import re
from datetime import date, timedelta
from typing import Optional

from loguru import logger

# 驗證明細分頁名稱格式：YYMMDD_VCP 或 YYMMDD_三線
_TAB_PATTERN = re.compile(r"^(\d{2})(\d{2})(\d{2})_(VCP|三線)$")


def _parse_tab_date(title: str) -> Optional[date]:
    """從分頁名稱解析日期；非驗證明細格式則回傳 None（代表不可刪）"""
    m = _TAB_PATTERN.match(title)
    if not m:
        return None
    try:
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return date(2000 + yy, mm, dd)
    except ValueError:
        return None


def cleanup_verification_tabs(
    spreadsheet,
    keep_days: int = 10,
    today: Optional[date] = None,
    dry_run: bool = False,
    delay_sec: float = 0.0,
) -> dict:
    """
    清理驗證 Sheet 中過舊的驗證明細分頁。

    Args:
        spreadsheet: 已開啟的 gspread Spreadsheet 物件（必須是「驗證 Sheet」）
        keep_days: 保留最近幾天（預設 10）
        today: 基準日（預設今天；供測試注入）
        dry_run: True 則只列出不刪除

    Returns:
        dict: {
            "deleted": list[str],   # 已刪（或 dry-run 將刪）的分頁名稱
            "kept": int,            # 保留的明細分頁數
            "skipped_fixed": int,   # 跳過的固定分頁數
            "dry_run": bool,
        }
    """
    if spreadsheet is None:
        logger.warning("驗證清理：未提供 spreadsheet，跳過")
        return {"deleted": [], "kept": 0, "skipped_fixed": 0, "dry_run": dry_run}

    base_day = today or date.today()
    cutoff = base_day - timedelta(days=keep_days)

    to_delete: list[tuple] = []
    kept = 0
    skipped_fixed = 0

    for ws in spreadsheet.worksheets():
        tab_date = _parse_tab_date(ws.title)
        if tab_date is None:
            skipped_fixed += 1  # 固定分頁（非日期格式）→ 一律保留
            continue
        if tab_date < cutoff:
            to_delete.append((ws, ws.title))
        else:
            kept += 1

    deleted_titles = [t for _, t in to_delete]

    if not to_delete:
        logger.info(
            f"驗證清理：無早於 {cutoff}（保留 {keep_days} 天）的舊分頁需刪除"
            f"；保留明細 {kept} 張、固定分頁 {skipped_fixed} 張"
        )
        return {
            "deleted": [], "kept": kept,
            "skipped_fixed": skipped_fixed, "dry_run": dry_run,
        }

    if dry_run:
        logger.info(
            f"[DRY-RUN] 驗證清理：將刪除 {len(to_delete)} 張早於 {cutoff} 的舊分頁"
            f"（不實際刪除）：{deleted_titles}"
        )
        return {
            "deleted": deleted_titles, "kept": kept,
            "skipped_fixed": skipped_fixed, "dry_run": True,
        }

    logger.info(
        f"驗證清理：開始刪除 {len(to_delete)} 張早於 {cutoff} 的舊驗證明細：{deleted_titles}"
    )
    import time

    ok: list[str] = []
    for idx, (ws, title) in enumerate(to_delete, 1):
        for attempt in range(3):
            try:
                spreadsheet.del_worksheet(ws)
                ok.append(title)
                break
            except Exception as e:
                msg = str(e)
                if ("429" in msg or "quota" in msg.lower() or "rate" in msg.lower()) and attempt < 2:
                    wait = 30 * (attempt + 1)
                    logger.warning(
                        f"驗證清理：刪 {title} 撞速率限制，等 {wait} 秒後重試 "
                        f"({attempt + 1}/3)..."
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"驗證清理：刪除分頁 {title} 失敗：{e}")
                break
        if delay_sec > 0 and idx < len(to_delete):
            time.sleep(delay_sec)

    logger.info(
        f"驗證清理完成：成功刪除 {len(ok)}/{len(to_delete)} 張"
        f"；保留明細 {kept} 張、固定分頁 {skipped_fixed} 張"
    )
    return {
        "deleted": ok, "kept": kept,
        "skipped_fixed": skipped_fixed, "dry_run": False,
    }
