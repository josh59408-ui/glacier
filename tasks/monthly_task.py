"""
每月任務
使用 HybridClient (FinMind + yfinance) + SQLite 架構
"""
from datetime import date
from typing import Optional

from loguru import logger

from api.hybrid_client import HybridClient
from data.sqlite_database import SQLiteDatabase
from exporters.google_sheet import GoogleSheetExporter


class MonthlyTask:
    """
    每月任務

    執行流程:
    1. 取得最新股票清單
    2. 更新資料庫
    3. 更新 Google Sheet 公司主檔
    """

    def __init__(
        self,
        client: Optional[HybridClient] = None,
        db: Optional[SQLiteDatabase] = None,
        exporter: Optional[GoogleSheetExporter] = None
    ):
        """
        初始化每月任務

        Args:
            client: HybridClient API 客戶端
            db: SQLite 資料庫連線
            exporter: Google Sheet 匯出器
        """
        self.client = client or HybridClient()
        self.db = db or SQLiteDatabase()
        self.exporter = exporter or GoogleSheetExporter()

    def run(self) -> dict:
        """
        執行每月任務

        Returns:
            執行結果統計
        """
        logger.info("=== 開始執行每月任務 ===")

        result = {
            "date": date.today(),
            "success": False,
            "stock_count": 0,
            "errors": [],
        }

        try:
            # Step 1: 取得股票清單
            stock_df = self.client.get_stock_info()

            if stock_df.empty:
                result["errors"].append("無法取得股票清單")
                return result

            result["stock_count"] = len(stock_df)

            # Step 2: 更新資料庫
            self.db.upsert_stock_info(stock_df)

            # Step 3: 匯出至 Google Sheet
            self._export_to_sheet(stock_df)

            result["success"] = True
            logger.info(f"=== 每月任務完成: 更新 {len(stock_df)} 檔股票 ===")

        except Exception as e:
            logger.error(f"每月任務失敗: {e}")
            result["errors"].append(str(e))

        return result

    def _export_to_sheet(self, stock_df):
        """匯出至 Google Sheet"""
        if not self.exporter.health_check():
            logger.warning("Google Sheet 未連線，跳過匯出")
            return

        # 轉換為匯出格式（使用 to_dict 而非 iterrows 提升效能）
        # FinMind 提供多重產業分類（如 2330 台積電：半導體業、電子工業）
        records = stock_df.to_dict("records")
        data = [
            {
                "stock_id": row["stock_id"],
                "stock_name": row["stock_name"],
                "company_name": row["stock_name"],
                "industry_category": row.get("industry_category", "-"),
                "industry_category2": row.get("industry_category2", "-"),
                "product_mix": "-",
            }
            for row in records
        ]

        # 匯出公司主檔
        self.exporter.export_company_master(data)

        # 更新紀錄（統一表格格式）
        self.exporter.update_company_master_log(
            note=f"公司主檔 {len(data)} 檔",
            success=True
        )


def run_monthly_task() -> dict:
    """
    執行每月任務的便捷函數

    Returns:
        執行結果
    """
    task = MonthlyTask()
    return task.run()
