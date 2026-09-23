"""
美股 SQLAlchemy 資料模型
完全獨立於台股模型，使用獨立的資料表

相容 Python 3.9+
"""
from __future__ import annotations

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


class USBase(DeclarativeBase):
    """美股 SQLAlchemy Base（獨立於台股）"""
    pass


class USStockInfo(USBase):
    """
    美股股票基本資料表

    儲存股票代號、名稱、交易所、產業分類等資訊
    """
    __tablename__ = "us_stock_info"

    stock_id: Mapped[str] = mapped_column(
        String(20), primary_key=True, comment="股票代號 (如 AAPL, MSFT)"
    )
    stock_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="公司名稱"
    )
    exchange: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, comment="交易所 (NYSE/NASDAQ/AMEX)"
    )
    sector: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="產業分類"
    )
    industry: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="細分產業"
    )
    market_cap: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(20, 2), nullable=True, comment="市值"
    )
    etf_flag: Mapped[Optional[str]] = mapped_column(
        String(1), nullable=True, comment="是否為 ETF (Y/N)"
    )
    # 自訂欄位（由 Google Sheet「自訂產業連結」分頁每日同步，每月更新不覆蓋）
    custom_industry1: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="自訂產業別1（覆蓋顯示用）"
    )
    custom_industry2: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="自訂產業別2（覆蓋顯示用）"
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
        return f"<USStockInfo({self.stock_id}, {self.stock_name})>"

    def to_dict(self) -> dict:
        """轉換為字典"""
        return {
            "stock_id": self.stock_id,
            "stock_name": self.stock_name,
            "company_name": self.stock_name,
            "exchange": self.exchange or "-",
            "sector": self.sector or "-",
            "industry": self.industry or "-",
            "industry_category": self.sector or "-",  # 相容台股欄位名稱
            "industry_category2": self.industry or "-",  # 相容台股欄位名稱
        }


class USDailyPrice(USBase):
    """
    美股每日股價資料表

    儲存股票每日 OHLCV 資料
    """
    __tablename__ = "us_daily_price"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, comment="股票代號"
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
    adj_close: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="調整後收盤價"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )

    __table_args__ = (
        UniqueConstraint("stock_id", "date", name="uq_us_daily_price_stock_date"),
        Index("idx_us_daily_price_stock_date", "stock_id", "date"),
        Index("idx_us_daily_price_date", "date"),
    )

    def __repr__(self):
        return f"<USDailyPrice({self.stock_id}, {self.date}, {self.close_price})>"


class USMarketIndex(USBase):
    """
    美股大盤指數資料表

    儲存 S&P 500 等大盤指數
    """
    __tablename__ = "us_market_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date_type] = mapped_column(
        Date, nullable=False, unique=True, index=True, comment="交易日期"
    )
    sp500: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="S&P 500 指數"
    )
    # 預留其他指數欄位
    dow_jones: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="道瓊工業指數"
    )
    nasdaq: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="NASDAQ 指數"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )

    __table_args__ = (
        Index("idx_us_market_index_date", "date"),
    )

    def __repr__(self):
        return f"<USMarketIndex({self.date}, SP500={self.sp500})>"


class USFilterResult(USBase):
    """
    美股篩選結果表

    儲存 VCP / 三線開花篩選結果，用於追蹤歷史
    """
    __tablename__ = "us_filter_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filter_date: Mapped[date_type] = mapped_column(
        Date, nullable=False, index=True, comment="篩選日期"
    )
    filter_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="篩選類型 (vcp/sanxian)"
    )
    stock_id: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="股票代號"
    )
    stock_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="公司名稱"
    )
    exchange: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True, comment="交易所"
    )
    sector: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, comment="產業分類"
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
        Index("idx_us_filter_result_date_type", "filter_date", "filter_type"),
    )

    def __repr__(self):
        return f"<USFilterResult({self.filter_type}, {self.filter_date}, {self.stock_id})>"


class USAnomalyWhitelist(USBase):
    """
    美股價格異常白名單

    用於記錄已驗證為「真實價格波動」的股票/日期，
    避免內部分割偵測重複觸發（如 BIRD 真實 6x 暴漲）。
    """
    __tablename__ = "us_anomaly_whitelist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stock_id: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, comment="股票代號"
    )
    anomaly_date: Mapped[date_type] = mapped_column(
        Date, nullable=False, comment="異常發生的日期（價格跳動的日期）"
    )
    prev_close: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="前一日收盤價"
    )
    today_close: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(12, 4), nullable=True, comment="當日收盤價"
    )
    ratio: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(10, 4), nullable=True, comment="跳動比值 today/prev"
    )
    reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="加入白名單原因（如：重新下載後仍有跳動，視為真實波動）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="建立時間"
    )

    __table_args__ = (
        UniqueConstraint("stock_id", "anomaly_date", name="uq_us_anomaly_stock_date"),
        Index("idx_us_anomaly_stock", "stock_id"),
    )

    def __repr__(self):
        return f"<USAnomalyWhitelist({self.stock_id}, {self.anomaly_date}, ratio={self.ratio})>"
