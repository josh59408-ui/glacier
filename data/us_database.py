"""
美股 SQLite 資料庫連線與操作
使用獨立的 zf_trend_us.db 資料庫
完全獨立於台股資料庫操作
"""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Generator, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import Session, sessionmaker

from data.us_models import USBase, USStockInfo, USDailyPrice, USMarketIndex, USFilterResult
from config.us_settings import US_SQLITE_DB_PATH


class USSQLiteDatabase:
    """
    美股 SQLite 資料庫操作類別

    特點:
    - 使用獨立的 zf_trend_us.db 資料庫
    - 完全與台股資料庫隔離
    - 支援所有基本 CRUD 操作
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        初始化資料庫連線

        Args:
            db_path: 資料庫檔案路徑（預設為 data/zf_trend_us.db）
        """
        self.db_path = db_path or US_SQLITE_DB_PATH

        # 確保目錄存在
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        self.database_url = f"sqlite:///{self.db_path}"

        self.engine = create_engine(
            self.database_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )

        # 啟用 WAL 模式以提升效能
        @event.listens_for(self.engine, "connect")
        def set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")  # 64MB cache
            cursor.close()

        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autocommit=False,
            autoflush=False,
        )

        logger.info(f"美股 SQLite 資料庫初始化完成: {self.db_path}")

    def create_tables(self):
        """建立所有美股資料表"""
        USBase.metadata.create_all(self.engine)
        self._migrate_schema()
        logger.info("美股資料表建立完成")

    def _migrate_schema(self):
        """Schema 遷移：既有 DB 補上新欄位（如果不存在）"""
        with self.engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(us_stock_info)"))
            columns = [row[1] for row in result.fetchall()]
            # 自訂欄位（Sheet 同步用，每月更新不覆蓋）
            for col, ddl in (
                ("custom_industry1", "ALTER TABLE us_stock_info ADD COLUMN custom_industry1 VARCHAR(100)"),
                ("custom_industry2", "ALTER TABLE us_stock_info ADD COLUMN custom_industry2 VARCHAR(100)"),
                ("custom_link", "ALTER TABLE us_stock_info ADD COLUMN custom_link VARCHAR(500)"),
            ):
                if col not in columns:
                    conn.execute(text(ddl))
                    conn.commit()
                    logger.info(f"美股 Schema 遷移: 已添加 {col} 欄位")

    def drop_tables(self):
        """刪除所有美股資料表（危險操作）"""
        USBase.metadata.drop_all(self.engine)
        logger.warning("美股所有資料表已刪除")

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """取得資料庫 Session（Context Manager）"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"美股資料庫操作失敗: {e}")
            raise
        finally:
            session.close()

    # ==================== USStockInfo 操作 ====================

    def upsert_stock_info(self, df: pd.DataFrame) -> int:
        """
        批次寫入/更新美股股票基本資料

        Args:
            df: 包含 stock_id, stock_name, exchange, sector, industry 的 DataFrame

        Returns:
            寫入/更新的筆數
        """
        if df.empty:
            return 0

        # 標準化欄位名稱
        column_mapping = {
            "symbol": "stock_id",
            "Symbol": "stock_id",
            "name": "stock_name",
            "Name": "stock_name",
            "Security Name": "stock_name",
            "Exchange": "exchange",
            "Sector": "sector",
            "Industry": "industry",
            "ETF": "etf_flag",
        }
        df = df.rename(columns=column_mapping)

        # 只保留需要的欄位
        required_cols = ["stock_id", "stock_name", "exchange", "sector", "industry", "etf_flag"]
        df = df[[c for c in required_cols if c in df.columns]].copy()

        count = 0
        with self.get_session() as session:
            for _, row in df.iterrows():
                existing = session.query(USStockInfo).filter(
                    USStockInfo.stock_id == row["stock_id"]
                ).first()

                if existing:
                    existing.stock_name = row.get("stock_name", existing.stock_name)
                    existing.exchange = row.get("exchange", existing.exchange)
                    existing.sector = row.get("sector", existing.sector)
                    existing.industry = row.get("industry", existing.industry)
                    existing.etf_flag = row.get("etf_flag", existing.etf_flag)
                else:
                    session.add(USStockInfo(
                        stock_id=row["stock_id"],
                        stock_name=row.get("stock_name", ""),
                        exchange=row.get("exchange"),
                        sector=row.get("sector"),
                        industry=row.get("industry"),
                        etf_flag=row.get("etf_flag"),
                    ))
                count += 1

        logger.info(f"寫入/更新 {count} 筆美股股票基本資料")
        return count

    def get_all_stock_info(self) -> pd.DataFrame:
        """取得所有美股股票基本資料"""
        with self.get_session() as session:
            query = session.query(USStockInfo)
            df = pd.read_sql(query.statement, session.bind)
        return df

    def replace_custom_overrides(self, overrides: dict) -> int:
        """以 Sheet 為準覆寫自訂欄位（產業別1/2、連結）

        先清空目前有自訂值的列，再套用 overrides。
        overrides: { stock_id: {"i1","i2","link"} }
        """
        count = 0
        with self.get_session() as session:
            dirty = session.query(USStockInfo).filter(
                (USStockInfo.custom_industry1.isnot(None))
                | (USStockInfo.custom_industry2.isnot(None))
                | (USStockInfo.custom_link.isnot(None))
            ).all()
            for row in dirty:
                row.custom_industry1 = None
                row.custom_industry2 = None
                row.custom_link = None
            for sid, ov in overrides.items():
                row = session.get(USStockInfo, sid)
                if not row:
                    continue
                row.custom_industry1 = ov.get("i1") or None
                row.custom_industry2 = ov.get("i2") or None
                row.custom_link = ov.get("link") or None
                count += 1
        logger.info(f"美股自訂欄位覆寫: {count} 檔")
        return count

    def get_stock_info_dict(self) -> dict[str, dict]:
        """取得美股股票資訊字典 (stock_id -> info)"""
        df = self.get_all_stock_info()
        if df.empty:
            return {}
        return {
            row["stock_id"]: {
                "stock_name": row["stock_name"],
                "exchange": row.get("exchange", "-"),
                "sector": row.get("sector", "-"),
                "industry": row.get("industry", "-"),
                "industry_category": row.get("sector", "-"),  # 相容台股欄位
                "industry_category2": row.get("industry", "-"),
            }
            for _, row in df.iterrows()
        }

    def update_sector_industry(self, df: pd.DataFrame) -> int:
        """
        批次更新股票的 sector/industry

        Args:
            df: 包含 stock_id, sector, industry 的 DataFrame

        Returns:
            更新的筆數
        """
        if df.empty:
            return 0

        count = 0
        with self.get_session() as session:
            for _, row in df.iterrows():
                existing = session.query(USStockInfo).filter(
                    USStockInfo.stock_id == row["stock_id"]
                ).first()
                if existing:
                    sector = row.get("sector", "")
                    industry = row.get("industry", "")
                    if sector and sector != "None":
                        existing.sector = sector
                    if industry and industry != "None":
                        existing.industry = industry
                    count += 1

        logger.info(f"更新 {count} 筆美股產業分類")
        return count

    def get_stock_ids(self) -> list[str]:
        """取得所有美股股票代號"""
        df = self.get_all_stock_info()
        if df.empty:
            return []
        return df["stock_id"].tolist()

    # ==================== USDailyPrice 操作 ====================

    def upsert_daily_price(self, df: pd.DataFrame) -> int:
        """
        批次寫入/更新美股每日股價

        Args:
            df: 包含 stock_id, date, open, high, low, close, volume 的 DataFrame

        Returns:
            寫入/更新的筆數
        """
        if df.empty:
            return 0

        # 重新命名欄位
        column_mapping = {
            "open": "open_price",
            "high": "high_price",
            "low": "low_price",
            "close": "close_price",
            "Open": "open_price",
            "High": "high_price",
            "Low": "low_price",
            "Close": "close_price",
            "Volume": "volume",
            "Adj Close": "adj_close",
        }
        df = df.rename(columns=column_mapping)

        # 只保留需要的欄位
        columns = ["stock_id", "date", "open_price", "high_price",
                   "low_price", "close_price", "volume", "adj_close"]
        df = df[[c for c in columns if c in df.columns]].copy()

        # 去除重複
        df = df.drop_duplicates(subset=["stock_id", "date"], keep="last")

        total_count = len(df)

        # 按日期分批處理
        with self.get_session() as session:
            for target_date in df["date"].unique():
                day_df = df[df["date"] == target_date]
                day_stock_ids = day_df["stock_id"].unique().tolist()

                # 只刪除該日期中即將插入的股票資料
                session.query(USDailyPrice).filter(
                    USDailyPrice.date == target_date,
                    USDailyPrice.stock_id.in_(day_stock_ids)
                ).delete(synchronize_session=False)

                # 插入該日期的資料
                records = day_df.to_dict("records")
                session.bulk_insert_mappings(USDailyPrice, records)

        logger.info(f"寫入/更新 {total_count} 筆美股股價資料")
        return total_count

    def get_daily_prices(
        self,
        start_date: date,
        end_date: date,
        stock_ids: Optional[list[str]] = None
    ) -> pd.DataFrame:
        """
        取得指定期間的美股股價資料

        Args:
            start_date: 開始日期
            end_date: 結束日期
            stock_ids: 指定股票代號列表（可選）

        Returns:
            股價 DataFrame
        """
        with self.get_session() as session:
            query = session.query(USDailyPrice).filter(
                USDailyPrice.date >= start_date,
                USDailyPrice.date <= end_date
            )

            if stock_ids:
                query = query.filter(USDailyPrice.stock_id.in_(stock_ids))

            query = query.order_by(USDailyPrice.stock_id, USDailyPrice.date)
            df = pd.read_sql(query.statement, session.bind)

        return df

    def get_latest_date(self) -> Optional[date]:
        """取得美股資料庫中最新的股價日期"""
        with self.get_session() as session:
            result = session.query(USDailyPrice.date).order_by(
                USDailyPrice.date.desc()
            ).first()

        return result[0] if result else None

    def get_price_count_by_date(self, target_date: date) -> int:
        """取得指定日期的股價資料筆數

        Args:
            target_date: 目標日期

        Returns:
            該日期的股價資料筆數
        """
        with self.get_session() as session:
            count = session.query(USDailyPrice).filter(
                USDailyPrice.date == target_date
            ).count()
        return count

    # ==================== USMarketIndex 操作 ====================

    def upsert_market_index(self, df: pd.DataFrame) -> int:
        """
        批次寫入/更新美股大盤指數

        Args:
            df: 包含 date, sp500 的 DataFrame

        Returns:
            寫入/更新的筆數
        """
        if df.empty:
            return 0

        # 標準化欄位
        if "close" in df.columns and "sp500" not in df.columns:
            df = df.rename(columns={"close": "sp500"})

        required_cols = ["date", "sp500"]
        optional_cols = ["dow_jones", "nasdaq"]

        cols_to_use = [c for c in required_cols + optional_cols if c in df.columns]
        df = df[cols_to_use].copy()
        df = df.drop_duplicates(subset=["date"], keep="last")

        with self.get_session() as session:
            dates = df["date"].unique().tolist()
            session.query(USMarketIndex).filter(
                USMarketIndex.date.in_(dates)
            ).delete(synchronize_session=False)

            records = df.to_dict("records")
            session.bulk_insert_mappings(USMarketIndex, records)

        logger.info(f"寫入/更新 {len(records)} 筆美股大盤指數")
        return len(records)

    def get_market_index(
        self,
        start_date: date,
        end_date: date
    ) -> pd.DataFrame:
        """取得指定期間的美股大盤指數"""
        with self.get_session() as session:
            query = session.query(USMarketIndex).filter(
                USMarketIndex.date >= start_date,
                USMarketIndex.date <= end_date
            ).order_by(USMarketIndex.date)

            df = pd.read_sql(query.statement, session.bind)

        return df

    # ==================== USFilterResult 操作 ====================

    def save_filter_results(
        self,
        results: list[dict],
        filter_type: str,
        filter_date: date
    ) -> int:
        """
        儲存美股篩選結果

        Args:
            results: 篩選結果列表
            filter_type: 篩選類型 (vcp/sanxian)
            filter_date: 篩選日期

        Returns:
            儲存的筆數
        """
        if not results:
            return 0

        with self.get_session() as session:
            # 先刪除當天同類型的舊結果
            session.query(USFilterResult).filter(
                USFilterResult.filter_date == filter_date,
                USFilterResult.filter_type == filter_type
            ).delete()

            # 批次寫入新結果
            records = [
                {
                    "filter_date": filter_date,
                    "filter_type": filter_type,
                    "stock_id": r["stock_id"],
                    "stock_name": r.get("stock_name", ""),
                    "exchange": r.get("exchange"),
                    "sector": r.get("sector", r.get("industry_category")),
                    "return_20d": r.get("return_20d"),
                    "is_strong_list": r.get("is_strong"),
                    "is_new_high_list": r.get("is_new_high"),
                    "today_price": r.get("today_price"),
                    "second_high_55d": r.get("second_high_55d"),
                    "gap_ratio": r.get("gap_ratio"),
                    "indicator_json": r.get("indicator_json"),
                }
                for r in results
            ]
            session.bulk_insert_mappings(USFilterResult, records)

        logger.info(f"儲存 {len(results)} 筆美股 {filter_type} 篩選結果")
        return len(results)

    def get_filter_results(
        self,
        filter_type: str,
        filter_date: date
    ) -> pd.DataFrame:
        """取得指定日期的美股篩選結果"""
        with self.get_session() as session:
            query = session.query(USFilterResult).filter(
                USFilterResult.filter_date == filter_date,
                USFilterResult.filter_type == filter_type
            )
            df = pd.read_sql(query.statement, session.bind)

        return df

    # ==================== 工具方法 ====================

    def execute_sql(self, sql: str) -> None:
        """執行原始 SQL"""
        with self.get_session() as session:
            session.execute(text(sql))

    def health_check(self) -> bool:
        """檢查美股資料庫連線"""
        try:
            with self.get_session() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"美股資料庫連線失敗: {e}")
            return False

    def get_db_size(self) -> str:
        """取得美股資料庫檔案大小"""
        db_file = Path(self.db_path)
        if db_file.exists():
            size_bytes = db_file.stat().st_size
            if size_bytes < 1024:
                return f"{size_bytes} B"
            elif size_bytes < 1024 * 1024:
                return f"{size_bytes / 1024:.1f} KB"
            else:
                return f"{size_bytes / (1024 * 1024):.1f} MB"
        return "0 B"

    def vacuum(self):
        """壓縮美股資料庫檔案"""
        with self.engine.connect() as conn:
            conn.execute(text("VACUUM"))
        logger.info("美股資料庫已壓縮")

    def get_stats(self) -> dict:
        """取得美股資料庫統計資訊"""
        with self.get_session() as session:
            stock_count = session.query(USStockInfo).count()
            price_count = session.query(USDailyPrice).count()
            index_count = session.query(USMarketIndex).count()
            filter_count = session.query(USFilterResult).count()

        return {
            "stock_count": stock_count,
            "price_count": price_count,
            "index_count": index_count,
            "filter_count": filter_count,
            "db_size": self.get_db_size(),
        }
