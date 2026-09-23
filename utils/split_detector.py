"""
台股除權息/減資偵測模組

偵測邏輯：
比對 DB 中前一交易日的 close_price（未調整）
與 FinMind TaiwanStockPriceAdj（還原權息價）的同日 close。
若兩者差異超過閾值，表示該股票歷經除權息或減資，
需要重新下載完整歷史股價以確保均線計算正確。

閾值設為 1%：
- 配息/配股/減資都會造成還原價與原始價的差異
- 正常情況下兩者應該幾乎相同（同一天未調整和當天的還原價一致）
- 只有在「新的」除權息事件發生後，還原價才會與 DB 中舊資料不同
"""
import os
from datetime import date as date_type

import requests
from dotenv import load_dotenv
from loguru import logger

load_dotenv()


class SplitDetector:
    """台股除權息/減資偵測器"""

    PRICE_DIFF_THRESHOLD = 0.01

    @staticmethod
    def fetch_adjusted_prices(target_date, token: str = "") -> dict[str, float]:
        """
        從 FinMind 取得指定日期的還原權息收盤價

        Args:
            target_date: 目標日期
            token: FinMind API token

        Returns:
            {stock_id: adjusted_close_price}
        """
        if not token:
            token = os.getenv("FINMIND_TOKEN", "")

        params = {
            "dataset": "TaiwanStockPriceAdj",
            "start_date": target_date.strftime("%Y-%m-%d"),
            "end_date": target_date.strftime("%Y-%m-%d"),
            "token": token,
        }

        try:
            resp = requests.get(
                "https://api.finmindtrade.com/api/v4/data",
                params=params,
                timeout=60,
            )
            data = resp.json()

            if data.get("msg") != "success" or not data.get("data"):
                logger.warning(f"FinMind TaiwanStockPriceAdj 無資料: {data.get('msg', '')}")
                return {}

            return {
                row["stock_id"]: float(row["close"])
                for row in data["data"]
                if row.get("close") is not None
            }

        except Exception as e:
            logger.error(f"取得 FinMind 還原股價失敗: {e}")
            return {}

    @staticmethod
    def fetch_adjusted_history(
        stock_ids: list[str],
        start_date,
        end_date,
        token: str = "",
    ) -> list[dict]:
        """
        從 FinMind 取得指定股票的還原權息歷史股價

        Args:
            stock_ids: 股票代號列表
            start_date: 開始日期
            end_date: 結束日期
            token: FinMind API token

        Returns:
            [{"stock_id", "date", "open", "high", "low", "close", "volume"}, ...]
        """
        if not token:
            token = os.getenv("FINMIND_TOKEN", "")

        all_records = []
        for sid in stock_ids:
            try:
                resp = requests.get(
                    "https://api.finmindtrade.com/api/v4/data",
                    params={
                        "dataset": "TaiwanStockPriceAdj",
                        "data_id": sid,
                        "start_date": start_date.strftime("%Y-%m-%d"),
                        "end_date": end_date.strftime("%Y-%m-%d"),
                        "token": token,
                    },
                    timeout=30,
                )
                data = resp.json()
                if data.get("msg") != "success" or not data.get("data"):
                    continue

                for row in data["data"]:
                    all_records.append({
                        "stock_id": row["stock_id"],
                        "date": date_type.fromisoformat(row["date"]),
                        "open": row.get("open", 0),
                        "high": row.get("max", 0),
                        "low": row.get("min", 0),
                        "close": row.get("close", 0),
                        "volume": row.get("Trading_Volume", 0),
                    })
            except Exception as e:
                logger.warning(f"取得 {sid} 還原股價歷史失敗: {e}")
                continue

        logger.info(f"取得 {len(stock_ids)} 檔股票共 {len(all_records)} 筆還原歷史股價")
        return all_records

    @staticmethod
    def detect_adjusted_stocks(
        db_prices: dict[str, float],
        adj_prices: dict[str, float],
    ) -> list[str]:
        """
        比對 DB 中的 close_price 與 FinMind 還原權息價，
        回傳有差異的股票代號列表。

        Args:
            db_prices: {stock_id: close_price} 從 DB 取得的收盤價（未調整）
            adj_prices: {stock_id: close_price} 從 FinMind 取得的還原權息價

        Returns:
            需要重新下載歷史的股票代號列表
        """
        adjusted_stocks = []

        for stock_id, db_close in db_prices.items():
            adj_close = adj_prices.get(stock_id)

            if adj_close is None or adj_close == 0:
                continue

            if db_close is None or db_close == 0:
                continue

            diff_ratio = abs(db_close - adj_close) / adj_close

            if diff_ratio > SplitDetector.PRICE_DIFF_THRESHOLD:
                logger.warning(
                    f"偵測到價格調整: {stock_id} "
                    f"DB={db_close:.4f}, Adj={adj_close:.4f}, "
                    f"差異={diff_ratio:.2%}"
                )
                adjusted_stocks.append(stock_id)

        return adjusted_stocks
