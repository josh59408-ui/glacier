"""
SQLAlchemy 資料模型
相容 Python 3.9+
"""
from __future__ import annotations  # 支援 Python 3.9 的 type hints

from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    String,
    Text,
    Integer,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Numeric,
    Index,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy Base"""
    pass


class StockInfo(Base):
    """
    股票基本資料表

    儲存股票代號、名稱、產業分類等資訊
    """
    __tablename__ = "stock_info"

    stock_id: Mapped[str] = mapped_column(
        String(10), primary_key=True, comment="股票代號"
    )
    stock_name: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="股票名稱"
    )
    industry_category: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="產業分類1"
    )
    industry_category2: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="產業分類2"
    )
    stock_type: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, comment="股票類型 (twse/tpex)"
    )
    # 自訂欄位（由 Google Sheet「自訂產業連結」分頁每日同步，每月更新不覆蓋）
    custom_industry1: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="自訂產業別1（覆蓋顯示用）"
    )
    custom_industry2: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="自訂產業別2（覆蓋顯示用）"
    )
    custom_link: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True, comment="自訂連結"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新時間"
    )

    def __repr__(self):
        return f"<StockInfo({self.stock_id}, {self.stock_name})>"

    def to_dict(self) -> dict:
        """轉換為字典"""
        return {
            "stock_id": self.stock_id,
            "stock_name": self.stock_name,
            "company_name": self.stock_name,  # 用股名替代公司名
            "industry_category": self.industry_category or "-",
            "industry_category2": self.industry_category2 or "-",
            "product_mix": "-",  # 無此資料
        }


class DailyPrice(Base):
    """
    每日股價資料表

    儲存股票每日 OHLCV 資料
    """
    __tablename__ = "daily_price"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id: Mapped[str] = mapped_column(
        String(10), nullable=False, index=True, comment="股票代號"
    )
    date: Mapped[date_type] = mapped_column(
        Date, nullable=False, index=True, comment="交易日期"
    )
    open_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="開盤價"
    )
    high_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="最高價"
    )
    low_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="最低價"
    )
    close_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="收盤價"
    )
    volume: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="成交量"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )

    __table_args__ = (
        UniqueConstraint("stock_id", "date", name="uq_daily_price_stock_date"),
        Index("idx_daily_price_stock_date", "stock_id", "date"),
        Index("idx_daily_price_date", "date"),
    )

    def __repr__(self):
        return f"<DailyPrice({self.stock_id}, {self.date}, {self.close_price})>"


class MarketIndex(Base):
    """
    大盤指數資料表

    儲存加權指數等大盤資料
    """
    __tablename__ = "market_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date_type] = mapped_column(
        Date, nullable=False, unique=True, index=True, comment="交易日期"
    )
    taiex: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="加權指數"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )

    __table_args__ = (
        Index("idx_market_index_date", "date"),
    )

    def __repr__(self):
        return f"<MarketIndex({self.date}, {self.taiex})>"


class FilterResult(Base):
    """
    篩選結果表

    儲存 VCP / 三線開花篩選結果，用於追蹤歷史
    """
    __tablename__ = "filter_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filter_date: Mapped[date_type] = mapped_column(
        Date, nullable=False, index=True, comment="篩選日期"
    )
    filter_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="篩選類型 (vcp/sanxian)"
    )
    stock_id: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="股票代號"
    )
    stock_name: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="股票名稱"
    )
    industry_category: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True, comment="產業分類"
    )

    # VCP 專用欄位
    return_20d: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 4), nullable=True, comment="近20日股價漲幅"
    )
    is_strong_list: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, comment="強勢清單"
    )
    is_new_high_list: Mapped[Optional[bool]] = mapped_column(
        Boolean, nullable=True, comment="新高清單"
    )

    # 三線開花專用欄位
    today_price: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="今日股價"
    )
    second_high_55d: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="55日內次高價"
    )
    gap_ratio: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(8, 4), nullable=True, comment="差距比例"
    )

    # 指標詳情（JSON 格式，供前端 tooltip 顯示）
    indicator_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="篩選指標原始值 (JSON)"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )

    __table_args__ = (
        Index("idx_filter_result_date_type", "filter_date", "filter_type"),
    )

    def __repr__(self):
        return f"<FilterResult({self.filter_type}, {self.filter_date}, {self.stock_id})>"
