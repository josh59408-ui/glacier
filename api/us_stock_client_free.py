"""
美股 API 客戶端 - 免費版實作
使用 yfinance + NASDAQ FTP 作為資料來源

特點：
- 完全免費
- yfinance 無官方限制（但過於頻繁可能被暫時封鎖）
- NASDAQ FTP 提供完整股票清單
"""
from __future__ import annotations

import io
import time
import urllib.request
from datetime import date, timedelta
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import yfinance as yf
from loguru import logger

from api.us_stock_client import USStockClientBase
from config.us_settings import (
    US_BATCH_SIZE,
    US_BATCH_INTERVAL,
    US_MAX_WORKERS,
    US_MARKET_INDEX,
    NASDAQ_FTP_URL,
)


class USStockClientFree(USStockClientBase):
    """
    美股免費版 API 客戶端

    資料來源：
    - 股票清單：NASDAQ FTP
    - 股價資料：yfinance
    - 大盤指數：yfinance (^GSPC)
    """

    def __init__(self):
        """初始化客戶端"""
        self._total_requests = 0
        self._error_log = []
        logger.info("美股免費版 API 客戶端初始化完成")

    def get_stock_info(self) -> pd.DataFrame:
        """
        從 NASDAQ FTP 下載完整美股清單

        過濾條件：
        - ETF = "N"（排除 ETF）
        - Test Issue = "N"（排除測試股票）

        Returns:
            包含股票基本資料的 DataFrame
        """
        logger.info("從 NASDAQ FTP 下載股票清單...")

        try:
            # 下載 NASDAQ traded 檔案
            with urllib.request.urlopen(NASDAQ_FTP_URL, timeout=60) as response:
                content = response.read().decode("utf-8")

            # 解析 pipe-delimited 格式
            df = pd.read_csv(
                io.StringIO(content),
                sep="|",
                dtype=str
            )

            # 移除最後一行（通常是檔案建立時間戳記）
            df = df[df["Symbol"].notna() & (df["Symbol"] != "")]

            # 過濾條件
            # ETF = "N" 且 Test Issue = "N"
            if "ETF" in df.columns:
                df = df[df["ETF"] == "N"]
            if "Test Issue" in df.columns:
                df = df[df["Test Issue"] == "N"]

            # 標準化欄位
            result_df = pd.DataFrame({
                "stock_id": df["Symbol"].str.strip(),
                "stock_name": df["Security Name"].str.strip() if "Security Name" in df.columns else "",
                "exchange": df["Listing Exchange"].str.strip() if "Listing Exchange" in df.columns else "",
                "etf_flag": df["ETF"].str.strip() if "ETF" in df.columns else "N",
            })

            # 移除無效代號（包含特殊字元的測試股票）
            # 但保留正常的特殊字元如 BRK.B
            result_df = result_df[result_df["stock_id"].str.match(r"^[A-Z0-9./-]+$", na=False)]

            # 移除過長的代號（通常是權證或特殊商品）
            result_df = result_df[result_df["stock_id"].str.len() <= 10]

            logger.info(f"取得 {len(result_df)} 檔美股股票清單")
            self._total_requests += 1

            return result_df

        except Exception as e:
            logger.error(f"下載 NASDAQ FTP 失敗: {e}")
            self._error_log.append({
                "time": pd.Timestamp.now(),
                "error": str(e),
                "source": "NASDAQ_FTP"
            })
            return pd.DataFrame()

    def get_stock_price(
        self,
        start_date: date,
        end_date: date,
        stock_ids: Optional[list[str]] = None
    ) -> pd.DataFrame:
        """
        使用 yfinance 批次下載美股股價

        Args:
            start_date: 開始日期
            end_date: 結束日期
            stock_ids: 指定股票代號列表

        Returns:
            股價 DataFrame
        """
        if stock_ids is None or len(stock_ids) == 0:
            logger.warning("未指定股票清單")
            return pd.DataFrame()

        logger.info(f"開始下載美股股價: {len(stock_ids)} 檔, {start_date} ~ {end_date}")

        all_data = []
        total_batches = (len(stock_ids) + US_BATCH_SIZE - 1) // US_BATCH_SIZE
        successful_stocks = 0
        failed_stocks = 0

        # 分批下載
        for batch_idx in range(0, len(stock_ids), US_BATCH_SIZE):
            batch = stock_ids[batch_idx:batch_idx + US_BATCH_SIZE]
            current_batch = batch_idx // US_BATCH_SIZE + 1

            logger.info(f"下載批次 {current_batch}/{total_batches} ({len(batch)} 檔)...")

            try:
                # yfinance 批次下載
                # 使用空格分隔的股票代號字串
                tickers_str = " ".join(batch)

                # 日期格式轉換（yfinance 需要字串格式）
                start_str = start_date.isoformat()
                end_str = (end_date + timedelta(days=1)).isoformat()  # yfinance end_date 是 exclusive

                # 下載資料
                data = yf.download(
                    tickers_str,
                    start=start_str,
                    end=end_str,
                    group_by="ticker",
                    progress=False,
                    threads=True,
                    auto_adjust=False,  # 保留原始價格和調整後價格
                )

                if data.empty:
                    logger.warning(f"批次 {current_batch} 無資料")
                    failed_stocks += len(batch)
                    continue

                # 處理單一股票和多股票的不同格式
                if len(batch) == 1:
                    # 單一股票時，columns 不是 MultiIndex
                    ticker = batch[0]
                    # 檢查必要 columns 是否存在（yfinance 對 delisted ticker 可能回空格式）
                    if "Close" not in data.columns:
                        logger.warning(f"批次 {current_batch} 單檔 {ticker} 缺 Close 欄位，跳過")
                        failed_stocks += 1
                        continue
                    # yfinance 1.4+ 把 index.name 設為 None，reset_index 後欄位會變 'index'
                    # 統一補回 'Date'，讓下面的 rename 在 1.0 / 1.4 都能對到
                    data.index.name = "Date"
                    df = data.reset_index()
                    df["stock_id"] = ticker
                    df = df.rename(columns={
                        "Date": "date",
                        "Open": "open",
                        "High": "high",
                        "Low": "low",
                        "Close": "close",
                        "Adj Close": "adj_close",
                        "Volume": "volume",
                    })
                    all_data.append(df)
                    successful_stocks += 1
                else:
                    # 多股票時，columns 是 MultiIndex
                    for ticker in batch:
                        try:
                            if ticker not in data.columns.get_level_values(0):
                                failed_stocks += 1
                                continue

                            ticker_data = data[ticker].copy()
                            if ticker_data.empty:
                                failed_stocks += 1
                                continue

                            # 移除全部為 NaN 的行
                            ticker_data = ticker_data.dropna(how="all")
                            if ticker_data.empty:
                                failed_stocks += 1
                                continue

                            # yfinance 1.4+ 把 index.name 設為 None，reset_index 後欄位會變 'index'
                            # 統一補回 'Date'，讓下面的 rename 在 1.0 / 1.4 都能對到
                            ticker_data.index.name = "Date"
                            df = ticker_data.reset_index()
                            df["stock_id"] = ticker
                            df = df.rename(columns={
                                "Date": "date",
                                "Open": "open",
                                "High": "high",
                                "Low": "low",
                                "Close": "close",
                                "Adj Close": "adj_close",
                                "Volume": "volume",
                            })
                            all_data.append(df)
                            successful_stocks += 1

                        except Exception as e:
                            logger.debug(f"處理 {ticker} 失敗: {e}")
                            failed_stocks += 1

                self._total_requests += 1

            except Exception as e:
                logger.error(f"批次 {current_batch} 下載失敗: {e}")
                self._error_log.append({
                    "time": pd.Timestamp.now(),
                    "error": str(e),
                    "source": "yfinance",
                    "batch": current_batch,
                })
                failed_stocks += len(batch)

            # 批次間隔（避免過於頻繁請求）
            if batch_idx + US_BATCH_SIZE < len(stock_ids):
                time.sleep(US_BATCH_INTERVAL)

        if not all_data:
            logger.warning("無法取得任何股價資料")
            return pd.DataFrame()

        # 合併所有資料
        result_df = pd.concat(all_data, ignore_index=True)

        # 防呆：若所有資料都缺 close 欄位，直接回空
        if "close" not in result_df.columns:
            logger.warning("合併後資料無 close 欄位，回傳空 DataFrame")
            return pd.DataFrame()

        # 標準化日期格式
        result_df["date"] = pd.to_datetime(result_df["date"]).dt.date

        # 移除無效資料
        result_df = result_df.dropna(subset=["close"])

        logger.info(
            f"美股股價下載完成: {len(result_df)} 筆, "
            f"成功 {successful_stocks} 檔, 失敗 {failed_stocks} 檔"
        )

        return result_df

    def get_market_index(
        self,
        start_date: date,
        end_date: Optional[date] = None
    ) -> pd.DataFrame:
        """
        使用 yfinance 取得美股大盤指數（S&P 500）

        Args:
            start_date: 開始日期
            end_date: 結束日期

        Returns:
            大盤指數 DataFrame
        """
        end_date = end_date or start_date

        logger.info(f"取得美股大盤指數: {start_date} ~ {end_date}")

        try:
            # S&P 500 指數
            sp500 = yf.Ticker(US_MARKET_INDEX)
            sp500_data = sp500.history(
                start=start_date.isoformat(),
                end=(end_date + timedelta(days=1)).isoformat()
            )

            if sp500_data.empty:
                logger.warning("無法取得 S&P 500 指數資料")
                return pd.DataFrame()

            result_df = pd.DataFrame({
                "date": sp500_data.index.date,
                "sp500": sp500_data["Close"].values,
            })

            # 嘗試取得其他指數（可選）
            try:
                # 道瓊指數
                dji = yf.Ticker("^DJI")
                dji_data = dji.history(
                    start=start_date.isoformat(),
                    end=(end_date + timedelta(days=1)).isoformat()
                )
                if not dji_data.empty:
                    result_df["dow_jones"] = dji_data["Close"].values[:len(result_df)]
            except Exception:
                pass

            try:
                # NASDAQ 指數
                nasdaq = yf.Ticker("^IXIC")
                nasdaq_data = nasdaq.history(
                    start=start_date.isoformat(),
                    end=(end_date + timedelta(days=1)).isoformat()
                )
                if not nasdaq_data.empty:
                    result_df["nasdaq"] = nasdaq_data["Close"].values[:len(result_df)]
            except Exception:
                pass

            self._total_requests += 1
            logger.info(f"取得 {len(result_df)} 筆大盤指數資料")

            return result_df

        except Exception as e:
            logger.error(f"取得大盤指數失敗: {e}")
            self._error_log.append({
                "time": pd.Timestamp.now(),
                "error": str(e),
                "source": "yfinance_index",
            })
            return pd.DataFrame()

    def get_stock_sector_industry(
        self,
        stock_ids: list[str],
        batch_size: int = 50,
        max_workers: int = 5
    ) -> pd.DataFrame:
        """
        使用 yfinance 批次取得股票的 sector/industry 資訊

        Args:
            stock_ids: 股票代號列表
            batch_size: 每批處理數量
            max_workers: 平行 worker 數

        Returns:
            包含 stock_id, sector, industry 的 DataFrame
        """
        logger.info(f"開始取得美股產業分類: {len(stock_ids)} 檔")

        results = []
        failed = 0

        def fetch_info(ticker_id: str) -> dict:
            """取得單一股票的 sector/industry"""
            try:
                ticker = yf.Ticker(ticker_id)
                info = ticker.info
                return {
                    "stock_id": ticker_id,
                    "sector": info.get("sector", ""),
                    "industry": info.get("industry", ""),
                }
            except Exception:
                return {"stock_id": ticker_id, "sector": "", "industry": ""}

        total_batches = (len(stock_ids) + batch_size - 1) // batch_size

        for batch_idx in range(0, len(stock_ids), batch_size):
            batch = stock_ids[batch_idx:batch_idx + batch_size]
            current_batch = batch_idx // batch_size + 1

            if current_batch % 10 == 1 or current_batch == total_batches:
                logger.info(f"取得產業分類 {current_batch}/{total_batches} ({len(batch)} 檔)...")

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(fetch_info, sid): sid for sid in batch}
                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=30)
                        results.append(result)
                        if not result.get("sector"):
                            failed += 1
                    except Exception:
                        failed += 1
                        results.append({
                            "stock_id": futures[future],
                            "sector": "",
                            "industry": ""
                        })

            # 批次間隔
            if batch_idx + batch_size < len(stock_ids):
                time.sleep(3)

            self._total_requests += 1

        if not results:
            return pd.DataFrame()

        result_df = pd.DataFrame(results)
        success = len(result_df[result_df["sector"] != ""])
        logger.info(f"美股產業分類取得完成: 成功 {success} 檔, 無資料 {failed} 檔")

        return result_df

    def health_check(self) -> bool:
        """
        檢查 API 連線狀態

        透過嘗試取得 S&P 500 當日資料來驗證
        """
        try:
            sp500 = yf.Ticker(US_MARKET_INDEX)
            info = sp500.info
            return "symbol" in info
        except Exception as e:
            logger.error(f"API 健康檢查失敗: {e}")
            return False

    # 判斷交易日用的指標股。用「多檔」而非單檔：單一個股在正常交易日也可能
    # 0 成交（停牌），只有「全部都 0 成交」才是全市場休市。
    DEFAULT_REF_STOCKS = ["AAPL", "MSFT", "NVDA", "JPM", "KO"]

    def get_latest_trading_date(
        self,
        ref_stocks: Optional[list[str]] = None,
        lookback_days: int = 10,
    ) -> Optional[date]:
        """查詢 yfinance 實際有交易的最新交易日（多檔指標股投票）。

        判定規則：某日只要**任一檔指標股 Volume > 0**，就算交易日；
        **全部指標股都 0 成交**才判定為休市日。

        yfinance 會為休市日捏造資料（台股 2026-07-10 颱風休市卻有 OHLC，
        破綻是 Volume=0），美股同樣不能無條件採信。但也不能只看單一檔——
        個股停牌時 Volume 也會是 0，會把真實交易日誤殺。故用多檔投票。

        真實交易日即使 Close 還是 NaN（盤中/未結算），Volume 仍然 > 0。

        Args:
            ref_stocks: 指標股清單（預設大型股）
            lookback_days: 往前查幾個日曆天（需涵蓋連假）

        Returns:
            最新「有實際成交」的交易日；查不到回傳 None（由呼叫端 fallback）
        """
        import yfinance as yf

        stocks = ref_stocks or self.DEFAULT_REF_STOCKS
        start = date.today() - timedelta(days=lookback_days)
        end = date.today() + timedelta(days=1)

        traded_dates: set[date] = set()
        ok_count = 0

        for ticker in stocks:
            try:
                hist = yf.Ticker(ticker).history(
                    start=start, end=end, auto_adjust=False
                )
            except Exception as e:
                logger.warning(f"yfinance 指標股 {ticker} 查詢失敗: {e}")
                continue

            if hist is None or hist.empty or "Volume" not in hist.columns:
                continue

            ok_count += 1
            traded = hist[hist["Volume"].fillna(0) > 0]
            for ts in traded.index:
                traded_dates.add(ts.date() if hasattr(ts, "date") else ts)

        if not ok_count:
            logger.warning("yfinance 所有指標股都查不到資料")
            return None
        if not traded_dates:
            logger.warning("yfinance 指標股近期都沒有成交紀錄（無法判斷交易日）")
            return None

        latest = max(traded_dates)
        logger.info(
            f"美股最新交易日 = {latest}"
            f"（{ok_count}/{len(stocks)} 檔指標股可用，已排除 0 成交的休市日）"
        )
        return latest

    def get_stats(self) -> dict:
        """取得 API 使用統計"""
        return {
            "total_requests": self._total_requests,
            "error_count": len(self._error_log),
            "data_provider": "free (yfinance + NASDAQ FTP)",
        }

    def get_error_log(self) -> list[dict]:
        """取得錯誤日誌"""
        return self._error_log
