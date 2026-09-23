"""
FinMind API 客戶端
"""
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import requests
import yfinance as yf
from loguru import logger

from config.settings import (
    FINMIND_API_URL,
    FINMIND_TOKEN,
    API_CALLS_PER_HOUR,
    RETRY_CONFIG,
)
from api.rate_limiter import RateLimiter, RetryHandler


class FinMindError(Exception):
    """FinMind API 錯誤"""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class FinMindClient:
    """
    FinMind API 客戶端

    封裝 FinMind API 呼叫，包含:
    - 自動限流
    - 錯誤重試
    - 資料轉換
    """

    def __init__(
        self,
        token: Optional[str] = None,
        calls_per_hour: Optional[int] = None
    ):
        """
        初始化客戶端

        Args:
            token: FinMind API Token
            calls_per_hour: 每小時呼叫限制
        """
        self.token = token or FINMIND_TOKEN
        self.api_url = FINMIND_API_URL

        # 初始化限流器
        self.rate_limiter = RateLimiter(
            calls_per_hour=calls_per_hour or API_CALLS_PER_HOUR
        )

        # 初始化重試處理器
        self.retry_handler = RetryHandler(
            max_retries=RETRY_CONFIG["max_retries"],
            retry_intervals=RETRY_CONFIG["retry_intervals"]
        )

        # 請求計數
        self._request_count = 0
        self._error_log: list[dict] = []

        logger.info("FinMindClient 初始化完成")

    def _make_request(self, params: dict) -> dict:
        """
        發送 API 請求（含限流與重試）

        Args:
            params: 請求參數

        Returns:
            API 回應資料

        Raises:
            FinMindError: API 錯誤
        """
        if self.token:
            params["token"] = self.token

        retry_count = 0
        last_error = None

        while True:
            # 等待限流
            with self.rate_limiter:
                try:
                    self._request_count += 1

                    response = requests.get(
                        self.api_url,
                        params=params,
                        timeout=30
                    )

                    # 成功
                    if response.status_code == 200:
                        data = response.json()

                        # 檢查 API 層級錯誤
                        if data.get("status") != 200:
                            raise FinMindError(
                                f"API Error: {data.get('msg', 'Unknown')}",
                                status_code=data.get("status")
                            )

                        return data

                    # 判斷是否重試
                    if self.retry_handler.should_retry(
                        response.status_code, retry_count
                    ):
                        self._log_error(
                            params, response.status_code, retry_count
                        )
                        self.retry_handler.wait_for_retry(retry_count)
                        retry_count += 1
                        continue

                    # 不重試的錯誤
                    self._log_error(params, response.status_code, retry_count)
                    raise FinMindError(
                        f"HTTP {response.status_code}: {response.text}",
                        status_code=response.status_code
                    )

                except requests.RequestException as e:
                    last_error = e

                    if retry_count < self.retry_handler.max_retries:
                        logger.warning(f"請求異常: {e}, 準備重試...")
                        self.retry_handler.wait_for_retry(retry_count)
                        retry_count += 1
                        continue

                    raise FinMindError(f"請求失敗: {e}")

    def _log_error(
        self,
        params: dict,
        status_code: int,
        retry_count: int
    ):
        """記錄錯誤"""
        error_entry = {
            "time": datetime.now().isoformat(),
            "params": {k: v for k, v in params.items() if k != "token"},
            "status_code": status_code,
            "retry_count": retry_count,
        }
        self._error_log.append(error_entry)

        logger.error(
            f"API 錯誤 - 狀態碼: {status_code}, "
            f"重試次數: {retry_count}, "
            f"參數: {error_entry['params']}"
        )

    def _fix_zero_prices_with_yfinance(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        使用 yfinance 補齊收盤價為 0 的異常資料

        只處理正規股票（4-5位數字代號），跳過權證（6位數）等商品

        Args:
            df: 含有異常資料的 DataFrame

        Returns:
            修正後的 DataFrame
        """
        import re

        # 找出需要修正的股票和日期
        zero_rows = df[df["close"] == 0].copy()
        if zero_rows.empty:
            return df

        # 按股票分組處理
        for stock_id in zero_rows["stock_id"].unique():
            # 只處理正規股票（4-5位數字代號），跳過權證等商品
            if not re.match(r"^\d{4,5}$", str(stock_id)):
                logger.debug(f"跳過非正規股票 {stock_id} 的 yfinance 補齊")
                continue
            stock_zeros = zero_rows[zero_rows["stock_id"] == stock_id]
            dates_to_fix = stock_zeros["date"].tolist()

            try:
                # 使用 yfinance 取得正確價格
                ticker_symbol = f"{stock_id}.TW"
                ticker = yf.Ticker(ticker_symbol)

                # 取得日期範圍的資料
                min_date = min(dates_to_fix)
                max_date = max(dates_to_fix)
                # yfinance 的 end 是 exclusive，所以加一天
                yf_data = ticker.history(
                    start=min_date,
                    end=max_date + timedelta(days=1)
                )

                if yf_data.empty:
                    # 如果主板沒有，試試上櫃
                    ticker_symbol = f"{stock_id}.TWO"
                    ticker = yf.Ticker(ticker_symbol)
                    yf_data = ticker.history(
                        start=min_date,
                        end=max_date + timedelta(days=1)
                    )

                if yf_data.empty:
                    logger.warning(f"yfinance 也無法取得 {stock_id} 的資料，跳過")
                    continue

                # 逐日補齊
                for fix_date in dates_to_fix:
                    yf_row = yf_data[yf_data.index.date == fix_date]
                    if not yf_row.empty:
                        row = yf_row.iloc[0]
                        mask = (df["stock_id"] == stock_id) & (df["date"] == fix_date)
                        df.loc[mask, "open"] = row["Open"]
                        df.loc[mask, "high"] = row["High"]
                        df.loc[mask, "low"] = row["Low"]
                        df.loc[mask, "close"] = row["Close"]
                        logger.info(f"已用 yfinance 補齊 {stock_id} {fix_date} 的價格: {row['Close']}")
                    else:
                        logger.warning(f"yfinance 無 {stock_id} {fix_date} 的資料")

            except Exception as e:
                logger.error(f"yfinance 補齊 {stock_id} 失敗: {e}")

        # 確認是否還有未修正的資料
        remaining_zeros = (df["close"] == 0).sum()
        if remaining_zeros > 0:
            logger.warning(f"仍有 {remaining_zeros} 筆資料無法補齊")

        return df

    def get_stock_info(self) -> pd.DataFrame:
        """
        取得台股股票清單

        Returns:
            DataFrame with columns:
            - stock_id: 股票代號
            - stock_name: 股票名稱
            - industry_category: 產業分類
            - type: 股票類型
        """
        logger.info("取得台股股票清單...")

        params = {"dataset": "TaiwanStockInfo"}
        data = self._make_request(params)

        # 安全取得 data 欄位
        raw_data = data.get("data", [])
        if not raw_data:
            logger.warning("API 回應無資料")
            return pd.DataFrame()

        df = pd.DataFrame(raw_data)

        # 檢查必要欄位是否存在
        required_cols = ["stock_id", "stock_name", "type"]
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            logger.warning(f"股票清單資料缺少欄位: {missing_cols}")
            return pd.DataFrame()

        # 過濾只保留上市/上櫃股票
        df = df[df["type"].isin(["twse", "tpex"])]

        # SPEC: 須扣除ETF等其它商品
        # 台股 ETF 通常股票代號為 00xx, 006xxx 等格式（以 0 開頭且長度 >= 4）
        # 排除以 0 開頭且長度大於等於 4 的代號（如 0050, 006208）
        # 但保留一般股票（如 1101 大同等開頭為 1-9 的代號）
        original_count = len(df)
        df = df[~df["stock_id"].str.match(r"^0\d{3,}")]
        filtered_count = original_count - len(df)
        if filtered_count > 0:
            logger.info(f"已過濾 {filtered_count} 檔 ETF/其他商品")

        # 過濾指數資料（industry_category 為 'Index' 或 '大盤'）
        # 過濾權證（industry_category 為 '所有證券'，通常以 7 開頭的 6 位數）
        # 以及非數字的代號（如 ElectricMachinery, TPEx 等）
        before_index_filter = len(df)
        df = df[~df["industry_category"].isin(["Index", "大盤", "所有證券"])]
        df = df[df["stock_id"].str.match(r"^\d{4,6}$")]  # 只保留 4-6 位純數字代號
        index_filtered = before_index_filter - len(df)
        if index_filtered > 0:
            logger.info(f"已過濾 {index_filtered} 檔指數/權證/非股票資料")

        # 過濾已下市股票
        # date 欄位表示資料更新日期，已下市股票的 date 會停留在下市日期
        # 只保留 date 為最近日期的股票（仍在交易中）
        if "date" in df.columns:
            before_delist_filter = len(df)
            # 轉換日期格式
            df["date"] = pd.to_datetime(df["date"])
            # 找出最新的日期
            latest_date = df["date"].max()
            # 只保留最新日期的股票（正常交易中的股票）
            df = df[df["date"] == latest_date]
            delist_filtered = before_delist_filter - len(df)
            if delist_filtered > 0:
                logger.info(f"已過濾 {delist_filtered} 檔已下市/停牌股票")

        # 處理多重產業分類（同一股票可能有多個 industry_category）
        # 例如：2330 台積電 有「半導體業」和「電子工業」兩個分類
        before_dedup = len(df)

        # 按 stock_id 分組，取得所有產業分類
        industry_groups = df.groupby("stock_id")["industry_category"].apply(list).reset_index()
        industry_groups.columns = ["stock_id", "industry_list"]

        # 去重後保留第一筆的其他欄位
        df = df.drop_duplicates(subset=["stock_id"], keep="first")

        # 合併產業分類列表
        df = df.merge(industry_groups, on="stock_id", how="left")

        # 定義產業優先級（數字越大越廣泛，reverse=True 時排前面）
        # 較廣泛的產業（如「電子工業」）排在前面，較具體的（如「半導體業」）排在後面
        INDUSTRY_PRIORITY = {
            # 電子相關
            "半導體業": 1,
            "電腦及週邊設備業": 2,
            "光電業": 3,
            "通信網路業": 4,
            "電子零組件業": 5,
            "電子通路業": 6,
            "資訊服務業": 7,
            "其他電子業": 8,
            "電子工業": 9,  # 較廣泛的分類
            # 生技醫療相關（數字越大越廣泛，reverse=True 時排前面）
            "化學工業": 3,  # 較廣泛的分類
            "化學生技醫療": 2,
            "生技醫療業": 1,
            # 其他（預設優先級）
        }

        # 非產業分類的標籤（板塊類型等），需要過濾掉
        NON_INDUSTRY_LABELS = {"創新板股票"}

        def sort_industries(industry_list):
            """排序產業分類，較廣泛的排在前面，並過濾非產業標籤"""
            if not industry_list:
                return industry_list
            # 過濾掉非產業標籤
            filtered = [x for x in industry_list if x not in NON_INDUSTRY_LABELS]
            if not filtered:
                return industry_list  # 如果全被過濾掉，保留原始
            if len(filtered) <= 1:
                return filtered
            return sorted(filtered, key=lambda x: INDUSTRY_PRIORITY.get(x, 50), reverse=True)

        # 設定產業分類1和產業分類2（排序後）
        df["industry_list"] = df["industry_list"].apply(sort_industries)
        df["industry_category"] = df["industry_list"].apply(lambda x: x[0] if x else "-")
        df["industry_category2"] = df["industry_list"].apply(lambda x: x[1] if len(x) > 1 else "-")

        # 移除暫時欄位
        df = df.drop(columns=["industry_list"])

        dedup_count = before_dedup - len(df)
        if dedup_count > 0:
            logger.info(f"已合併 {dedup_count} 筆重複資料（多重產業分類）")

        logger.info(f"取得 {len(df)} 檔股票資訊")
        return df

    def get_stock_price(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        stock_ids: Optional[list[str]] = None,
        market_types: Optional[dict[str, str]] = None,
        retry_count: int = 3,
    ) -> pd.DataFrame:
        """
        取得股票每日股價

        Args:
            start_date: 開始日期
            end_date: 結束日期（預設為 start_date）
            stock_ids: 股票代號列表（可選，不指定則取得全部）
            market_types: 市場類型字典（忽略，FinMind 不需要）
            retry_count: 重試次數（已由 RetryHandler 處理）

        Returns:
            DataFrame with columns:
            - date: 日期
            - stock_id: 股票代號
            - open: 開盤價
            - high: 最高價
            - low: 最低價
            - close: 收盤價
            - volume: 成交量
        """
        if end_date is None:
            end_date = start_date

        # 驗證日期範圍
        if start_date > end_date:
            logger.warning(f"日期範圍錯誤: start_date ({start_date}) > end_date ({end_date})，自動交換")
            start_date, end_date = end_date, start_date

        stock_count = len(stock_ids) if stock_ids else "全部"
        logger.info(f"取得股價資料: {start_date} ~ {end_date}, 共 {stock_count} 檔")

        # 使用未調整股價（TaiwanStockPrice）
        # 券商的均線圖是用未調整收盤價計算的，不是還原權息價格
        params = {
            "dataset": "TaiwanStockPrice",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
        }

        data = self._make_request(params)

        # 安全取得 data 欄位
        raw_data = data.get("data", [])
        if not raw_data:
            logger.warning("無股價資料")
            return pd.DataFrame()

        df = pd.DataFrame(raw_data)

        # 檢查必要欄位是否存在
        required_cols = ["stock_id", "date", "open", "max", "min", "close"]
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            logger.warning(f"股價資料缺少欄位: {missing_cols}")
            return pd.DataFrame()

        # 重新命名欄位
        df = df.rename(columns={
            "Trading_Volume": "volume",
            "max": "high",
            "min": "low",
        })

        # 轉換日期
        df["date"] = pd.to_datetime(df["date"]).dt.date

        # 修正異常資料：收盤價為 0 的資料（可能是停牌或資料錯誤）
        # FinMind 偶爾會返回價格全為 0 的資料，會影響均線計算
        # 使用 yfinance 來取得正確的價格補齊
        zero_close_mask = df["close"] == 0
        zero_count = zero_close_mask.sum()
        if zero_count > 0:
            logger.warning(f"發現 {zero_count} 筆異常資料（收盤價為 0），嘗試用 yfinance 補齊...")
            df = self._fix_zero_prices_with_yfinance(df)

        # 如果有指定股票清單，過濾結果
        if stock_ids:
            df = df[df["stock_id"].isin(stock_ids)]

        logger.info(f"取得 {len(df)} 筆股價資料")
        return df

    def get_latest_trading_date(
        self,
        ref_stock: str = "2330",
        lookback_days: int = 14,
    ) -> Optional[date]:
        """查詢 FinMind 實際擁有資料的最新交易日（用單一指標股判斷）。

        必須帶 data_id 做「單檔查詢」，不能用 get_stock_price()：
        後者不帶 data_id、一律拉全市場，而台股全市場「單日就約 4 萬筆」，
        已逼近 FinMind 單次查詢上限。一旦查超過一天就會被靜默截斷、
        只回「區間第一天」且仍回報 success——2026-07-14 就因此把最新交易日
        誤判成 6/30，害當天用兩週前的資料跑篩選。
        單檔查詢只有 10 筆左右，不會觸發截斷。

        Args:
            ref_stock: 指標股代號（預設台積電 2330，每個交易日必有成交）
            lookback_days: 往前查幾個日曆天（需涵蓋連假）

        Returns:
            FinMind 有資料的最新交易日；查不到回傳 None（由呼叫端 fallback）
        """
        start_date = date.today() - timedelta(days=lookback_days)
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": ref_stock,          # ← 關鍵：單檔查詢，避免全市場截斷
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": date.today().strftime("%Y-%m-%d"),
        }

        try:
            data = self._make_request(params)
        except FinMindError as e:
            logger.warning(f"FinMind 查詢最新交易日失敗（{ref_stock}）: {e}")
            return None

        raw_data = data.get("data", [])
        if not raw_data:
            logger.warning(f"FinMind 查無 {ref_stock} 近 {lookback_days} 天的資料")
            return None

        dates = [row["date"] for row in raw_data if row.get("date")]
        if not dates:
            return None

        latest = pd.to_datetime(max(dates)).date()
        logger.info(f"FinMind 最新交易日（{ref_stock}）= {latest}")
        return latest

    def get_market_index(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        retry_count: int = 3,
    ) -> pd.DataFrame:
        """
        取得大盤指數 (加權指數)

        Args:
            start_date: 開始日期
            end_date: 結束日期
            retry_count: 重試次數（已由 RetryHandler 處理）

        Returns:
            DataFrame with columns:
            - date: 日期
            - taiex: 加權指數
        """
        if end_date is None:
            end_date = start_date

        # 驗證日期範圍
        if start_date > end_date:
            logger.warning(f"日期範圍錯誤: start_date ({start_date}) > end_date ({end_date})，自動交換")
            start_date, end_date = end_date, start_date

        logger.info(f"取得大盤指數: {start_date} ~ {end_date}")

        # 使用 TaiwanStockPrice 取得 TAIEX 指數
        # 備選方案：TaiwanStockTotalReturnIndex
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": "TAIEX",
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
        }

        try:
            data = self._make_request(params)
        except FinMindError:
            # 如果 TaiwanStockPrice 沒有 TAIEX，嘗試用報酬指數
            logger.info("嘗試使用 TaiwanStockTotalReturnIndex...")
            params = {
                "dataset": "TaiwanStockTotalReturnIndex",
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date": end_date.strftime("%Y-%m-%d"),
            }
            data = self._make_request(params)

        # 安全取得 data 欄位
        raw_data = data.get("data", [])
        if not raw_data:
            logger.warning("無大盤指數資料")
            return pd.DataFrame()

        df = pd.DataFrame(raw_data)

        # 根據資料來源處理欄位
        if "close" in df.columns:
            # TaiwanStockPrice 格式
            df = df.rename(columns={"close": "taiex"})
        elif "price" in df.columns:
            # TaiwanStockTotalReturnIndex 格式
            df = df[df["stock_id"] == "TAIEX"]
            df = df.rename(columns={"price": "taiex"})

        if df.empty or "taiex" not in df.columns:
            logger.warning("無 TAIEX 指數資料")
            return pd.DataFrame()

        # 轉換日期
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[["date", "taiex"]]

        logger.info(f"取得 {len(df)} 筆大盤指數")
        return df

    def get_stats(self) -> dict:
        """取得客戶端統計資訊"""
        return {
            "total_requests": self._request_count,
            "error_count": len(self._error_log),
            "rate_limiter": self.rate_limiter.get_stats(),
        }

    def get_error_log(self) -> list[dict]:
        """取得錯誤日誌"""
        return self._error_log.copy()
