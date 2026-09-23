"""
美股 API 客戶端抽象介面
提供可替換的資料來源架構

設計原則：
- 抽象介面定義統一的資料存取方法
- 免費版和付費版實作相同介面
- 切換資料來源只需修改 us_settings.py
"""
from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

import pandas as pd


class USStockClientBase(ABC):
    """
    美股 API 客戶端抽象基底類別

    所有美股資料來源（免費/付費）都必須實作此介面
    """

    @abstractmethod
    def get_stock_info(self) -> pd.DataFrame:
        """
        取得美股股票清單

        Returns:
            DataFrame 包含欄位:
            - stock_id: 股票代號 (如 AAPL, MSFT)
            - stock_name: 公司名稱
            - exchange: 交易所 (NYSE/NASDAQ/AMEX)
            - sector: 產業分類（選填）
            - industry: 細分產業（選填）
        """
        pass

    @abstractmethod
    def get_stock_price(
        self,
        start_date: date,
        end_date: date,
        stock_ids: Optional[list[str]] = None
    ) -> pd.DataFrame:
        """
        取得美股股價資料

        Args:
            start_date: 開始日期
            end_date: 結束日期
            stock_ids: 指定股票代號列表（可選，不指定則取全部）

        Returns:
            DataFrame 包含欄位:
            - stock_id: 股票代號
            - date: 交易日期
            - open: 開盤價
            - high: 最高價
            - low: 最低價
            - close: 收盤價
            - volume: 成交量
            - adj_close: 調整後收盤價（選填）
        """
        pass

    @abstractmethod
    def get_market_index(
        self,
        start_date: date,
        end_date: Optional[date] = None
    ) -> pd.DataFrame:
        """
        取得美股大盤指數（S&P 500）

        Args:
            start_date: 開始日期
            end_date: 結束日期（預設為 start_date）

        Returns:
            DataFrame 包含欄位:
            - date: 交易日期
            - sp500: S&P 500 指數收盤價
            - dow_jones: 道瓊指數（選填）
            - nasdaq: NASDAQ 指數（選填）
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        檢查 API 連線狀態

        Returns:
            是否連線正常
        """
        pass

    def get_stats(self) -> dict:
        """
        取得 API 使用統計（選填實作）

        Returns:
            統計資訊字典
        """
        return {}

    def get_error_log(self) -> list[dict]:
        """
        取得錯誤日誌（選填實作）

        Returns:
            錯誤日誌列表
        """
        return []
