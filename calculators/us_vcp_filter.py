"""
美股 VCP 強勢股篩選器
複製自台股 vcp_filter.py，完全獨立運作
"""
from datetime import date
from typing import Optional

import pandas as pd
from loguru import logger

from config.us_settings import US_VCP_PARAMS
from calculators.us_moving_average import USMovingAverageCalculator


class USVCPFilter:
    """
    美股 VCP (Volatility Contraction Pattern) 強勢股篩選器

    篩選邏輯（與台股相同）:
    1. 強勢清單（4 條件 AND）:
       - 收盤價 > MA50 > MA150 > MA200
       - MA200 今日 > MA200 20日前
       - 股票 20 日報酬 > 大盤 20 日報酬
       - 收盤價 > 近 250 交易日盤中最低價 × 1.30（離低點至少 30%）

    2. 新高清單（3 條件 AND）:
       - 近 5 日最高價 == 近 250 交易日最高價（250 日最高點落在最近 5 日內）
       - 股票 20 日報酬 > 大盤 20 日報酬
       - 收盤價 > 近 250 交易日盤中最低價 × 1.30（離低點至少 30%）

    3. 最終結果 = 強勢清單 UNION 新高清單
    """

    def __init__(
        self,
        ma50_period: int = None,
        ma150_period: int = None,
        ma200_period: int = None,
        lookback_20d: int = None,
        lookback_52w: int = None,
        new_high_tolerance: float = None
    ):
        """
        初始化篩選器

        Args:
            ma50_period: MA50 週期
            ma150_period: MA150 週期
            ma200_period: MA200 週期
            lookback_20d: 20 日回看
            lookback_52w: 52 週回看（交易日）
            new_high_tolerance: 新高容差
        """
        self.ma50_period = ma50_period or US_VCP_PARAMS["ma50_period"]
        self.ma150_period = ma150_period or US_VCP_PARAMS["ma150_period"]
        self.ma200_period = ma200_period or US_VCP_PARAMS["ma200_period"]
        self.lookback_20d = lookback_20d or US_VCP_PARAMS["lookback_20d"]
        self.lookback_52w = lookback_52w or US_VCP_PARAMS["lookback_52w"]
        self.new_high_tolerance = (
            new_high_tolerance or US_VCP_PARAMS["new_high_tolerance"]
        )

    def filter(
        self,
        price_df: pd.DataFrame,
        market_return_20d: float,
        target_date: Optional[date] = None
    ) -> pd.DataFrame:
        """
        執行美股 VCP 篩選

        Args:
            price_df: 股價 DataFrame（需包含計算所需欄位）
            market_return_20d: 大盤 20 日報酬率（S&P 500）
            target_date: 目標日期（預設為最新日期）

        Returns:
            篩選結果 DataFrame，包含:
            - stock_id: 股票代號
            - return_20d: 20 日報酬率
            - is_strong: 是否為強勢清單
            - is_new_high: 是否為新高清單
        """
        if price_df.empty:
            logger.warning("輸入資料為空")
            return pd.DataFrame()

        # 準備計算資料
        df = USMovingAverageCalculator.prepare_vcp_data(price_df)

        if df.empty:
            logger.warning("計算資料為空")
            return pd.DataFrame()

        # 取得目標日期的資料（統一日期格式避免比較問題）
        df["date"] = pd.to_datetime(df["date"]).dt.date
        if target_date:
            df = df[df["date"] == target_date]
        else:
            # 取最新日期
            latest_date = df["date"].max()
            df = df[df["date"] == latest_date]
            logger.info(f"使用最新日期: {latest_date}")

        if df.empty:
            logger.warning("目標日期無資料")
            return pd.DataFrame()

        # 篩選強勢清單
        strong_mask = self._filter_strong_list(df)

        # 篩選新高清單
        new_high_mask = self._filter_new_high_list(df)

        # 篩選打敗大盤（處理 NaN 值）
        # 過濾異常報酬率（> 500% 或 < -90% 視為分割/合股未修正）
        return_20d = df["return_20d"].fillna(-float("inf"))
        sane_return = (return_20d > -0.9) & (return_20d < 5.0)
        beat_market_mask = (return_20d > market_return_20d) & sane_return

        # 離 250 日低點門檻（強勢/新高共用）：收盤 > 近250日盤中最低 × 1.30
        # low_250d 由 prepare_vcp_data 產生；NaN（資料不足）比較為 False，不誤選
        off_low_mask = df["close_price"].fillna(0) > df["low_250d"] * US_VCP_PARAMS["low_250d_mult"]

        # 合併條件（使用 .loc 避免 SettingWithCopyWarning）
        df = df.copy()
        df.loc[:, "is_strong"] = strong_mask & beat_market_mask & off_low_mask
        df.loc[:, "is_new_high"] = new_high_mask & beat_market_mask & off_low_mask

        # 篩選出符合任一條件的股票
        result_mask = df["is_strong"] | df["is_new_high"]
        result_df = df[result_mask].copy()

        if result_df.empty:
            logger.info("無符合美股 VCP 條件的股票")
            return pd.DataFrame()

        # 整理輸出欄位
        output_columns = [
            "stock_id",
            "date",
            "close_price",
            "return_20d",
            "is_strong",
            "is_new_high",
        ]
        result_df = result_df[[c for c in output_columns if c in result_df.columns]]

        logger.info(
            f"美股 VCP 篩選完成: 強勢清單 {df['is_strong'].sum()} 檔, "
            f"新高清單 {df['is_new_high'].sum()} 檔, "
            f"總計 {len(result_df)} 檔 (聯集)"
        )

        return result_df

    def _filter_strong_list(self, df: pd.DataFrame) -> pd.Series:
        """
        篩選強勢清單

        條件:
        1. close > MA50 > MA150 > MA200
        2. MA200 今日 > MA200 20日前
        """
        # 條件 1: 價格趨勢（處理 NaN）
        close = df["close_price"].fillna(0)
        ma50 = df["ma50"].fillna(float("inf"))
        ma150 = df["ma150"].fillna(float("inf"))
        ma200 = df["ma200"].fillna(float("inf"))

        cond1 = (close > ma50) & (ma50 > ma150) & (ma150 > ma200)

        # 條件 2: MA200 上升
        cond2 = df["ma200_slope_20d"].fillna(-1) > 0

        return cond1 & cond2

    def _filter_new_high_list(self, df: pd.DataFrame) -> pd.Series:
        """
        篩選新高清單

        條件:
        1. 近 5 日最高價 == 近 250 交易日最高價 → 250 日最高點落在最近 5 日內（創 250 日新高）
        """
        # high_5d <= high_250d 恆成立，故 >= 等同 ==；NaN（資料不足）比較為 False，不誤判
        cond = df["high_5d"] >= df["high_250d"]
        return cond


