"""
yfinance API 客戶端
免費批量取得台股資料
"""
import random
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger


class AdaptiveBatchDownloader:
    """
    自適應批次下載器

    根據 API 錯誤率動態調整批次大小，避免限流。
    """

    def __init__(
        self,
        initial_batch_size: int = 100,
        min_batch_size: int = 10,
        max_batch_size: int = 500,
        initial_interval: float = 5.0,
        max_interval: float = 30.0,
    ):
        """
        初始化自適應下載器

        Args:
            initial_batch_size: 初始批次大小
            min_batch_size: 最小批次大小
            max_batch_size: 最大批次大小
            initial_interval: 初始批次間隔（秒）
            max_interval: 最大批次間隔（秒）
        """
        self.batch_size = initial_batch_size
        self.min_batch_size = min_batch_size
        self.max_batch_size = max_batch_size
        self.batch_interval = initial_interval
        self.max_interval = max_interval
        self.error_count = 0
        self.success_count = 0

    def adjust(self, success: bool):
        """
        根據成功/失敗調整批次大小和間隔

        Args:
            success: 本次批次是否成功
        """
        if success:
            self.success_count += 1
            # 連續成功多次後，嘗試增大批次
            if self.success_count >= 5 and self.batch_size < self.max_batch_size:
                old_size = self.batch_size
                self.batch_size = min(self.batch_size * 2, self.max_batch_size)
                if self.batch_size != old_size:
                    logger.info(f"批次大小增至 {self.batch_size} (從 {old_size})")
                    self.success_count = 0  # 重置計數
        else:
            self.error_count += 1
            total = self.success_count + self.error_count
            error_rate = self.error_count / total if total > 0 else 0

            # 錯誤率超過 20% 時，減小批次並增加間隔
            if error_rate > 0.2:
                old_size = self.batch_size
                old_interval = self.batch_interval

                self.batch_size = max(self.batch_size // 2, self.min_batch_size)
                self.batch_interval = min(self.batch_interval * 2, self.max_interval)

                logger.warning(
                    f"錯誤率過高 ({error_rate:.1%}), "
                    f"批次降至 {self.batch_size} (從 {old_size}), "
                    f"間隔 {self.batch_interval}s (從 {old_interval}s)"
                )
                # 重置計數器
                self.error_count = 0
                self.success_count = 0

    def get_batch_size(self) -> int:
        """取得當前批次大小"""
        return self.batch_size

    def get_interval(self, add_jitter: bool = True) -> float:
        """
        取得當前批次間隔

        Args:
            add_jitter: 是否加入隨機延遲

        Returns:
            間隔秒數
        """
        if add_jitter:
            # 加入 ±20% 的隨機延遲，避免規律性請求
            jitter = random.uniform(0.8, 1.2)
            return self.batch_interval * jitter
        return self.batch_interval

    def reset(self):
        """重置計數器"""
        self.error_count = 0
        self.success_count = 0


class YFinanceError(Exception):
    """yfinance API 錯誤"""
    pass


class YFinanceClient:
    """
    yfinance API 客戶端

    特點:
    - 免費無限制
    - 支援批量查詢
    - 台股格式：代號.TW (上市) 或 代號.TWO (上櫃)
    """

    # 台股產業分類對照表
    INDUSTRY_MAP = {
        "Semiconductors": "半導體業",
        "Consumer Electronics": "電子零組件業",
        "Electronic Components": "電子零組件業",
        "Communication Equipment": "通信網路業",
        "Computer Hardware": "電腦及週邊設備業",
        "Software - Application": "資訊服務業",
        "Software - Infrastructure": "資訊服務業",
        "Banks - Regional": "金融保險業",
        "Insurance - Life": "金融保險業",
        "Insurance - Property & Casualty": "金融保險業",
        "Asset Management": "金融保險業",
        "Steel": "鋼鐵工業",
        "Specialty Chemicals": "化學工業",
        "Building Materials": "建材營造業",
        "Auto Parts": "汽車工業",
        "Biotechnology": "生技醫療業",
        "Medical Devices": "生技醫療業",
        "Drug Manufacturers - General": "生技醫療業",
        "Oil & Gas E&P": "油電燃氣業",
        "Utilities - Regulated Electric": "油電燃氣業",
        "Shipping & Ports": "航運業",
        "Airlines": "航運業",
        "Restaurants": "觀光餐旅",
        "Lodging": "觀光餐旅",
        "Entertainment": "文化創意業",
        "Apparel Manufacturing": "紡織纖維",
        "Textile Manufacturing": "紡織纖維",
        "Packaged Foods": "食品工業",
        "Beverages - Non-Alcoholic": "食品工業",
        "Farm Products": "食品工業",
        "Real Estate Services": "建材營造業",
        "REIT - Retail": "建材營造業",
    }

    def __init__(
        self,
        initial_batch_size: int = 100,
        min_batch_size: int = 10,
        max_batch_size: int = 500,
        initial_interval: float = 5.0,
        max_interval: float = 30.0,
    ):
        """
        初始化客戶端

        Args:
            initial_batch_size: 初始批次大小
            min_batch_size: 最小批次大小
            max_batch_size: 最大批次大小
            initial_interval: 初始批次間隔（秒）
            max_interval: 最大批次間隔（秒）
        """
        self._request_count = 0
        self._error_log: list[dict] = []
        self._downloader = AdaptiveBatchDownloader(
            initial_batch_size=initial_batch_size,
            min_batch_size=min_batch_size,
            max_batch_size=max_batch_size,
            initial_interval=initial_interval,
            max_interval=max_interval,
        )
        logger.info(
            f"YFinanceClient 初始化完成 "
            f"(批次: {initial_batch_size}, 間隔: {initial_interval}s)"
        )

    def _to_tw_symbol(self, stock_id: str, market: str = "twse") -> str:
        """
        轉換為 yfinance 台股格式

        Args:
            stock_id: 股票代號（如 2330）
            market: 市場類型 (twse=上市, tpex=上櫃)

        Returns:
            yfinance 格式代號（如 2330.TW 或 2330.TWO）
        """
        suffix = ".TW" if market == "twse" else ".TWO"
        return f"{stock_id}{suffix}"

    def _from_tw_symbol(self, symbol: str) -> str:
        """從 yfinance 格式轉回股票代號"""
        # 注意：必須先處理 .TWO，再處理 .TW，否則 .TWO 會變成 O
        if symbol.endswith(".TWO"):
            return symbol[:-4]
        elif symbol.endswith(".TW"):
            return symbol[:-3]
        return symbol

    def get_stock_info(self) -> pd.DataFrame:
        """
        取得台股股票清單

        注意：yfinance 無法直接取得完整股票清單，
        改為從 TWSE/TPEX 網站取得

        Returns:
            DataFrame with columns:
            - stock_id: 股票代號
            - stock_name: 股票名稱
            - industry_category: 產業分類
            - type: 股票類型 (twse/tpex)
        """
        logger.info("取得台股股票清單...")

        try:
            # 從證交所取得上市股票清單
            twse_url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
            twse_df = self._fetch_stock_list(twse_url, "twse")

            # 從櫃買中心取得上櫃股票清單
            tpex_url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"
            tpex_df = self._fetch_stock_list(tpex_url, "tpex")

            # 合併
            df = pd.concat([twse_df, tpex_df], ignore_index=True)

            # 過濾：只保留純數字 4 碼的股票代號（排除權證、認購/認售證等 6 位數）
            df = df[df["stock_id"].str.match(r"^\d{4}$")]

            # 過濾：排除 ETF（以 00 開頭）
            df = df[~df["stock_id"].str.match(r"^00")]

            # 去除重複
            df = df.drop_duplicates(subset=["stock_id"], keep="first")

            logger.info(f"取得 {len(df)} 檔股票資訊")
            return df

        except Exception as e:
            logger.error(f"取得股票清單失敗: {e}")
            self._log_error("get_stock_info", str(e))
            return pd.DataFrame()

    def _fetch_stock_list(self, url: str, market_type: str) -> pd.DataFrame:
        """從 TWSE 網站取得股票清單"""
        import requests
        import urllib3
        from io import StringIO

        # 停用 SSL 警告（TWSE 憑證有時有問題）
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        try:
            # 使用 requests 手動處理編碼，停用 SSL 驗證
            response = requests.get(url, timeout=30, verify=False)
            # 嘗試不同編碼
            for encoding in ["cp950", "big5-hkscs", "big5", "utf-8"]:
                try:
                    response.encoding = encoding
                    # 使用 StringIO 包裝以避免 FutureWarning
                    dfs = pd.read_html(StringIO(response.text))
                    if dfs:
                        break
                except Exception:
                    continue
            else:
                logger.warning(f"無法解析 {url}")
                return pd.DataFrame()

            if not dfs:
                return pd.DataFrame()

            df = dfs[0]
            # 欄位：有價證券代號及名稱, 國際證券辨識號碼, 上市日, 市場別, 產業別, CFICode, 備註
            df.columns = ["code_name", "isin", "list_date", "market", "industry", "cfi", "note"]

            # 分離代號和名稱（使用 .copy() 避免 SettingWithCopyWarning）
            df = df[df["code_name"].str.contains(r"^\d", na=False)].copy()
            df[["stock_id", "stock_name"]] = df["code_name"].str.split("\u3000", n=1, expand=True)

            # 清理資料
            df["stock_id"] = df["stock_id"].str.strip()
            df["stock_name"] = df["stock_name"].str.strip() if "stock_name" in df.columns else ""
            df["industry_category"] = df["industry"].fillna("-")
            df["type"] = market_type

            return df[["stock_id", "stock_name", "industry_category", "type"]]

        except Exception as e:
            logger.warning(f"從 {url} 取得資料失敗: {e}")
            return pd.DataFrame()

    def get_stock_price(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        stock_ids: Optional[list[str]] = None,
        market_types: Optional[dict[str, str]] = None,
        retry_count: int = 3,
    ) -> pd.DataFrame:
        """
        批量取得股票每日股價（使用自適應批次下載）

        Args:
            start_date: 開始日期
            end_date: 結束日期（預設為今天）
            stock_ids: 股票代號列表
            market_types: 股票代號對應的市場類型 {stock_id: "twse"/"tpex"}
            retry_count: 每批次重試次數

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
            end_date = date.today()

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        if not stock_ids:
            logger.warning("未指定股票代號")
            return pd.DataFrame()

        logger.info(f"取得股價資料: {start_date} ~ {end_date}, 共 {len(stock_ids)} 檔")

        # 預設市場類型
        if market_types is None:
            market_types = {sid: "twse" for sid in stock_ids}

        # 轉換為 yfinance 格式
        symbols = [self._to_tw_symbol(sid, market_types.get(sid, "twse")) for sid in stock_ids]

        all_data = []
        processed = 0

        while processed < len(symbols):
            # 取得當前批次大小（自適應）
            batch_size = self._downloader.get_batch_size()
            batch_symbols = symbols[processed:processed + batch_size]
            batch_str = " ".join(batch_symbols)
            batch_num = processed // batch_size + 1
            total_batches = (len(symbols) + batch_size - 1) // batch_size

            logger.info(
                f"下載批次 {batch_num}/{total_batches}: {len(batch_symbols)} 檔 "
                f"(批次大小: {batch_size}, 間隔: {self._downloader.batch_interval:.1f}s)"
            )

            # 帶重試的下載
            success = False
            for attempt in range(retry_count):
                try:
                    batch_df = yf.download(
                        tickers=batch_str,
                        start=start_date.strftime("%Y-%m-%d"),
                        end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                        progress=False,
                        group_by="ticker",
                        auto_adjust=True,  # 使用調整後收盤價（考慮除權息）
                        threads=True,
                    )

                    if batch_df is not None and not batch_df.empty:
                        batch_data = self._process_batch_data(batch_df, batch_symbols)
                        all_data.extend(batch_data)
                        success = True
                        break
                    else:
                        # 空資料也算成功（可能是非交易日）
                        success = True
                        break

                except Exception as e:
                    error_msg = str(e)
                    self._log_error(f"get_stock_price_batch_{batch_num}", error_msg)

                    if attempt < retry_count - 1:
                        wait_time = self._downloader.get_interval() * (attempt + 1)
                        logger.warning(
                            f"批次 {batch_num} 下載失敗: {error_msg}, "
                            f"等待 {wait_time:.1f} 秒後重試 ({attempt + 1}/{retry_count})"
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"批次 {batch_num} 下載失敗，已達重試上限: {error_msg}")

            # 調整下載器參數
            self._downloader.adjust(success)
            self._request_count += 1
            processed += len(batch_symbols)

            # 批次間延遲（最後一批不延遲）
            if processed < len(symbols):
                interval = self._downloader.get_interval(add_jitter=True)
                logger.debug(f"等待 {interval:.1f} 秒後繼續...")
                time.sleep(interval)

        if not all_data:
            logger.warning("無股價資料")
            return pd.DataFrame()

        result_df = pd.concat(all_data, ignore_index=True)
        result_df["date"] = pd.to_datetime(result_df["date"]).dt.date

        logger.info(f"取得 {len(result_df)} 筆股價資料")
        return result_df

    def _process_batch_data(
        self,
        df: pd.DataFrame,
        batch_symbols: list[str]
    ) -> list[pd.DataFrame]:
        """
        處理批次下載的資料（相容 yfinance 1.0+）

        Args:
            df: yfinance 返回的 DataFrame
            batch_symbols: 該批次的股票代號列表

        Returns:
            處理後的 DataFrame 列表
        """
        result = []

        # yfinance 1.0+ 使用 MultiIndex columns: (Ticker, Price)
        # - Level 0: Ticker (2330.TW, 2317.TW, ...)
        # - Level 1: Price (Open, High, Low, Close, Volume, ...)
        has_multi_index = isinstance(df.columns, pd.MultiIndex)

        if len(batch_symbols) == 1:
            symbol = batch_symbols[0]
            stock_df = df.copy()

            if has_multi_index:
                # yfinance 1.0+: 扁平化欄位名稱，取 Price 層 (level=1)
                stock_df.columns = stock_df.columns.get_level_values(1)

            # yfinance 1.4+ 把 index.name 設為 None，reset_index 後欄位會變 'index'
            # 統一補回 'Date'，讓下面的 rename 在 1.0 / 1.4 都能對到
            stock_df.index.name = "Date"
            stock_df = stock_df.reset_index()
            stock_df = stock_df.rename(columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })
            stock_df["stock_id"] = self._from_tw_symbol(symbol)
            stock_df = stock_df.dropna(subset=["close"])
            if not stock_df.empty:
                result.append(stock_df[["date", "stock_id", "open", "high", "low", "close", "volume"]])
        else:
            # 多檔處理
            for symbol in batch_symbols:
                try:
                    if has_multi_index:
                        # yfinance 1.0+: columns 是 (Ticker, Price)
                        # Ticker 在 level 0
                        if symbol not in df.columns.get_level_values(0):
                            continue
                        # 取出該股票的資料
                        stock_df = df.xs(symbol, axis=1, level=0).copy()
                    else:
                        # 舊版 yfinance: 直接用 ticker 索引
                        if symbol not in df.columns:
                            continue
                        stock_df = df[symbol].copy()

                    # yfinance 1.4+ 把 index.name 設為 None，reset_index 後欄位會變 'index'
                    # 統一補回 'Date'，讓下面的 rename 在 1.0 / 1.4 都能對到
                    stock_df.index.name = "Date"
                    stock_df = stock_df.reset_index()
                    stock_df = stock_df.rename(columns={
                        "Date": "date",
                        "Open": "open",
                        "High": "high",
                        "Low": "low",
                        "Close": "close",
                        "Volume": "volume",
                    })
                    stock_df["stock_id"] = self._from_tw_symbol(symbol)
                    stock_df = stock_df.dropna(subset=["close"])
                    if not stock_df.empty:
                        result.append(stock_df[["date", "stock_id", "open", "high", "low", "close", "volume"]])
                except Exception as e:
                    logger.debug(f"處理 {symbol} 失敗: {e}")
                    continue

        return result

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
            retry_count: 重試次數

        Returns:
            DataFrame with columns:
            - date: 日期
            - taiex: 加權指數
        """
        if end_date is None:
            end_date = date.today()

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        logger.info(f"取得大盤指數: {start_date} ~ {end_date}")

        for attempt in range(retry_count):
            try:
                self._request_count += 1

                # 台灣加權指數代號
                df = yf.download(
                    tickers="^TWII",
                    start=start_date.strftime("%Y-%m-%d"),
                    end=(end_date + timedelta(days=1)).strftime("%Y-%m-%d"),
                    progress=False,
                    auto_adjust=True,  # 使用調整後收盤價
                )

                if df.empty:
                    if attempt < retry_count - 1:
                        wait_time = self._downloader.get_interval() * (attempt + 1)
                        logger.warning(f"大盤指數無資料，等待 {wait_time:.1f} 秒後重試...")
                        time.sleep(wait_time)
                        continue
                    logger.warning("無大盤指數資料")
                    return pd.DataFrame()

                # yfinance 1.0+: 處理 MultiIndex columns (Price, Ticker)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                df = df.reset_index()
                df = df.rename(columns={"Date": "date", "Close": "taiex"})
                df["date"] = pd.to_datetime(df["date"]).dt.date
                df = df[["date", "taiex"]]

                logger.info(f"取得 {len(df)} 筆大盤指數")
                return df

            except Exception as e:
                error_msg = str(e)
                self._log_error("get_market_index", error_msg)

                if attempt < retry_count - 1:
                    wait_time = self._downloader.get_interval() * (attempt + 1)
                    logger.warning(
                        f"取得大盤指數失敗: {error_msg}, "
                        f"等待 {wait_time:.1f} 秒後重試 ({attempt + 1}/{retry_count})"
                    )
                    time.sleep(wait_time)
                else:
                    logger.error(f"取得大盤指數失敗，已達重試上限: {error_msg}")

        return pd.DataFrame()

    # 判斷交易日用的指標股（台股權值股）。用「多檔」而非單檔，是因為單一個股
    # 在正常交易日也可能 0 成交（停牌、冷門股），只有「全部都 0 成交」才是休市。
    DEFAULT_REF_STOCKS = ["2330", "2317", "2454", "2412", "1301"]

    def get_latest_trading_date(
        self,
        ref_stocks: Optional[list[str]] = None,
        lookback_days: int = 14,
    ) -> Optional[date]:
        """查詢 yfinance 實際有交易的最新交易日（多檔指標股投票）。

        判定規則：某日只要**任一檔指標股 Volume > 0**，就算交易日；
        **全部指標股都 0 成交**才判定為休市日。

        為什麼要這樣做：
        1. yfinance 會為休市日捏造資料——2026-07-10 台股因颱風全日休市，
           yfinance 仍給出該日 OHLC（全部等於 7/09 收盤價），破綻只有 Volume=0。
           不濾掉的話會把不存在的交易日當成最新日（2026-07-13 就因此抓了假的
           7/10、把整批假股價寫進 DB）。
        2. 但**不能只看單一檔**：個股在正常交易日也可能 0 成交（停牌、冷門股），
           單檔判斷會把真實交易日誤殺。多檔投票可避免。
        3. 也不能用大盤指數（^TWII）的成交量：實測 2026-07-14 是真實交易日，
           ^TWII 的 Volume 卻是 0，會誤殺。

        真實交易日即使 Close 還是 NaN（盤中/尚未結算），Volume 仍然 > 0。

        Args:
            ref_stocks: 指標股清單（預設台股權值股；美股請傳 ["AAPL", ...]）
            lookback_days: 往前查幾個日曆天（需涵蓋連假）

        Returns:
            最新「有實際成交」的交易日；查不到回傳 None
        """
        stocks = ref_stocks or self.DEFAULT_REF_STOCKS
        start = date.today() - timedelta(days=lookback_days)
        end = date.today() + timedelta(days=1)

        # 收集每個日期「有成交的指標股檔數」
        traded_dates: set[date] = set()
        ok_count = 0

        for stock_id in stocks:
            symbol = self._to_tw_symbol(stock_id) if stock_id.isdigit() else stock_id
            try:
                hist = yf.Ticker(symbol).history(
                    start=start, end=end, auto_adjust=False
                )
            except Exception as e:
                logger.warning(f"yfinance 指標股 {symbol} 查詢失敗: {e}")
                continue

            if hist is None or hist.empty or "Volume" not in hist.columns:
                continue

            ok_count += 1
            traded = hist[hist["Volume"].fillna(0) > 0]
            for ts in traded.index:
                traded_dates.add(ts.date() if hasattr(ts, "date") else ts)

        if not ok_count:
            self._log_error("get_latest_trading_date", "所有指標股都查詢失敗")
            logger.warning("yfinance 所有指標股都查不到資料")
            return None

        if not traded_dates:
            logger.warning("yfinance 指標股近期都沒有成交紀錄（無法判斷交易日）")
            return None

        latest = max(traded_dates)
        logger.info(
            f"yfinance 最新交易日 = {latest}"
            f"（{ok_count}/{len(stocks)} 檔指標股可用，已排除全市場 0 成交的休市日）"
        )
        return latest

    def _log_error(self, method: str, error: str):
        """記錄錯誤"""
        self._error_log.append({
            "time": datetime.now().isoformat(),
            "method": method,
            "error": error,
        })

    def get_stats(self) -> dict:
        """取得客戶端統計資訊"""
        return {
            "total_requests": self._request_count,
            "error_count": len(self._error_log),
        }

    def get_error_log(self) -> list[dict]:
        """取得錯誤日誌"""
        return self._error_log.copy()
