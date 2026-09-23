"""
三線開花篩選器
"""
from datetime import date
from typing import Optional

import pandas as pd
from loguru import logger

from config.settings import SANXIAN_PARAMS
from calculators.moving_average import MovingAverageCalculator


class SanxianFilter:
    """
    三線開花篩選器

    篩選邏輯（2 條件 AND）:
    1. 收盤價 > MA8 > MA21 > MA55
    2. 收盤價 == 55 日最高價（創新高）

    輸出欄位:
    - 今日股價
    - 55 日內次高價
    - 差距比例 = (今日股價 / 55日次高價) - 1
    """

    def __init__(
        self,
        ma8_period: int = None,
        ma21_period: int = None,
        ma55_period: int = None
    ):
        """
        初始化篩選器

        Args:
            ma8_period: MA8 週期
            ma21_period: MA21 週期
            ma55_period: MA55 週期
        """
        self.ma8_period = ma8_period or SANXIAN_PARAMS["ma8_period"]
        self.ma21_period = ma21_period or SANXIAN_PARAMS["ma21_period"]
        self.ma55_period = ma55_period or SANXIAN_PARAMS["ma55_period"]

    def filter(
        self,
        price_df: pd.DataFrame,
        target_date: Optional[date] = None
    ) -> pd.DataFrame:
        """
        執行三線開花篩選

        Args:
            price_df: 股價 DataFrame（需包含計算所需欄位）
            target_date: 目標日期（預設為最新日期）

        Returns:
            篩選結果 DataFrame，包含:
            - stock_id: 股票代號
            - today_price: 今日股價
            - second_high_55d: 55 日內次高價
            - gap_ratio: 差距比例
        """
        if price_df.empty:
            logger.warning("輸入資料為空")
            return pd.DataFrame()

        # 準備計算資料
        df = MovingAverageCalculator.prepare_sanxian_data(price_df)

        if df.empty:
            logger.warning("計算資料為空")
            return pd.DataFrame()

        # 取得目標日期的資料（統一日期格式避免比較問題）
        df["date"] = pd.to_datetime(df["date"]).dt.date
        if target_date:
            df = df[df["date"] == target_date]
        else:
            latest_date = df["date"].max()
            df = df[df["date"] == latest_date]
            logger.info(f"使用最新日期: {latest_date}")

        if df.empty:
            logger.warning("目標日期無資料")
            return pd.DataFrame()

        # 條件 1: 三線開花排列（處理 NaN）
        close = df["close_price"].fillna(0)
        ma8 = df["ma8"].fillna(float("inf"))
        ma21 = df["ma21"].fillna(float("inf"))
        ma55 = df["ma55"].fillna(float("inf"))

        cond1 = (close > ma8) & (ma8 > ma21) & (ma21 > ma55)

        # 條件 2: 創 55 日新高
        high_55d = df["high_55d"].fillna(float("inf"))
        cond2 = close >= high_55d

        # 防呆：排除 20 日漲幅異常（分割/合股未修正的假訊號）
        # 只排除「有值且超出 -90% ~ +500%」；無值（新股不足 20 日）保留，不誤殺
        if "return_20d" in df.columns:
            ret = df["return_20d"]
            sane = ~(ret.notna() & ((ret <= -0.9) | (ret >= 5.0)))
        else:
            sane = pd.Series(True, index=df.index)

        # 合併條件
        result_mask = cond1 & cond2 & sane
        result_df = df[result_mask].copy()

        if result_df.empty:
            logger.info("無符合三線開花條件的股票")
            return pd.DataFrame()

        # 計算差距比例（處理除以零，使用 .loc 避免 SettingWithCopyWarning）
        result_df.loc[:, "today_price"] = result_df["close_price"]

        # 確認次高價欄位是否存在
        second_high_col = "second_high_55d"
        if second_high_col not in result_df.columns:
            logger.warning(f"欄位 {second_high_col} 不存在，無法計算差距比例")
            result_df.loc[:, "second_high_55d"] = None
            result_df.loc[:, "gap_ratio"] = None
        else:
            second_high = result_df[second_high_col].fillna(1).replace(0, 1)
            result_df.loc[:, "gap_ratio"] = (result_df["close_price"] / second_high - 1)

        # 整理輸出欄位
        output_columns = [
            "stock_id",
            "date",
            "today_price",
            "second_high_55d",
            "gap_ratio",
            "return_20d",
        ]
        result_df = result_df[[c for c in output_columns if c in result_df.columns]]

        logger.info(f"三線開花篩選完成: {len(result_df)} 檔")

        return result_df
