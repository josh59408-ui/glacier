"""美股量大強漲篩選器（獨立類型）

篩選邏輯（2 條件 AND，其他一律不看）：
1. 近 lookback 交易日（含當日）波動範圍 = (期間最高 high − 期間最低 low) / 期間最低 low < range_threshold
2. 當日成交量 > 前 lookback 交易日（不含當日）平均成交量 × volume_multiple

與 vcp / sanxian 完全獨立，不比較新舊。
"""
from datetime import date
from typing import Optional

import pandas as pd
from loguru import logger


class USVolumeSurgeFilter:
    """美股量大強漲：窄幅整理（波動小）+ 當日爆量"""

    def __init__(
        self,
        lookback: int = 20,
        range_threshold: float = 0.25,
        volume_multiple: float = 3.0,
        day_change_threshold: float = 0.03,
    ):
        """
        Args:
            lookback: 回看交易日數（波動範圍與均量都用「前 lookback 日、不含當日」）
            range_threshold: 波動範圍上限（0.25 = 25%）
            volume_multiple: 成交量倍數門檻（3.0 = 3 倍）
            day_change_threshold: 當日漲幅下限（0.03 = 今日收盤較前一交易日漲超過 3%）
        """
        self.lookback = lookback
        self.range_threshold = range_threshold
        self.volume_multiple = volume_multiple
        self.day_change_threshold = day_change_threshold

    def filter(
        self,
        price_df: pd.DataFrame,
        target_date: Optional[date] = None,
    ) -> pd.DataFrame:
        """執行量大強漲篩選

        Args:
            price_df: 股價 DataFrame，需含 stock_id, date,
                      high_price, low_price, close_price, volume
                      （需涵蓋 target_date 前 lookback 個交易日以上的歷史）
            target_date: 目標日期（預設為資料最新日期）

        Returns:
            DataFrame，含 stock_id, date, close_price, return_20d,
            vol_range, volume_ratio
        """
        if price_df.empty:
            logger.warning("美股量大強漲：輸入資料為空")
            return pd.DataFrame()

        df = price_df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        if target_date is None:
            target_date = df["date"].max()

        df = df[df["date"] <= target_date]
        results = []

        for stock_id, g in df.groupby("stock_id"):
            g = g.sort_values("date")
            # 需要當日 + 前 lookback 日，共 lookback+1 筆
            if len(g) < self.lookback + 1:
                continue

            # 前 lookback 日（不含當日）：波動範圍與均量都看這段，判斷「當日之前有沒有窄幅整理」
            prev = g.iloc[-(self.lookback + 1):-1]
            low_min = prev["low_price"].min()
            high_max = prev["high_price"].max()
            if pd.isna(low_min) or low_min <= 0:
                continue

            vol_range = (high_max - low_min) / high_max  # 條件 1：波動範圍（前 lookback 日，不含當日；分母用最高）

            avg_vol = prev["volume"].mean()
            today_vol = g.iloc[-1]["volume"]
            if not avg_vol or avg_vol <= 0 or pd.isna(today_vol):
                continue

            volume_ratio = today_vol / avg_vol  # 條件 2：成交量倍數

            # 條件 3：當日漲幅（今日收盤 vs 前一交易日收盤）> 門檻
            today_close = float(g.iloc[-1]["close_price"])
            prev_close = g.iloc[-2]["close_price"]
            day_change = (
                (today_close - prev_close) / prev_close
                if prev_close and prev_close > 0 else 0.0
            )

            if (vol_range < self.range_threshold
                    and volume_ratio > self.volume_multiple
                    and day_change > self.day_change_threshold):
                # 附帶算 20 日漲幅供前端顯示（非篩選條件）
                ret_20d = None
                if len(g) >= self.lookback + 1:
                    base_close = g.iloc[-(self.lookback + 1)]["close_price"]
                    if base_close and base_close > 0:
                        ret_20d = float(g.iloc[-1]["close_price"] / base_close - 1)

                results.append({
                    "stock_id": stock_id,
                    "date": target_date,
                    "close_price": today_close,
                    "return_20d": ret_20d,
                    "vol_range": round(float(vol_range), 4),
                    "volume_ratio": round(float(volume_ratio), 2),
                    "day_change": round(float(day_change), 4),
                })

        result_df = pd.DataFrame(results)
        logger.info(f"美股量大強漲篩選完成: {len(result_df)} 檔")
        return result_df
