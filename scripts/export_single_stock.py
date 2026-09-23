"""
匯出單一股票的完整資料到驗證 Google Sheet

用途：詳細對照資料庫計算結果（匯出全部歷史資料）
"""
import sqlite3
import sys
from datetime import date

import numpy as np
import pandas as pd
from loguru import logger

from calculators.moving_average import MovingAverageCalculator
from config.settings import SQLITE_DB_PATH, SHEET_IDS, GOOGLE_CREDENTIALS_PATH
from exporters.google_sheet import GoogleSheetExporter


def export_single_stock(stock_id: str):
    """
    匯出單一股票的完整資料

    Args:
        stock_id: 股票代號
    """
    logger.info(f"=== 開始匯出股票 {stock_id} 的完整資料 ===")

    # 連接資料庫
    conn = sqlite3.connect(SQLITE_DB_PATH)

    # 讀取該股票的所有歷史資料
    query = """
        SELECT stock_id, date, open_price, high_price, low_price, close_price, volume
        FROM daily_price
        WHERE stock_id = ?
        ORDER BY date
    """
    df = pd.read_sql_query(query, conn, params=(stock_id,))
    conn.close()

    if df.empty:
        logger.error(f"找不到股票 {stock_id} 的資料")
        return False

    logger.info(f"讀取 {len(df)} 筆歷史資料")
    logger.info(f"日期範圍: {df['date'].min()} ~ {df['date'].max()}")

    # 計算所有均線和指標
    logger.info("計算技術指標...")

    # VCP 相關均線
    df = MovingAverageCalculator.calculate_sma(df, [50, 150, 200])
    df = MovingAverageCalculator.calculate_ma_slope(df, "ma200", lookback=20)
    df = MovingAverageCalculator.calculate_returns(df, [20])
    df = MovingAverageCalculator.calculate_high_low(df, [5, 252])

    # 三線開花相關均線
    df = MovingAverageCalculator.calculate_sma(df, [8, 21, 55])
    df = MovingAverageCalculator.calculate_close_high(df, periods=[55])
    df = MovingAverageCalculator.calculate_second_high(df, period=55)

    # 準備匯出資料（全部歷史資料）
    df_export = df.copy()

    # 格式化數值
    def safe_round(val, decimals=4):
        """安全格式化數值"""
        if pd.isna(val) or (isinstance(val, float) and np.isinf(val)):
            return ""
        if isinstance(val, (int, float)):
            return round(val, decimals)
        return val

    # 準備標題列
    headers = [
        "stock_id", "date",
        "open_price", "high_price", "low_price", "close_price", "volume",
        "ma8", "ma21", "ma50", "ma55", "ma150", "ma200",
        "ma200_slope_20d", "return_20d",
        "high_5d", "high_260d", "high_55d", "second_high_55d"
    ]

    # 準備資料列
    rows = []
    for _, row in df_export.iterrows():
        rows.append([
            row.get("stock_id", ""),
            str(row.get("date", "")),
            safe_round(row.get("open_price"), 2),
            safe_round(row.get("high_price"), 2),
            safe_round(row.get("low_price"), 2),
            safe_round(row.get("close_price"), 2),
            int(row.get("volume", 0)) if pd.notna(row.get("volume")) else "",
            safe_round(row.get("ma8"), 4),
            safe_round(row.get("ma21"), 4),
            safe_round(row.get("ma50"), 4),
            safe_round(row.get("ma55"), 4),
            safe_round(row.get("ma150"), 4),
            safe_round(row.get("ma200"), 4),
            safe_round(row.get("ma200_slope_20d"), 4),
            safe_round(row.get("return_20d"), 4),
            safe_round(row.get("high_5d"), 2),
            safe_round(row.get("high_260d"), 2),
            safe_round(row.get("high_55d"), 2),
            safe_round(row.get("second_high_55d"), 2),
        ])

    # 匯出到 Google Sheet
    logger.info("匯出到 Google Sheet...")

    sheet_id = SHEET_IDS.get("verification")
    if not sheet_id:
        logger.error("未設定 SHEET_ID_VERIFICATION")
        return False

    exporter = GoogleSheetExporter(GOOGLE_CREDENTIALS_PATH)
    if not exporter.health_check():
        logger.error("無法連線到 Google Sheets")
        return False

    sheet = exporter._get_sheet(sheet_id)
    if not sheet:
        return False

    # 使用股票代號作為分頁名稱
    tab_name = f"Stock_{stock_id}"

    try:
        # 如果分頁已存在，先刪除
        try:
            existing = sheet.worksheet(tab_name)
            sheet.del_worksheet(existing)
            logger.info(f"已刪除舊分頁: {tab_name}")
        except Exception:
            pass

        # 建立新分頁
        worksheet = sheet.add_worksheet(
            title=tab_name,
            rows=len(rows) + 10,
            cols=len(headers) + 5,
            index=0  # 插入在最前面
        )

        # 寫入標題和資料
        all_data = [headers] + rows
        worksheet.update(all_data, "A1")

        logger.info(f"✅ 匯出完成: {len(rows)} 筆資料 -> {tab_name}")
        return True

    except Exception as e:
        logger.error(f"匯出失敗: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/export_single_stock.py <stock_id>")
        print("範例: python scripts/export_single_stock.py 1101")
        sys.exit(1)

    stock_id = sys.argv[1]
    success = export_single_stock(stock_id)
    sys.exit(0 if success else 1)
