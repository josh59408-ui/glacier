"""
每日任務
使用 HybridClient (FinMind + yfinance) + SQLite 架構
"""
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

from api.hybrid_client import HybridClient
from data.sqlite_database import SQLiteDatabase
from calculators.vcp_filter import VCPFilter, calculate_market_return
from calculators.sanxian_filter import SanxianFilter
from calculators.volume_surge_filter import VolumeSurgeFilter
from exporters.google_sheet import GoogleSheetExporter
from utils.trading_calendar import TradingCalendar
from utils.split_detector import SplitDetector
from utils.daily_verifier import DailyVerifier
from utils.objective_verifier import ObjectiveVerifier
from utils.price_gap_filler import fill_price_gaps

# 資料源回報的「最新交易日」與交易日曆最近交易日的最大容許落差（日曆天）。
# 超過就判定資料源給了過期/異常資料，不予採信（2026-07-14 FinMind 曾回 6/30）。
# 抓 5 天：足以涵蓋連假（資料源正常落後），又能擋掉「倒退兩週」這種明顯異常。
MAX_SOURCE_LAG_DAYS = 5


class DailyTask:
    """
    每日任務

    執行流程:
    1. 取得當日股價（yfinance 批量查詢）
    2. 更新 SQLite 資料庫
    3. 除權息/減資偵測（比對 DB vs FinMind 還原權息價）
    4. 取得當日大盤指數
    5. 執行 VCP 篩選
    6. 執行三線開花篩選
    7. 匯出至 Google Sheet
    """

    def __init__(
        self,
        client: Optional[HybridClient] = None,
        db: Optional[SQLiteDatabase] = None,
        exporter: Optional[GoogleSheetExporter] = None
    ):
        """
        初始化每日任務

        Args:
            client: HybridClient API 客戶端
            db: SQLite 資料庫連線
            exporter: Google Sheet 匯出器
        """
        self.client = client or HybridClient()
        self.db = db or SQLiteDatabase()
        self.exporter = exporter or GoogleSheetExporter()

        # 篩選器
        self.vcp_filter = VCPFilter()
        self.sanxian_filter = SanxianFilter()
        self.volume_surge_filter = VolumeSurgeFilter()

    def run(
        self,
        target_date: Optional[date] = None,
        skip_non_trading_day: bool = True
    ) -> dict:
        """
        執行每日任務

        Args:
            target_date: 目標日期（預設為今天）
            skip_non_trading_day: 是否在非交易日跳過執行（預設 True）

        Returns:
            執行結果統計
        """
        # 自動模式（未指定日期）：問「資料源」最新交易日來決定要抓哪天，而非 date.today()。
        # 資料源自己知道颱風假、臨時休市（沒交易就沒資料），也不受排程延遲影響。
        # 但資料源可能回「過期的爛資料」（2026-07-14 FinMind 回 6/30 害當天用兩週前資料跑），
        # 所以一定要做合理性驗證：與交易日曆的最近交易日差距過大就不採信。
        # fallback 順序：資料源最新日（通過驗證）→ 交易日曆最近交易日。
        if target_date is None:
            calendar_latest = TradingCalendar.get_latest_trading_day(date.today())
            source_latest = self.client.get_latest_trading_date()

            if source_latest and (calendar_latest - source_latest).days <= MAX_SOURCE_LAG_DAYS:
                original_date = source_latest
                logger.info(f"自動模式：資料源最新交易日 = {original_date}")
            else:
                if source_latest:
                    logger.error(
                        f"資料源回傳 {source_latest}，與交易日曆最近交易日 {calendar_latest} "
                        f"差距超過 {MAX_SOURCE_LAG_DAYS} 天，判定為異常資料（不採信）"
                    )
                else:
                    logger.warning("資料源查詢失敗")
                original_date = calendar_latest
                logger.warning(f"改用交易日曆最近交易日: {original_date}")
        else:
            original_date = target_date

        # 檢查是否為交易日
        if not TradingCalendar.is_trading_day(original_date):
            if skip_non_trading_day:
                logger.info(f"{original_date} 非交易日，跳過執行")
                return {
                    "date": original_date,
                    "success": True,  # 跳過也算成功
                    "skipped": True,
                    "reason": "非交易日",
                    "price_count": 0,
                    "vcp_count": 0,
                    "sanxian_count": 0,
                    "volume_surge_count": 0,
                    "errors": [],
                }
            else:
                # 使用最近的交易日
                target_date = TradingCalendar.get_latest_trading_day(original_date)
                logger.info(f"{original_date} 非交易日，使用最近交易日: {target_date}")
        else:
            target_date = original_date

        logger.info(f"=== 開始執行每日任務: {target_date} ===")

        result = {
            "date": target_date,
            "success": False,
            "skipped": False,
            "price_count": 0,
            "split_refreshed_count": 0,
            "vcp_count": 0,
            "sanxian_count": 0,
            "volume_surge_count": 0,
            "errors": [],
        }

        try:
            # 確保資料表存在
            self.db.create_tables()

            # Step 1: 確保有股票清單
            stock_info = self.db.get_stock_info_dict()
            if not stock_info:
                logger.info("股票清單為空，先取得股票清單...")
                stock_df = self.client.get_stock_info()
                if not stock_df.empty:
                    self.db.upsert_stock_info(stock_df)
                    stock_info = self.db.get_stock_info_dict()

            if not stock_info:
                result["errors"].append("無法取得股票清單")
                logger.error("無法取得股票清單，任務結束")
                return result

            # Step 1.5: 同步自訂產業/連結（Google Sheet「自訂產業連結」分頁 → DB）
            try:
                master = {sid: (info.get("stock_name") or "") for sid, info in stock_info.items()}
                overrides = self.exporter.sync_custom_overrides(master)
                if overrides is not None:
                    self.db.replace_custom_overrides(overrides)
                else:
                    logger.warning("自訂欄位同步回傳 None（讀取失敗），沿用 DB 既有值")
            except Exception as e:
                logger.warning(f"自訂欄位同步失敗（不影響後續流程）: {e}")

            # Step 2: 取得並儲存股價（批量查詢）
            price_count = self._fetch_and_save_prices(target_date, stock_info)
            result["price_count"] = price_count

            if price_count == 0:
                # 防呆：當日抓到 0 筆，通常是排程延遲、在當日資料尚未產生時跑。
                # 自動退回上一個交易日重抓，避免整個任務空跑失敗。
                prev = TradingCalendar.get_previous_trading_day(target_date)
                if prev and prev != target_date:
                    logger.warning(
                        f"{target_date} 抓到 0 筆（可能尚未收盤/資料未就緒），"
                        f"自動退回上一交易日 {prev} 重抓"
                    )
                    target_date = prev
                    result["date"] = target_date
                    price_count = self._fetch_and_save_prices(target_date, stock_info)
                    result["price_count"] = price_count

                if price_count == 0:
                    result["errors"].append("無股價資料（可能非交易日）")
                    logger.warning("無股價資料，任務結束")
                    return result

            # Step 2.5: 補齊歷史缺漏股價（在篩選前修好）
            try:
                gap_filled = fill_price_gaps(
                    db_path=self.db.db_path,
                    price_table="daily_price",
                    ref_stock="2330",
                    yf_suffix=".TW",
                    yf_alt_suffix=".TWO",
                )
                result["gap_filled"] = gap_filled
            except Exception as e:
                logger.warning(f"補漏失敗（不影響後續流程）: {e}")
                result["gap_filled"] = 0

            # Step 3: 減資/分割偵測
            split_count = self._detect_and_refresh_splits(target_date, stock_info)
            result["split_refreshed_count"] = split_count

            # Step 4: 取得並儲存大盤指數
            market_count = self._fetch_and_save_market_index(target_date)
            if market_count == 0:
                logger.warning("無大盤指數資料，VCP 篩選可能不準確")

            # Step 5: 執行篩選
            vcp_results, sanxian_results, volume_surge_results, market_return = \
                self._run_filters(target_date)
            result["vcp_count"] = len(vcp_results)
            result["sanxian_count"] = len(sanxian_results)
            result["volume_surge_count"] = len(volume_surge_results)

            # Step 6: 匯出至 Google Sheet（包含驗證資料）
            self._export_to_sheet(
                target_date, vcp_results, sanxian_results,
                volume_surge_results, market_return
            )

            # Step 7: 每日自動驗證
            verifier = DailyVerifier(self.db, market="tw", min_price_count=1500)
            verify_ok = verifier.verify_all(
                target_date, vcp_results, sanxian_results,
                price_count, market_return,
            )
            result["verification_passed"] = verify_ok

            # Step 8: 客觀驗證（獨立資料來源 + Sheet 回讀）
            try:
                obj_verifier = ObjectiveVerifier(db=self.db, market="tw")
                obj_result = obj_verifier.verify_all(
                    target_date, vcp_results, sanxian_results,
                    market_return, self.exporter,
                )
                result["objective_verification"] = obj_result
            except Exception as e:
                logger.warning(f"客觀驗證失敗（不影響結果）: {e}")

            result["success"] = True
            logger.info(
                f"=== 每日任務完成: VCP {len(vcp_results)} 檔, "
                f"三線開花 {len(sanxian_results)} 檔 ==="
            )

        except Exception as e:
            logger.error(f"每日任務失敗: {e}")
            result["errors"].append(str(e))

        # SPEC: 將錯誤日誌寫入 Google Sheet「台股更新紀錄」
        error_logs = self.client.get_error_log()
        if error_logs and self.exporter.health_check():
            self.exporter.log_error_to_sheet(error_logs)

        return result

    def _fetch_and_save_prices(self, target_date: date, stock_info: dict) -> int:
        """取得並儲存股價（批量查詢）"""
        logger.info("取得當日股價...")

        # 取得所有股票代號和市場類型
        stock_ids = list(stock_info.keys())
        market_types = self.db.get_stock_market_types()

        # 使用 yfinance 批量查詢
        price_df = self.client.get_stock_price(
            start_date=target_date,
            end_date=target_date,
            stock_ids=stock_ids,
            market_types=market_types
        )

        if price_df.empty:
            return 0

        # 儲存至資料庫
        count = self.db.upsert_daily_price(price_df)
        return count

    def _fetch_and_save_market_index(self, target_date: date) -> int:
        """取得並儲存大盤指數"""
        logger.info("取得大盤指數...")

        market_df = self.client.get_market_index(target_date)

        if market_df.empty:
            logger.warning("無大盤指數資料")
            return 0

        count = self.db.upsert_market_index(market_df)
        return count

    def _detect_and_refresh_splits(self, target_date: date, stock_info: dict) -> int:
        """偵測除權息/減資並重新下載受影響股票的完整歷史

        用 FinMind TaiwanStockPriceAdj（還原權息價）比對 DB 中前一交易日的
        未調整收盤價。差異超過 1% 的股票需要重新下載歷史以確保均線正確。

        Returns:
            重新下載的股票數量
        """
        prev_date = TradingCalendar.get_previous_trading_day(target_date)
        if not prev_date:
            return 0

        # 取得 DB 中前一交易日收盤價（未調整）
        prev_prices_df = self.db.get_daily_prices(prev_date, prev_date)
        if prev_prices_df.empty:
            logger.info("DB 中無前一交易日資料，跳過除權息偵測")
            return 0

        db_prices = dict(zip(
            prev_prices_df["stock_id"],
            prev_prices_df["close_price"],
        ))

        # 從 FinMind 取得同日還原權息價
        adj_prices = SplitDetector.fetch_adjusted_prices(prev_date)
        if not adj_prices:
            logger.warning("無法取得 FinMind 還原股價，跳過除權息偵測")
            return 0

        logger.info(f"除權息偵測：比對 {len(adj_prices)} 檔股票的 DB vs 還原權息價")

        # 偵測有差異的股票
        adjusted_stocks = SplitDetector.detect_adjusted_stocks(db_prices, adj_prices)

        if not adjusted_stocks:
            logger.info("未偵測到除權息/減資，所有價格一致")
            return 0

        logger.warning(
            f"偵測到 {len(adjusted_stocks)} 檔股票有價格調整（除權息/減資）: "
            f"{adjusted_stocks[:10]}{'...' if len(adjusted_stocks) > 10 else ''}"
        )

        # 用 FinMind 還原股價重新下載受影響股票的 365 天完整歷史
        history_start = target_date - timedelta(days=365)
        logger.info(
            f"開始重新下載 {len(adjusted_stocks)} 檔股票的還原權息歷史 "
            f"({history_start} ~ {target_date})..."
        )

        records = SplitDetector.fetch_adjusted_history(
            adjusted_stocks, history_start, target_date
        )

        if not records:
            logger.warning("重新下載還原歷史資料為空")
            return 0

        history_df = pd.DataFrame(records)
        count = self.db.upsert_daily_price(history_df)
        logger.info(
            f"已重新下載並更新 {len(adjusted_stocks)} 檔股票的歷史資料 "
            f"(共 {count} 筆)"
        )

        return len(adjusted_stocks)

    def _run_filters(
        self, target_date: date
    ) -> tuple[list[dict], list[dict], list[dict], float]:
        """執行篩選

        Returns:
            (vcp_results, sanxian_results, volume_surge_results, market_return_20d)
        """
        logger.info("執行篩選...")

        # 取得計算所需的歷史資料（252 天）
        start_date = target_date - timedelta(days=365)
        price_df = self.db.get_daily_prices(start_date, target_date)
        market_df = self.db.get_market_index(start_date, target_date)

        if price_df.empty:
            logger.warning("無足夠歷史資料")
            return [], [], [], 0.0

        # 計算大盤報酬率
        market_return = calculate_market_return(market_df, target_date, lookback=20)
        logger.info(f"大盤 20 日報酬率: {market_return:.2%}")

        # 取得股票基本資料
        stock_info = self.db.get_stock_info_dict()
        if not stock_info:
            logger.warning("股票基本資料為空，請先執行 'python main.py init'")

        # 只保留 stock_info 中的股票（過濾掉 ETF、權證等）
        valid_stock_ids = set(stock_info.keys())
        before_filter = price_df["stock_id"].nunique()
        price_df = price_df[price_df["stock_id"].isin(valid_stock_ids)]
        after_filter = price_df["stock_id"].nunique()
        logger.info(f"過濾股票: {before_filter} -> {after_filter} 檔（排除 ETF/權證）")

        # VCP 篩選
        vcp_df = self.vcp_filter.filter(price_df, market_return, target_date)
        vcp_results = self._enrich_results(vcp_df, stock_info)

        # 三線開花篩選
        sanxian_df = self.sanxian_filter.filter(price_df, target_date)
        sanxian_results = self._enrich_results(sanxian_df, stock_info)

        # 量大強漲篩選（獨立第四類，不比較新舊）
        volume_surge_df = self.volume_surge_filter.filter(price_df, target_date)
        volume_surge_results = self._enrich_results(volume_surge_df, stock_info)

        # 儲存篩選結果
        self.db.save_filter_results(vcp_results, "vcp", target_date)
        self.db.save_filter_results(sanxian_results, "sanxian", target_date)
        self.db.save_filter_results(volume_surge_results, "volume_surge", target_date)

        # 準備驗證資料
        self._vcp_verification_data = self._prepare_vcp_verification(
            price_df, market_return, target_date
        )
        self._sanxian_verification_data = self._prepare_sanxian_verification(
            price_df, target_date
        )

        return vcp_results, sanxian_results, volume_surge_results, market_return

    def _enrich_results(
        self,
        df,
        stock_info: dict[str, dict]
    ) -> list[dict]:
        """補充股票基本資料"""
        if df.empty:
            return []

        def _safe_str(val, default="-"):
            """將 NaN/None 轉為預設字串"""
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return default
            return str(val)

        results = []
        for _, row in df.iterrows():
            stock_id = row["stock_id"]
            info = stock_info.get(stock_id, {})

            result = row.to_dict()
            # 清理 row.to_dict() 中的 NaN 值（pandas 將 NULL 轉為 float NaN）
            result = {k: (v if not (isinstance(v, float) and pd.isna(v)) else None)
                      for k, v in result.items()}
            result.update({
                "stock_name": _safe_str(info.get("stock_name"), ""),
                "company_name": _safe_str(info.get("stock_name"), ""),
                "industry_category": _safe_str(info.get("industry_category")),
                "industry_category2": _safe_str(info.get("industry_category2")),
                "product_mix": "-",
            })
            results.append(result)

        return results

    def _prepare_vcp_verification(
        self,
        price_df: pd.DataFrame,
        market_return: float,
        target_date: date
    ) -> list[dict]:
        """
        準備 VCP 驗證資料（包含所有計算欄位）
        """
        from calculators.moving_average import MovingAverageCalculator
        from config.settings import VCP_PARAMS

        if price_df.empty:
            return []

        # 準備計算資料
        df = MovingAverageCalculator.prepare_vcp_data(price_df)
        if df.empty:
            return []

        # 取得目標日期的資料
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[df["date"] == target_date].copy()

        if df.empty:
            return []

        # 計算所有條件
        close = df["close_price"].fillna(0)
        ma50 = df["ma50"].fillna(float("inf"))
        ma150 = df["ma150"].fillna(float("inf"))
        ma200 = df["ma200"].fillna(float("inf"))

        df["cond1"] = close > ma50
        df["cond2"] = ma50 > ma150
        df["cond3"] = ma150 > ma200
        df["cond4"] = df["ma200_slope_20d"].fillna(-1) > 0
        df["cond5"] = df["return_20d"].fillna(-float("inf")) > market_return
        # cond6: 離 250 日盤中最低點 ≥ 30%（收盤 > low_250d × 1.30，強勢/新高共用）
        # low_250d 為 NaN（資料不足）時比較為 False，不誤選
        df["cond6"] = close > df["low_250d"] * VCP_PARAMS["low_250d_mult"]

        # 強勢清單
        df["is_strong"] = (
            df["cond1"] & df["cond2"] & df["cond3"] & df["cond4"] & df["cond5"] & df["cond6"]
        )

        # 新高清單：近 5 日最高價 == 近 250 交易日最高價（250 日高點落在最近 5 日內）
        df["is_new_high"] = (df["high_5d"] >= df["high_250d"]) & df["cond5"] & df["cond6"]

        # VCP = 強勢 OR 新高
        df["is_vcp"] = df["is_strong"] | df["is_new_high"]

        # 輸出所有股票的計算數據供驗證
        return df.to_dict("records")

    def _prepare_sanxian_verification(
        self,
        price_df: pd.DataFrame,
        target_date: date
    ) -> list[dict]:
        """
        準備三線開花驗證資料（包含所有計算欄位）
        """
        from calculators.moving_average import MovingAverageCalculator

        if price_df.empty:
            return []

        # 準備計算資料
        df = MovingAverageCalculator.prepare_sanxian_data(price_df)
        if df.empty:
            return []

        # 取得目標日期的資料
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[df["date"] == target_date].copy()

        if df.empty:
            return []

        # 計算所有條件
        close = df["close_price"].fillna(0)
        ma8 = df["ma8"].fillna(float("inf"))
        ma21 = df["ma21"].fillna(float("inf"))
        ma55 = df["ma55"].fillna(float("inf"))

        df["cond1"] = close > ma8
        df["cond2"] = ma8 > ma21
        df["cond3"] = ma21 > ma55
        df["cond4"] = close >= df["high_55d"].fillna(float("inf"))

        df["is_sanxian"] = df["cond1"] & df["cond2"] & df["cond3"] & df["cond4"]

        # 計算差距比例
        second_high = df["second_high_55d"].fillna(1).replace(0, 1)
        df["gap_ratio"] = (close / second_high - 1)

        # 輸出所有股票的計算數據供驗證
        return df.to_dict("records")

    def _get_recent_stock_ids(
        self, target_date: date, filter_type: str, lookback: int = 20
    ) -> set:
        """取得近 lookback 個交易日（不含當天）出現過的篩選結果股票代號聯集

        用於新/舊股票標記（lookback 單位為「交易日」）：
        - 在此集合內 → 近 lookback 交易日曾出現過（灰底，舊股）
        - 不在此集合 → 近 lookback 交易日首次出現（白底，新股）
        """
        # 20 交易日約 28 日曆天，往前抓 2 倍日曆範圍以確保湊滿 lookback 個交易日
        start = target_date - timedelta(days=lookback * 2)
        end = target_date - timedelta(days=1)
        recent_days = TradingCalendar.get_trading_days_in_range(start, end)[-lookback:]

        recent_ids: set = set()
        for d in recent_days:
            try:
                df = self.db.get_filter_results(filter_type, d)
                if not df.empty:
                    recent_ids.update(df["stock_id"].tolist())
            except Exception as e:
                logger.warning(f"取得 {d} {filter_type} 結果失敗: {e}")
        return recent_ids

    def _export_to_sheet(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
        volume_surge_results: list[dict],
        market_return: float = 0.0
    ):
        """匯出至 Google Sheet"""
        if not self.exporter.health_check():
            logger.warning("Google Sheet 未連線，跳過匯出")
            return

        # 取得近 20 交易日出現過的股票（新/舊標記：近 20 交易日首次出現=新股白底）
        recent_vcp_ids = self._get_recent_stock_ids(target_date, "vcp")
        recent_sanxian_ids = self._get_recent_stock_ids(target_date, "sanxian")

        # 匯出 VCP
        if vcp_results:
            self.exporter.export_vcp(
                vcp_results, target_date, prev_stock_ids=recent_vcp_ids
            )

        # 匯出三線開花
        if sanxian_results:
            self.exporter.export_sanxian(
                sanxian_results, target_date, prev_stock_ids=recent_sanxian_ids
            )

        # 匯出量大強漲（獨立類型，不比較新舊、不傳 prev_stock_ids）
        if volume_surge_results:
            self.exporter.export_volume_surge(volume_surge_results, target_date)

        # 匯出驗證資料
        vcp_verification = getattr(self, "_vcp_verification_data", [])
        sanxian_verification = getattr(self, "_sanxian_verification_data", [])

        if vcp_verification or sanxian_verification:
            self.exporter.export_verification(
                vcp_verification,
                sanxian_verification,
                target_date,
                market_return
            )

            # 清理驗證 Sheet 過舊的每日明細分頁（保留最近 10 天的 YYMMDD_VCP/三線；
            # 「驗證日誌」等固定分頁一律保留）。刻意放在每日任務的匯出路徑——
            # reexport/backfill 走別的入口不會觸發，避免誤刪正在補的歷史分頁。
            try:
                from config.settings import SHEET_IDS
                from utils.verification_cleaner import cleanup_verification_tabs

                vsheet_id = SHEET_IDS.get("verification")
                if vsheet_id and self.exporter.client:
                    ss = self.exporter.client.open_by_key(vsheet_id)
                    cleanup_verification_tabs(ss, keep_days=10)
            except Exception as e:
                logger.warning(f"驗證分頁清理失敗（不影響主流程）: {e}")


def run_daily_task(target_date: Optional[date] = None) -> dict:
    """
    執行每日任務的便捷函數

    Args:
        target_date: 目標日期

    Returns:
        執行結果
    """
    task = DailyTask()
    return task.run(target_date)
