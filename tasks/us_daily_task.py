"""
美股每日任務
完全獨立於台股，使用獨立的資料庫和設定
"""
from datetime import date, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

from config.us_settings import get_us_client
from data.us_database import USSQLiteDatabase
from calculators.us_vcp_filter import USVCPFilter, calculate_us_market_return
from calculators.us_sanxian_filter import USSanxianFilter
from calculators.us_volume_surge_filter import USVolumeSurgeFilter
from exporters.us_google_sheet import USGoogleSheetExporter
from utils.us_trading_calendar import USMarketCalendar
from utils.us_split_detector import USSplitDetector
from utils.daily_verifier import DailyVerifier
from utils.objective_verifier import ObjectiveVerifier
from utils.price_gap_filler import fill_price_gaps
from utils.internal_split_detector import detect_and_fix_internal_splits

# 資料源回報的「最新交易日」與交易日曆最近交易日的最大容許落差（日曆天）。
# 超過就判定資料源給了過期/異常資料，不予採信（台股 2026-07-14 曾被 FinMind 回 6/30 害到）。
MAX_SOURCE_LAG_DAYS = 5


class USDailyTask:
    """
    美股每日任務

    執行流程:
    1. 檢查是否為美股交易日
    2. 取得當日股價（yfinance 批量查詢）
    3. 取得 S&P 500 大盤指數
    4. 更新 SQLite 資料庫
    5. 執行 VCP 篩選
    6. 執行三線開花篩選
    7. 匯出至美股專用 Google Sheet
    """

    def __init__(
        self,
        client=None,
        db: Optional[USSQLiteDatabase] = None,
        exporter: Optional[USGoogleSheetExporter] = None
    ):
        """
        初始化美股每日任務

        Args:
            client: 美股 API 客戶端
            db: 美股 SQLite 資料庫連線
            exporter: 美股 Google Sheet 匯出器
        """
        self.client = client or get_us_client()
        self.db = db or USSQLiteDatabase()
        self.exporter = exporter or USGoogleSheetExporter()

        # 篩選器
        self.vcp_filter = USVCPFilter()
        self.sanxian_filter = USSanxianFilter()
        self.volume_surge_filter = USVolumeSurgeFilter()

    def run(
        self,
        target_date: Optional[date] = None,
        skip_non_trading_day: bool = True
    ) -> dict:
        """
        執行美股每日任務

        Args:
            target_date: 目標日期（預設為今天）
            skip_non_trading_day: 是否在非交易日跳過執行（預設 True）

        Returns:
            執行結果統計
        """
        # 自動模式（未指定日期）：問「資料源」最新交易日來決定要抓哪天，而非 date.today()。
        # 資料源自己知道臨時休市（沒交易就沒資料），也不受排程延遲影響。
        # 但資料源可能回過期/異常的日期（台股 2026-07-14 曾被 FinMind 截斷回 6/30），
        # 所以一定要做合理性驗證：與交易日曆最近交易日差距過大就不採信。
        if target_date is None:
            calendar_latest = USMarketCalendar.get_latest_trading_day(date.today())
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

        # 檢查是否為美股交易日
        if not USMarketCalendar.is_trading_day(original_date):
            if skip_non_trading_day:
                logger.info(f"{original_date} 非美股交易日，跳過執行")
                return {
                    "date": original_date,
                    "success": True,  # 跳過也算成功
                    "skipped": True,
                    "reason": "非美股交易日",
                    "price_count": 0,
                    "vcp_count": 0,
                    "sanxian_count": 0,
                    "volume_surge_count": 0,
                    "errors": [],
                }
            else:
                # 使用最近的美股交易日
                target_date = USMarketCalendar.get_latest_trading_day(original_date)
                logger.info(f"{original_date} 非美股交易日，使用最近交易日: {target_date}")
        else:
            target_date = original_date

        logger.info(f"=== 開始執行美股每日任務: {target_date} ===")

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
            # 確保美股資料表存在
            self.db.create_tables()

            # Step 1: 確保有股票清單
            stock_info = self.db.get_stock_info_dict()
            if not stock_info:
                logger.info("美股股票清單為空，先取得股票清單...")
                stock_df = self.client.get_stock_info()
                if not stock_df.empty:
                    self.db.upsert_stock_info(stock_df)
                    stock_info = self.db.get_stock_info_dict()

            if not stock_info:
                result["errors"].append("無法取得美股股票清單")
                logger.error("無法取得美股股票清單，任務結束")
                return result

            # Step 1.5: 同步自訂產業/連結（Google Sheet「自訂產業連結」分頁 → DB）
            try:
                master = {sid: (info.get("stock_name") or "") for sid, info in stock_info.items()}
                overrides = self.exporter.sync_custom_overrides(master)
                if overrides is not None:
                    self.db.replace_custom_overrides(overrides)
                else:
                    logger.warning("美股自訂欄位同步回傳 None（讀取失敗），沿用 DB 既有值")
            except Exception as e:
                logger.warning(f"美股自訂欄位同步失敗（不影響後續流程）: {e}")

            # Step 2: 取得並儲存股價（批量查詢，下載 2 天供分割偵測）
            price_count = self._fetch_and_save_prices(target_date, stock_info)
            result["price_count"] = price_count

            if price_count == 0:
                # 防呆：當日抓到 0 筆，通常是 GitHub 排程延遲、在美股開盤前跑，
                # 當日資料尚未產生。自動退回上一個交易日重抓，避免整個任務空跑失敗。
                prev = USMarketCalendar.get_previous_trading_day(target_date)
                if prev and prev != target_date:
                    logger.warning(
                        f"{target_date} 抓到 0 筆（可能尚未開盤/收盤），"
                        f"自動退回上一交易日 {prev} 重抓"
                    )
                    target_date = prev
                    result["date"] = target_date
                    price_count = self._fetch_and_save_prices(target_date, stock_info)
                    result["price_count"] = price_count

                if price_count == 0:
                    result["errors"].append("無美股股價資料（可能非交易日）")
                    logger.warning("無美股股價資料，任務結束")
                    return result

            # Step 2.5: 補齊歷史缺漏股價（在篩選前修好）
            try:
                gap_filled = fill_price_gaps(
                    db_path=self.db.db_path,
                    price_table="us_daily_price",
                    ref_stock="AAPL",
                    yf_suffix="",
                )
                result["gap_filled"] = gap_filled
            except Exception as e:
                logger.warning(f"補漏失敗（不影響後續流程）: {e}")
                result["gap_filled"] = 0

            # Step 2.6: 偵測分割並重新下載受影響股票的歷史資料
            split_count = self._detect_and_refresh_splits(target_date)
            result["split_refreshed_count"] = split_count

            # Step 2.7: 內部分割偵測（DB 自身相鄰價格跳動，補足前兩層的盲點）
            try:
                internal_result = detect_and_fix_internal_splits(
                    db_path=self.db.db_path,
                    price_table="us_daily_price",
                    whitelist_table="us_anomaly_whitelist",
                    scan_days=30,
                )
                result["internal_split_result"] = internal_result
            except Exception as e:
                logger.warning(f"內部分割偵測失敗（不影響後續流程）: {e}")
                result["internal_split_result"] = {"error": str(e)}

            # Step 3: 取得並儲存大盤指數
            market_count = self._fetch_and_save_market_index(target_date)
            if market_count == 0:
                logger.warning("無美股大盤指數資料，VCP 篩選可能不準確")

            # Step 4: 執行篩選
            vcp_results, sanxian_results, volume_surge_results, market_return = \
                self._run_filters(target_date)
            result["vcp_count"] = len(vcp_results)
            result["sanxian_count"] = len(sanxian_results)
            result["volume_surge_count"] = len(volume_surge_results)

            # Step 5: 匯出至美股 Google Sheet
            self._export_to_sheet(
                target_date, vcp_results, sanxian_results,
                volume_surge_results, market_return
            )

            # Step 6: 每日自動驗證
            verifier = DailyVerifier(self.db, market="us", min_price_count=6500)
            verify_ok = verifier.verify_all(
                target_date, vcp_results, sanxian_results,
                price_count, market_return,
            )
            result["verification_passed"] = verify_ok

            # Step 7: 客觀驗證（獨立資料來源 + Sheet 回讀）
            try:
                obj_verifier = ObjectiveVerifier(db=self.db, market="us")
                obj_result = obj_verifier.verify_all(
                    target_date, vcp_results, sanxian_results,
                    market_return, self.exporter,
                )
                result["objective_verification"] = obj_result
            except Exception as e:
                logger.warning(f"客觀驗證失敗（不影響結果）: {e}")

            result["success"] = True
            logger.info(
                f"=== 美股每日任務完成: VCP {len(vcp_results)} 檔, "
                f"三線開花 {len(sanxian_results)} 檔 ==="
            )

        except Exception as e:
            logger.error(f"美股每日任務失敗: {e}")
            result["errors"].append(str(e))

        # 記錄錯誤日誌
        error_logs = self.client.get_error_log()
        if error_logs and self.exporter.health_check():
            self.exporter.log_error_to_sheet(error_logs)

        return result

    def _fetch_and_save_prices(self, target_date: date, stock_info: dict) -> int:
        """取得並儲存美股股價（批量查詢 + rate limit 重試）

        下載前一交易日 + 當日共 2 天資料，用於分割偵測比對。
        如果資料庫中已有該日期的資料，則跳過下載以避免 API 速率限制。
        下載不完整時自動重試缺失的股票（最多 3 次）。

        Returns:
            當日股價筆數
        """
        # 先檢查資料庫中是否已有該日期的資料
        # 正常交易日應有 6000+ 筆，低於 MIN_PRICE_COUNT 筆視為殘缺需重新下載
        MIN_PRICE_COUNT = 6500
        MAX_RETRY = 3
        existing_count = self.db.get_price_count_by_date(target_date)
        if existing_count >= MIN_PRICE_COUNT:
            logger.info(f"資料庫中已有 {target_date} 的股價資料 ({existing_count} 筆)，跳過下載")
            return existing_count
        if existing_count > 0:
            logger.warning(
                f"資料庫中 {target_date} 的股價資料不完整 ({existing_count} 筆 < {MIN_PRICE_COUNT})，重新下載"
            )

        logger.info("取得美股當日股價...")

        # 取得所有股票代號
        stock_ids = list(stock_info.keys())

        # 取得前一交易日，下載 2 天資料供分割偵測使用
        prev_trading_day = USMarketCalendar.get_previous_trading_day(target_date)
        download_start = prev_trading_day if prev_trading_day else target_date

        # 第一次下載
        price_df = self.client.get_stock_price(
            start_date=download_start,
            end_date=target_date,
            stock_ids=stock_ids
        )

        if price_df.empty:
            return 0

        # 儲存第一次結果
        self.db.upsert_daily_price(price_df)
        today_count = len(price_df[price_df["date"] == target_date])

        # 防呆：當日完全沒資料（0 筆）通常是排程在美股開盤前跑、資料尚未產生，
        # 重試也不會有資料，直接回報 0 讓上層退回上一交易日（避免空等 35 分鐘重試）
        if today_count == 0:
            logger.warning(
                f"{target_date} 當日 0 筆（可能尚未開盤/收盤），"
                f"跳過重試，交由上層退回上一交易日"
            )
            return 0

        # 重試：如果筆數不足，找出缺失的股票重新下載
        for retry in range(1, MAX_RETRY + 1):
            if today_count >= MIN_PRICE_COUNT:
                break

            # 找出已成功下載的股票
            downloaded_ids = set(
                price_df[price_df["date"] == target_date]["stock_id"].unique()
            )
            missing_ids = [s for s in stock_ids if s not in downloaded_ids]

            if not missing_ids:
                break

            # rate limit 需要較長恢復時間：第1次 5min，第2次 15min，第3次 15min
            wait_time = 300 if retry == 1 else 900
            logger.warning(
                f"股價不完整: {today_count} 筆 (< {MIN_PRICE_COUNT})，"
                f"缺 {len(missing_ids)} 檔，"
                f"等待 {wait_time} 秒後重試 ({retry}/{MAX_RETRY})..."
            )
            import time
            time.sleep(wait_time)

            retry_df = self.client.get_stock_price(
                start_date=download_start,
                end_date=target_date,
                stock_ids=missing_ids
            )

            if not retry_df.empty:
                self.db.upsert_daily_price(retry_df)
                retry_count = len(retry_df[retry_df["date"] == target_date])
                today_count += retry_count
                # 合併到 price_df 供後續分割偵測用
                price_df = pd.concat([price_df, retry_df], ignore_index=True)
                logger.info(
                    f"重試 {retry}: 補回 {retry_count} 筆，累計 {today_count} 筆"
                )

        if today_count < MIN_PRICE_COUNT:
            logger.warning(
                f"重試 {MAX_RETRY} 次後仍不完整: {today_count} 筆 < {MIN_PRICE_COUNT}"
            )

        # 分割偵測：在 upsert 前先讀取 DB 中前一交易日的舊值
        self._fresh_prev_day_prices = {}
        self._db_prev_day_prices = {}

        if prev_trading_day:
            # 從 yfinance 下載結果中提取前一交易日的 fresh 價格
            prev_day_df = price_df[price_df["date"] == prev_trading_day]
            self._fresh_prev_day_prices = {
                row["stock_id"]: row["close"]
                for _, row in prev_day_df.iterrows()
                if pd.notna(row.get("close"))
            }

            # 從 DB 讀取前一交易日的舊收盤價
            db_prev_df = self.db.get_daily_prices(
                prev_trading_day, prev_trading_day
            )
            if not db_prev_df.empty:
                self._db_prev_day_prices = {
                    row["stock_id"]: row["close_price"]
                    for _, row in db_prev_df.iterrows()
                    if pd.notna(row.get("close_price"))
                }

        return today_count

    def _fetch_and_save_market_index(self, target_date: date) -> int:
        """取得並儲存美股大盤指數

        如果資料庫中已有該日期的資料，則跳過下載以避免 API 速率限制
        """
        # 先檢查資料庫中是否已有該日期的資料
        existing_df = self.db.get_market_index(target_date, target_date)
        if not existing_df.empty:
            logger.info(f"資料庫中已有 {target_date} 的大盤指數資料，跳過下載")
            return len(existing_df)

        logger.info("取得美股大盤指數 (S&P 500)...")

        market_df = self.client.get_market_index(target_date)

        if market_df.empty:
            logger.warning("無美股大盤指數資料")
            return 0

        count = self.db.upsert_market_index(market_df)
        return count

    def _detect_and_refresh_splits(self, target_date: date) -> int:
        """偵測分割/合股並重新下載受影響股票的完整歷史

        利用 _fetch_and_save_prices 已下載的前一日 fresh 資料，
        與 DB 中原本的舊值比對，找出有價格調整的股票。

        Returns:
            重新下載的股票數量
        """
        fresh_prices = getattr(self, "_fresh_prev_day_prices", {})
        if not fresh_prices:
            logger.info("無前一交易日的 fresh 資料，跳過分割偵測")
            return 0

        prev_trading_day = USMarketCalendar.get_previous_trading_day(target_date)
        if not prev_trading_day:
            return 0

        # 使用 _db_prev_day_prices（在 _fetch_and_save_prices 中於 upsert 前讀取）
        db_prices = getattr(self, "_db_prev_day_prices", {})
        if not db_prices:
            logger.info("DB 中無前一交易日的舊資料，跳過分割偵測")
            return 0

        # 偵測有價格調整的股票（方法 1：前一日 DB vs fresh 比對）
        adjusted_stocks = USSplitDetector.detect_adjusted_stocks(db_prices, fresh_prices)

        # 方法 2：DB 有前一日資料但 yfinance 沒回傳前一日的股票
        # 用今日價格 vs DB 前一日比對（抓 yfinance 刪除歷史的情況，如 NINE 合股）
        today_prices = {}
        import sqlite3
        conn = sqlite3.connect(self.db.db_path)
        rows = conn.execute(
            "SELECT stock_id, close_price FROM us_daily_price WHERE date = ?",
            (target_date.isoformat(),),
        ).fetchall()
        conn.close()
        today_prices = {r[0]: float(r[1]) for r in rows if r[1]}

        # 方法 2：對 penny stock（< $1）不偵測，避免假警報
        # （penny stock 買賣價差大，每日 30%+ 波動正常，會大量觸發無效重抓）
        MIN_PRICE_FOR_DETECT = 1.0
        missing_from_fresh = set(db_prices.keys()) - set(fresh_prices.keys())
        for stock_id in missing_from_fresh:
            db_prev = db_prices.get(stock_id)
            today_close = today_prices.get(stock_id)
            if (
                db_prev and today_close
                and db_prev >= MIN_PRICE_FOR_DETECT
                and today_close >= MIN_PRICE_FOR_DETECT
            ):
                ratio = today_close / db_prev
                if ratio > 1.5 or ratio < 0.67:
                    if stock_id not in adjusted_stocks:
                        adjusted_stocks.append(stock_id)
                        logger.warning(
                            f"  疑似分割（yfinance 無前日資料）: {stock_id} "
                            f"DB前日={db_prev:.4f} → 今日={today_close:.4f} "
                            f"(比值={ratio:.2f})"
                        )

        if not adjusted_stocks:
            logger.info("未偵測到股票分割/合股，所有價格一致")
            return 0

        logger.warning(
            f"偵測到 {len(adjusted_stocks)} 檔股票有價格調整（疑似分割/合股）: "
            f"{adjusted_stocks[:10]}{'...' if len(adjusted_stocks) > 10 else ''}"
        )

        # 重新下載受影響股票的 365 天完整歷史
        history_start = target_date - timedelta(days=365)
        logger.info(
            f"開始重新下載 {len(adjusted_stocks)} 檔股票的歷史資料 "
            f"({history_start} ~ {target_date})..."
        )

        history_df = self.client.get_stock_price(
            start_date=history_start,
            end_date=target_date,
            stock_ids=adjusted_stocks
        )

        if history_df.empty:
            logger.warning("重新下載歷史資料為空")
            return 0

        # 先刪除 DB 中受影響股票的所有舊資料，再寫入新的
        # 避免分割前的舊價格殘留（yfinance 可能不回傳分割前的歷史）
        import sqlite3
        conn = sqlite3.connect(self.db.db_path)
        for stock_id in adjusted_stocks:
            conn.execute(
                "DELETE FROM us_daily_price WHERE stock_id = ?",
                (stock_id,),
            )
        conn.commit()
        conn.close()
        logger.info(f"已刪除 {len(adjusted_stocks)} 檔股票的舊資料")

        # 寫入新的歷史資料
        count = self.db.upsert_daily_price(history_df)
        logger.info(
            f"已重新下載並更新 {len(adjusted_stocks)} 檔股票的歷史資料 "
            f"(共 {count} 筆)"
        )

        return len(adjusted_stocks)

    def _run_filters(
        self, target_date: date
    ) -> tuple[list[dict], list[dict], list[dict], float]:
        """執行美股篩選

        Returns:
            (vcp_results, sanxian_results, volume_surge_results, market_return_20d)
        """
        logger.info("執行美股篩選...")

        # 取得計算所需的歷史資料（252 天）
        start_date = target_date - timedelta(days=365)
        price_df = self.db.get_daily_prices(start_date, target_date)
        market_df = self.db.get_market_index(start_date, target_date)

        if price_df.empty:
            logger.warning("無足夠美股歷史資料")
            return [], [], [], 0.0

        # 計算 S&P 500 報酬率
        market_return = calculate_us_market_return(market_df, target_date, lookback=20)
        logger.info(f"S&P 500 20 日報酬率: {market_return:.2%}")

        # 取得股票基本資料
        stock_info = self.db.get_stock_info_dict()
        if not stock_info:
            logger.warning("美股股票基本資料為空，請先執行 'python us_main.py init'")

        # 只保留 stock_info 中的股票（過濾掉 ETF 等）
        valid_stock_ids = set(stock_info.keys())
        before_filter = price_df["stock_id"].nunique()
        price_df = price_df[price_df["stock_id"].isin(valid_stock_ids)]
        after_filter = price_df["stock_id"].nunique()
        logger.info(f"過濾美股: {before_filter} -> {after_filter} 檔（排除 ETF 等）")

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
        """補充美股股票基本資料"""
        if df.empty:
            return []

        results = []
        for _, row in df.iterrows():
            stock_id = row["stock_id"]
            info = stock_info.get(stock_id, {})

            result = row.to_dict()
            result.update({
                "stock_name": info.get("stock_name", ""),
                "company_name": info.get("stock_name", ""),
                "exchange": info.get("exchange", "-"),
                "sector": info.get("sector", "-"),
                "industry": info.get("industry", "-"),
                "industry_category": info.get("sector", "-"),  # 相容欄位
                "industry_category2": info.get("industry", "-"),
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
        準備美股 VCP 驗證資料（包含所有計算欄位）
        """
        from calculators.us_moving_average import USMovingAverageCalculator
        from config.us_settings import US_VCP_PARAMS

        if price_df.empty:
            return []

        # 準備計算資料
        df = USMovingAverageCalculator.prepare_vcp_data(price_df)
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
        df["cond6"] = close > df["low_250d"] * US_VCP_PARAMS["low_250d_mult"]

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
        準備美股三線開花驗證資料（包含所有計算欄位）
        """
        from calculators.us_moving_average import USMovingAverageCalculator

        if price_df.empty:
            return []

        # 準備計算資料
        df = USMovingAverageCalculator.prepare_sanxian_data(price_df)
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
        recent_days = USMarketCalendar.get_trading_days_in_range(start, end)[-lookback:]

        recent_ids: set = set()
        for d in recent_days:
            try:
                df = self.db.get_filter_results(filter_type, d)
                if not df.empty:
                    recent_ids.update(df["stock_id"].tolist())
            except Exception as e:
                logger.warning(f"取得 {d} 美股 {filter_type} 結果失敗: {e}")
        return recent_ids

    def _export_to_sheet(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
        volume_surge_results: list[dict],
        market_return: float = 0.0
    ):
        """匯出至美股 Google Sheet"""
        if not self.exporter.health_check():
            logger.warning("美股 Google Sheet 未連線，跳過匯出")
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
                from config.us_settings import US_SHEET_IDS
                from utils.verification_cleaner import cleanup_verification_tabs

                vsheet_id = US_SHEET_IDS.get("verification")
                if vsheet_id and self.exporter.client:
                    ss = self.exporter.client.open_by_key(vsheet_id)
                    cleanup_verification_tabs(ss, keep_days=10)
            except Exception as e:
                logger.warning(f"驗證分頁清理失敗（不影響主流程）: {e}")


def run_us_daily_task(target_date: Optional[date] = None) -> dict:
    """
    執行美股每日任務的便捷函數

    Args:
        target_date: 目標日期

    Returns:
        執行結果
    """
    task = USDailyTask()
    return task.run(target_date)
