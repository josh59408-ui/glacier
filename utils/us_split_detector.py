"""
美股分割/合股偵測模組

偵測邏輯：
比對資料庫中前一交易日的 close_price 與 yfinance 最新下載的同一天價格。
當 Yahoo Finance 因為股票分割而回溯修改歷史價格時，
DB 中的舊值會與 yfinance 的新值產生差異。
差異超過閾值（1%）的股票需要重新下載完整歷史。
"""
from loguru import logger


class USSplitDetector:
    """美股分割/合股偵測器"""

    # 價格差異閾值：超過 1% 視為有調整
    PRICE_DIFF_THRESHOLD = 0.01

    @staticmethod
    def detect_adjusted_stocks(
        db_prices: dict[str, float],
        fresh_prices: dict[str, float],
    ) -> list[str]:
        """
        比對 DB 中前一交易日的 close_price 與 yfinance 最新值，
        回傳有差異的股票代號列表。

        Args:
            db_prices: {stock_id: close_price} 從資料庫取得的前一交易日收盤價
            fresh_prices: {stock_id: close_price} 從 yfinance 重新下載的同一天收盤價

        Returns:
            需要重新下載歷史的股票代號列表
        """
        adjusted_stocks = []

        for stock_id, db_close in db_prices.items():
            fresh_close = fresh_prices.get(stock_id)

            if fresh_close is None or fresh_close == 0:
                continue

            if db_close is None or db_close == 0:
                continue

            # 計算差異比例
            diff_ratio = abs(db_close - fresh_close) / fresh_close

            if diff_ratio > USSplitDetector.PRICE_DIFF_THRESHOLD:
                logger.warning(
                    f"偵測到價格調整: {stock_id} "
                    f"DB={db_close:.4f}, Fresh={fresh_close:.4f}, "
                    f"差異={diff_ratio:.2%}"
                )
                adjusted_stocks.append(stock_id)

        return adjusted_stocks
