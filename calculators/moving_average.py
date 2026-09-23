"""
均線計算模組
"""
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger


class MovingAverageCalculator:
    """
    移動平均線計算器

    支援:
    - 簡單移動平均 (SMA)
    - 指定期間的最高/最低價
    - 股價漲幅計算
    """

    @staticmethod
    def fix_zero_prices(
        df: pd.DataFrame,
        price_columns: tuple[str, ...] = (
            "close_price", "open_price", "high_price", "low_price"
        ),
    ) -> pd.DataFrame:
        """
        修正零價異常：將 close_price=0 的資料用同一股票前一個交易日的值補齊

        原因：FinMind API 偶爾回傳 OHLC 全為 0 但有成交量的異常資料，
        會導致均線計算偏差和 pct_change 產生 inf。

        Args:
            df: 股價 DataFrame（需包含 stock_id, date, close_price）
            price_columns: 需要修正的價格欄位

        Returns:
            修正後的 DataFrame（新物件）
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        zero_mask = df["close_price"] == 0
        zero_count = zero_mask.sum()

        if zero_count == 0:
            return df

        # 將 0 替換為 NaN，再用同一股票的前一天 forward-fill
        for col in price_columns:
            if col in df.columns:
                df.loc[zero_mask, col] = np.nan
                df[col] = df.groupby("stock_id")[col].ffill()

        remaining = df["close_price"].isna().sum()
        if remaining > 0:
            logger.warning(f"仍有 {remaining} 筆無法補齊（該股票無前日資料）")

        fixed = zero_count - remaining
        if fixed > 0:
            logger.info(f"已修正 {fixed} 筆零價異常（forward-fill 前一交易日收盤價）")

        return df

    @staticmethod
    def calculate_sma(
        df: pd.DataFrame,
        periods: list[int],
        price_column: str = "close_price"
    ) -> pd.DataFrame:
        """
        計算簡單移動平均線

        Args:
            df: 股價 DataFrame（需包含 stock_id, date, price_column）
            periods: 均線週期列表 [8, 21, 50, 55, 150, 200]
            price_column: 價格欄位名稱

        Returns:
            加入均線欄位的 DataFrame
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        for period in periods:
            col_name = f"ma{period}"
            df[col_name] = df.groupby("stock_id")[price_column].transform(
                lambda x: x.rolling(window=period, min_periods=period).mean()
            )
            logger.debug(f"計算 MA{period} 完成")

        return df

    @staticmethod
    def calculate_high_low(
        df: pd.DataFrame,
        periods: list[int],
        high_column: str = "high_price",
        low_column: str = "low_price"
    ) -> pd.DataFrame:
        """
        計算指定期間的最高價與最低價

        Args:
            df: 股價 DataFrame
            periods: 週期列表
            high_column: 最高價欄位
            low_column: 最低價欄位

        Returns:
            加入最高/最低價欄位的 DataFrame
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        for period in periods:
            # 有多少資料就算多少，不足 52 週就用現有資料的最高/最低
            min_required = 1

            # 最高價
            high_col = f"high_{period}d"
            df[high_col] = df.groupby("stock_id")[high_column].transform(
                lambda x: x.rolling(window=period, min_periods=min_required).max()
            )

            # 最低價
            low_col = f"low_{period}d"
            df[low_col] = df.groupby("stock_id")[low_column].transform(
                lambda x: x.rolling(window=period, min_periods=min_required).min()
            )

        return df

    @staticmethod
    def calculate_prior_high(
        df: pd.DataFrame,
        period: int,
        high_column: str = "high_price"
    ) -> pd.DataFrame:
        """計算近 period 交易日「不含當天」的最高價（供突破新高判斷）

        用 rolling(period).max().shift(1)：在第 t 列取得第 t-period ~ t-1 天的最高價，
        排除當天，讓「收盤 > 此值」能真正代表突破過去所有高點。
        """
        df = df.copy().sort_values(["stock_id", "date"])
        col = f"high_{period}d_prior"
        df[col] = df.groupby("stock_id")[high_column].transform(
            lambda x: x.rolling(window=period, min_periods=1).max().shift(1)
        )
        return df

    @staticmethod
    def calculate_returns(
        df: pd.DataFrame,
        periods: list[int],
        price_column: str = "close_price"
    ) -> pd.DataFrame:
        """
        計算股價報酬率

        Args:
            df: 股價 DataFrame
            periods: 週期列表 [20]
            price_column: 價格欄位

        Returns:
            加入報酬率欄位的 DataFrame
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        for period in periods:
            col_name = f"return_{period}d"
            df[col_name] = df.groupby("stock_id")[price_column].transform(
                lambda x: x.pct_change(periods=period)
            )

        return df

    @staticmethod
    def get_second_highest(
        series: pd.Series,
        window: int
    ) -> Optional[float]:
        """
        取得指定區間內的次高價

        Args:
            series: 價格序列
            window: 回看天數

        Returns:
            次高價
        """
        if len(series) < 2:
            return None

        recent = series.tail(window)
        if len(recent) < 2:
            return None

        sorted_vals = recent.sort_values(ascending=False)
        return sorted_vals.iloc[1] if len(sorted_vals) > 1 else None

    @staticmethod
    def calculate_second_high(
        df: pd.DataFrame,
        period: int = 55,
        price_column: str = "close_price"
    ) -> pd.DataFrame:
        """
        計算指定期間的次高價

        用於三線開花的差距比例計算

        Args:
            df: 股價 DataFrame
            period: 回看天數
            price_column: 價格欄位

        Returns:
            加入次高價欄位的 DataFrame
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        def get_second_high(x):
            result = []
            for i in range(len(x)):
                window = x.iloc[max(0, i - period + 1):i + 1]
                if len(window) < 2:
                    result.append(np.nan)
                else:
                    sorted_vals = window.sort_values(ascending=False)
                    result.append(sorted_vals.iloc[1])
            return pd.Series(result, index=x.index)

        col_name = f"second_high_{period}d"
        df[col_name] = df.groupby("stock_id")[price_column].transform(get_second_high)

        return df

    @staticmethod
    def calculate_ma_slope(
        df: pd.DataFrame,
        ma_column: str,
        lookback: int = 20
    ) -> pd.DataFrame:
        """
        計算均線斜率（用於判斷均線是否上升）

        Args:
            df: 包含均線的 DataFrame
            ma_column: 均線欄位名稱 (如 ma200)
            lookback: 回看天數

        Returns:
            加入斜率欄位的 DataFrame
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        col_name = f"{ma_column}_slope_{lookback}d"

        df[col_name] = df.groupby("stock_id")[ma_column].transform(
            lambda x: x - x.shift(lookback)
        )

        return df

    @classmethod
    def prepare_vcp_data(
        cls,
        df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        準備 VCP 篩選所需的所有計算欄位

        Args:
            df: 原始股價 DataFrame

        Returns:
            包含所有 VCP 所需欄位的 DataFrame
        """
        logger.info("準備 VCP 計算資料...")

        # 修正零價異常：close_price=0 時用前一個交易日的收盤價補齊
        df = cls.fix_zero_prices(df)

        # 計算均線
        df = cls.calculate_sma(df, [50, 150, 200])

        # 計算 MA200 斜率（20日前比較）
        df = cls.calculate_ma_slope(df, "ma200", lookback=20)

        # 計算 20 日報酬率
        df = cls.calculate_returns(df, [20])

        # 計算 5 日 / 250 日 / 52 週高低點
        # high_5d 與 high_250d 供新高判斷（近5日高==近250日高）；high_260d 供舊驗證欄位相容
        df = cls.calculate_high_low(df, [5, 250, 260])

        logger.info("VCP 計算資料準備完成")
        return df

    @classmethod
    def prepare_sanxian_data(
        cls,
        df: pd.DataFrame
    ) -> pd.DataFrame:
        """
        準備三線開花篩選所需的所有計算欄位

        Args:
            df: 原始股價 DataFrame

        Returns:
            包含所有三線開花所需欄位的 DataFrame
        """
        logger.info("準備三線開花計算資料...")

        # 修正零價異常：close_price=0 時用前一個交易日的收盤價補齊
        df = cls.fix_zero_prices(df)

        # 計算均線 (8, 21, 55)
        df = cls.calculate_sma(df, [8, 21, 55])

        # 計算 55 日收盤價高點（注意：三線開花是用收盤價創新高，不是最高價）
        df = cls.calculate_close_high(df, periods=[55])

        # 計算 55 日次高價
        df = cls.calculate_second_high(df, period=55)

        # 計算 20 日報酬率（供前端顯示，與 VCP 一致）
        df = cls.calculate_returns(df, [20])

        logger.info("三線開花計算資料準備完成")
        return df

    @staticmethod
    def calculate_close_high(
        df: pd.DataFrame,
        periods: list[int],
        price_column: str = "close_price"
    ) -> pd.DataFrame:
        """
        計算指定期間的收盤價最高值

        用於三線開花「收盤價創新高」判斷

        Args:
            df: 股價 DataFrame
            periods: 週期列表
            price_column: 價格欄位（預設收盤價）

        Returns:
            加入收盤價高點欄位的 DataFrame
        """
        if df.empty:
            return df

        df = df.copy()
        df = df.sort_values(["stock_id", "date"])

        for period in periods:
            col_name = f"high_{period}d"
            # 至少需要 period/2 天資料才算有效，避免新上市股票誤判
            min_required = max(period // 2, 1)
            df[col_name] = df.groupby("stock_id")[price_column].transform(
                lambda x: x.rolling(window=period, min_periods=min_required).max()
            )

        return df
