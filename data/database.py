"""
資料庫連線與操作
"""
from contextlib import contextmanager
from datetime import date
from typing import Generator, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config.settings import DATABASE_URL
from data.models import Base, StockInfo, DailyPrice, MarketIndex, FilterResult


class Database:
    """
    資料庫操作類別

    提供:
    - 連線管理
    - 資料表建立
    - CRUD 操作
    - 批次寫入
    """

    def __init__(self, database_url: Optional[str] = None):
        """
        初始化資料庫連線

        Args:
            database_url: 資料庫連線字串
        """
        self.database_url = database_url or DATABASE_URL
        self.engine = create_engine(
            self.database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autocommit=False,
            autoflush=False,
        )

        logger.info(f"資料庫連線初始化完成")

    def create_tables(self):
        """建立所有資料表"""
        Base.metadata.create_all(self.engine)
        logger.info("資料表建立完成")

    def drop_tables(self):
        """刪除所有資料表（危險操作）"""
        Base.metadata.drop_all(self.engine)
        logger.warning("所有資料表已刪除")

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """取得資料庫 Session（Context Manager）"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"資料庫操作失敗: {e}")
            raise
        finally:
            session.close()

    # ==================== StockInfo 操作 ====================

    def upsert_stock_info(self, df: pd.DataFrame) -> int:
        """
        批次寫入/更新股票基本資料

        Args:
            df: 包含 stock_id, stock_name, industry_category, type 的 DataFrame

        Returns:
            寫入/更新的筆數
        """
        if df.empty:
            return 0

        # 只保留需要的欄位，避免傳入多餘欄位
        required_cols = ["stock_id", "stock_name", "industry_category", "type"]
        df = df[[c for c in required_cols if c in df.columns]].copy()

        records = df.rename(columns={"type": "stock_type"}).to_dict("records")

        with self.get_session() as session:
            stmt = pg_insert(StockInfo).values(records)
            stmt = stmt.on_conflict_do_update(
                index_elements=["stock_id"],
                set_={
                    "stock_name": stmt.excluded.stock_name,
                    "industry_category": stmt.excluded.industry_category,
                    "stock_type": stmt.excluded.stock_type,
                }
            )
            session.execute(stmt)

        logger.info(f"寫入/更新 {len(records)} 筆股票基本資料")
        return len(records)

    def get_all_stock_info(self) -> pd.DataFrame:
        """取得所有股票基本資料"""
        with self.get_session() as session:
            query = session.query(StockInfo)
            df = pd.read_sql(query.statement, session.bind)
        return df

    def get_stock_info_dict(self) -> dict[str, dict]:
        """取得股票資訊字典 (stock_id -> info)"""
        df = self.get_all_stock_info()
        return {
            row["stock_id"]: {
                "stock_name": row["stock_name"],
                "industry_category": row.get("industry_category", "-"),
            }
            for _, row in df.iterrows()
        }

    # ==================== DailyPrice 操作 ====================

    def upsert_daily_price(self, df: pd.DataFrame) -> int:
        """
        批次寫入/更新每日股價

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
        }
        df = df.rename(columns=column_mapping)

        # 只保留需要的欄位
        columns = ["stock_id", "date", "open_price", "high_price",
                   "low_price", "close_price", "volume"]
        df = df[[c for c in columns if c in df.columns]]

        records = df.to_dict("records")
        total_count = len(records)

        # 分批寫入以避免記憶體問題（每批 5000 筆）
        BATCH_SIZE = 5000
        with self.get_session() as session:
            for i in range(0, total_count, BATCH_SIZE):
                batch = records[i:i + BATCH_SIZE]
                stmt = pg_insert(DailyPrice).values(batch)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_daily_price_stock_date",
                    set_={
                        "open_price": stmt.excluded.open_price,
                        "high_price": stmt.excluded.high_price,
                        "low_price": stmt.excluded.low_price,
                        "close_price": stmt.excluded.close_price,
                        "volume": stmt.excluded.volume,
                    }
                )
                session.execute(stmt)
                logger.debug(f"已寫入 {min(i + BATCH_SIZE, total_count)}/{total_count} 筆")

        logger.info(f"寫入/更新 {total_count} 筆股價資料")
        return total_count

    def get_daily_prices(
        self,
        start_date: date,
        end_date: date,
        stock_ids: Optional[list[str]] = None
    ) -> pd.DataFrame:
        """
        取得指定期間的股價資料

        Args:
            start_date: 開始日期
            end_date: 結束日期
            stock_ids: 指定股票代號列表（可選）

        Returns:
            股價 DataFrame
        """
        with self.get_session() as session:
            query = session.query(DailyPrice).filter(
                DailyPrice.date >= start_date,
                DailyPrice.date <= end_date
            )

            if stock_ids:
                query = query.filter(DailyPrice.stock_id.in_(stock_ids))

            query = query.order_by(DailyPrice.stock_id, DailyPrice.date)
            df = pd.read_sql(query.statement, session.bind)

        return df

    def get_latest_date(self) -> Optional[date]:
        """取得資料庫中最新的股價日期"""
        with self.get_session() as session:
            result = session.query(DailyPrice.date).order_by(
                DailyPrice.date.desc()
            ).first()

        return result[0] if result else None

    # ==================== MarketIndex 操作 ====================

    def upsert_market_index(self, df: pd.DataFrame) -> int:
        """
        批次寫入/更新大盤指數

        Args:
            df: 包含 date, taiex 的 DataFrame

        Returns:
            寫入/更新的筆數
        """
        if df.empty:
            return 0

        records = df[["date", "taiex"]].to_dict("records")

        with self.get_session() as session:
            # 使用批次 upsert 提升效能
            stmt = pg_insert(MarketIndex).values(records)
            stmt = stmt.on_conflict_do_update(
                index_elements=["date"],
                set_={"taiex": stmt.excluded.taiex}
            )
            session.execute(stmt)

        logger.info(f"寫入/更新 {len(records)} 筆大盤指數")
        return len(records)

    def get_market_index(
        self,
        start_date: date,
        end_date: date
    ) -> pd.DataFrame:
        """取得指定期間的大盤指數"""
        with self.get_session() as session:
            query = session.query(MarketIndex).filter(
                MarketIndex.date >= start_date,
                MarketIndex.date <= end_date
            ).order_by(MarketIndex.date)

            df = pd.read_sql(query.statement, session.bind)

        return df

    # ==================== FilterResult 操作 ====================

    def save_filter_results(
        self,
        results: list[dict],
        filter_type: str,
        filter_date: date
    ) -> int:
        """
        儲存篩選結果

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
            session.query(FilterResult).filter(
                FilterResult.filter_date == filter_date,
                FilterResult.filter_type == filter_type
            ).delete()

            # 批次寫入新結果（提升效能）
            records = [
                {
                    "filter_date": filter_date,
                    "filter_type": filter_type,
                    "stock_id": r["stock_id"],
                    "stock_name": r.get("stock_name", ""),
                    "industry_category": r.get("industry_category"),
                    "return_20d": r.get("return_20d"),
                    "is_strong_list": r.get("is_strong"),
                    "is_new_high_list": r.get("is_new_high"),
                    "today_price": r.get("today_price"),
                    "second_high_55d": r.get("second_high_55d"),
                    "gap_ratio": r.get("gap_ratio"),
                }
                for r in results
            ]
            session.bulk_insert_mappings(FilterResult, records)

        logger.info(f"儲存 {len(results)} 筆 {filter_type} 篩選結果")
        return len(results)

    def get_filter_results(
        self,
        filter_type: str,
        filter_date: date
    ) -> pd.DataFrame:
        """取得指定日期的篩選結果"""
        with self.get_session() as session:
            query = session.query(FilterResult).filter(
                FilterResult.filter_date == filter_date,
                FilterResult.filter_type == filter_type
            )
            df = pd.read_sql(query.statement, session.bind)

        return df

    # ==================== 工具方法 ====================

    def execute_sql(self, sql: str) -> None:
        """執行原始 SQL"""
        with self.get_session() as session:
            session.execute(text(sql))

    def health_check(self) -> bool:
        """檢查資料庫連線"""
        try:
            with self.get_session() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"資料庫連線失敗: {e}")
            return False