def calculate_us_market_return(
    market_df: pd.DataFrame,
    target_date: date,
    lookback: int = 20
) -> float:
    """
    計算美股大盤報酬率（S&P 500）

    Args:
        market_df: 大盤指數 DataFrame（需包含 date, sp500）
        target_date: 目標日期
        lookback: 回看天數

    Returns:
        報酬率（小數形式）
    """
    if market_df.empty:
        logger.warning("美股大盤資料為空，回傳 0")
        return 0.0

    df = market_df.copy()

    # 統一日期格式為 datetime 進行比較
    df["date"] = pd.to_datetime(df["date"])
    target_dt = pd.to_datetime(target_date)

    df = df.sort_values("date").reset_index(drop=True)

    # 找到目標日期或之前最近的日期
    df_before = df[df["date"] <= target_dt]
    if df_before.empty:
        logger.warning("無符合的美股大盤資料")
        return 0.0

    target_pos = len(df_before) - 1

    if target_pos < lookback:
        logger.warning(f"資料不足 {lookback} 天，使用可用資料 ({target_pos} 天)")
        lookback = target_pos

    if lookback == 0:
        return 0.0

    current_price = df.iloc[target_pos]["sp500"]
    past_price = df.iloc[target_pos - lookback]["sp500"]

    # 處理無效價格
    if pd.isna(current_price) or pd.isna(past_price) or past_price == 0:
        return 0.0

    return float((current_price - past_price) / past_price)
