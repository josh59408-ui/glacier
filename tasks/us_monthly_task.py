"""
美股每月任務
完全獨立於台股，使用獨立的資料庫和設定
"""
from datetime import date
from typing import Optional

from loguru import logger

from config.us_settings import get_us_client
from data.us_database import USSQLiteDatabase
from exporters.us_google_sheet import USGoogleSheetExporter


class USMonthlyTask:
    """
    美股每月任務

    執行流程:
    1. 取得最新美股股票清單
    2. 更新美股資料庫
    3. 更新美股 Google Sheet 公司主檔
    """

    def __init__(
        self,
        client=None,
        db: Optional[USSQLiteDatabase] = None,
        exporter: Optional[USGoogleSheetExporter] = None
    ):
        """
        初始化美股每月任務

        Args:
            client: 美股 API 客戶端
            db: 美股 SQLite 資料庫連線
            exporter: 美股 Google Sheet 匯出器
        """
        self.client = client or get_us_client()
        self.db = db or USSQLiteDatabase()
        self.exporter = exporter or USGoogleSheetExporter()

    def run(self) -> dict:
        """
        執行美股每月任務

        Returns:
            執行結果統計
        """
        logger.info("=== 開始執行美股每月任務 ===")

        result = {
            "date": date.today(),
            "success": False,
            "stock_count": 0,
            "errors": [],
        }

        try:
            # 確保美股資料表存在
            self.db.create_tables()

            # Step 1: 取得美股股票清單
            stock_df = self.client.get_stock_info()

            if stock_df.empty:
                result["errors"].append("無法取得美股股票清單")
                return result

            result["stock_count"] = len(stock_df)

            # Step 2: 更新美股資料庫
            self.db.upsert_stock_info(stock_df)

            # Step 2.5: 補充 sector/industry（NASDAQ FTP 不含產業分類）
            self._update_sector_industry(stock_df)

            # 重新讀取含產業分類的資料
            stock_df = self.db.get_all_stock_info()

            # Step 3: 匯出至美股 Google Sheet
            self._export_to_sheet(stock_df)

            result["success"] = True
            logger.info(f"=== 美股每月任務完成: 更新 {len(stock_df)} 檔股票 ===")

        except Exception as e:
            logger.error(f"美股每月任務失敗: {e}")
            result["errors"].append(str(e))

        return result

    def _update_sector_industry(self, stock_df):
        """從 yfinance 補充 sector/industry 資訊"""
        from api.us_stock_client_free import USStockClientFree

        # 只對尚無 sector 的股票查詢
        all_info = self.db.get_all_stock_info()
        missing = all_info[
            all_info["sector"].isna() | (all_info["sector"] == "") | (all_info["sector"] == "None")
        ]

        if missing.empty:
            logger.info("所有股票已有產業分類，跳過更新")
            return

        stock_ids = missing["stock_id"].tolist()
        logger.info(f"需補充產業分類: {len(stock_ids)} 檔")

        # 使用免費客戶端取得 sector/industry
        if isinstance(self.client, USStockClientFree):
            sector_df = self.client.get_stock_sector_industry(stock_ids)
        else:
            # 付費版可能有自己的方式
            free_client = USStockClientFree()
            sector_df = free_client.get_stock_sector_industry(stock_ids)

        if sector_df.empty:
            logger.warning("無法取得產業分類資料")
            return

        # 只更新有資料的
        sector_df = sector_df[
            (sector_df["sector"] != "") & sector_df["sector"].notna()
        ]

        if not sector_df.empty:
            self.db.update_sector_industry(sector_df)
            logger.info(f"已更新 {len(sector_df)} 檔股票的產業分類")

    def _export_to_sheet(self, stock_df):
        """匯出至美股 Google Sheet"""
        if not self.exporter.health_check():
            logger.warning("美股 Google Sheet 未連線，跳過匯出")
            return

        # 轉換為匯出格式
        records = stock_df.to_dict("records")
        data = [
            {
                "stock_id": row.get("stock_id", ""),
                "stock_name": row.get("stock_name", ""),
                "company_name": row.get("stock_name", ""),
                "exchange": row.get("exchange", "-"),
                "sector": row.get("sector", "-"),
                "industry": row.get("industry", "-"),
                "industry_category": row.get("sector", "-"),  # 相容欄位
                "industry_category2": row.get("industry", "-"),
            }
            for row in records
        ]

        # 匯出美股公司主檔
        self.exporter.export_company_master(data)

        # 更新紀錄
        self.exporter.update_company_master_log(
            note=f"美股公司主檔 {len(data)} 檔",
            success=True
        )


def run_us_monthly_task() -> dict:
    """
    執行美股每月任務的便捷函數

    Returns:
        執行結果
    """
    task = USMonthlyTask()
    return task.run()
