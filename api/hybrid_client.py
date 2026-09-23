"""
混合 API 客戶端（含備援機制）

整合 FinMind + yfinance 的優點，並提供自動備援：
- 主要來源失敗時自動切換到備援來源
- 資料不完整時觸發備援
"""
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from loguru import logger

from api.finmind_client import FinMindClient
from api.yfinance_client import YFinanceClient


class HybridClient:
    """
    混合 API 客戶端（含備援機制）

    備援策略:
    - get_stock_info(): 主要 FinMind，備援 yfinance
    - get_stock_price(): 主要 FinMind，備援 yfinance
    - get_market_index(): 主要 FinMind，備援 yfinance
    """

    # 備援觸發閾值
    MIN_STOCK_COUNT = 1000  # 股票清單最少應有 1000 檔
    MIN_PRICE_RATIO = 0.5   # 股價資料成功率至少 50%

    def __init__(self):
        """初始化混合客戶端"""
        self._finmind = FinMindClient()
        self._yfinance = YFinanceClient()
        self._fallback_log: list[dict] = []

        logger.info("HybridClient 初始化完成 (含備援機制)")

    def _log_fallback(self, method: str, primary: str, fallback: str, reason: str):
        """記錄備援事件"""
        event = {
            "time": datetime.now().isoformat(),
            "method": method,
            "primary": primary,
            "fallback": fallback,
            "reason": reason,
        }
        self._fallback_log.append(event)
        logger.warning(f"[備援] {method}: {primary} -> {fallback}, 原因: {reason}")

    def get_stock_info(self) -> pd.DataFrame:
        """
        取得台股股票清單

        備援策略:
        - 主要: FinMind（資料完整、有產業分類）
        - 備援: yfinance（從 TWSE 網站爬取）

        觸發備援條件:
        - API 錯誤
        - 資料為空
        - 資料量 < 1000 檔

        Returns:
            DataFrame with columns:
            - stock_id: 股票代號
            - stock_name: 股票名稱
            - industry_category: 產業分類
            - type: 股票類型
        """
        # 主要來源: FinMind
        logger.info("[主要] 使用 FinMind 取得股票清單...")
        try:
            df = self._finmind.get_stock_info()

            if df.empty:
                self._log_fallback("get_stock_info", "FinMind", "yfinance", "資料為空")
            elif len(df) < self.MIN_STOCK_COUNT:
                self._log_fallback(
                    "get_stock_info", "FinMind", "yfinance",
                    f"資料不完整 ({len(df)} < {self.MIN_STOCK_COUNT})"
                )
            else:
                logger.info(f"[主要] FinMind 取得 {len(df)} 檔股票")
                return df

        except Exception as e:
            self._log_fallback("get_stock_info", "FinMind", "yfinance", f"API 錯誤: {e}")

        # 備援來源: yfinance
        logger.info("[備援] 使用 yfinance 取得股票清單...")
        try:
            df = self._yfinance.get_stock_info()
            if not df.empty:
                logger.info(f"[備援] yfinance 取得 {len(df)} 檔股票")
                return df
        except Exception as e:
            logger.error(f"[備援] yfinance 也失敗: {e}")

        # 兩者都失敗
        logger.error("股票清單取得失敗（主要和備援都失敗）")
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
        批量取得股票每日股價（含補齊機制）

        備援策略:
        - 主要: FinMind（資料與券商一致）
        - 備援: yfinance（免費無限制）

        補齊機制:
        1. 主要來源取得資料
        2. 檢查缺失的股票
        3. 用備援來源補齊缺失的股票
        4. 合併結果

        觸發完整備援條件（跳過主要來源）:
        - API 錯誤
        - 資料為空
        - 成功率 < 50%

        Args:
            start_date: 開始日期
            end_date: 結束日期
            stock_ids: 股票代號列表
            market_types: 股票代號對應的市場類型
            retry_count: 重試次數

        Returns:
            DataFrame with columns:
            - date: 日期
            - stock_id: 股票代號
            - open/high/low/close: 價格
            - volume: 成交量
        """
        if not stock_ids:
            logger.warning("未指定股票代號")
            return pd.DataFrame()

        expected_count = len(stock_ids)
        requested_stocks = set(stock_ids)
        primary_df = pd.DataFrame()
        need_full_fallback = False

        # ===== 主要來源: FinMind =====
        logger.info(f"[主要] 使用 FinMind 取得股價資料 ({expected_count} 檔)...")
        try:
            primary_df = self._finmind.get_stock_price(
                start_date=start_date,
                end_date=end_date,
                stock_ids=stock_ids,
                market_types=market_types,
                retry_count=retry_count,
            )

            if primary_df.empty:
                self._log_fallback("get_stock_price", "FinMind", "yfinance", "資料為空")
                need_full_fallback = True
            else:
                # 檢查成功率
                fetched_stocks = set(primary_df["stock_id"].unique())
                success_ratio = len(fetched_stocks) / expected_count

                if success_ratio < self.MIN_PRICE_RATIO:
                    self._log_fallback(
                        "get_stock_price", "FinMind", "yfinance",
                        f"成功率過低 ({len(fetched_stocks)}/{expected_count} = {success_ratio:.1%})"
                    )
                    need_full_fallback = True
                else:
                    logger.info(f"[主要] FinMind 取得 {len(primary_df)} 筆股價 ({len(fetched_stocks)} 檔)")

        except Exception as e:
            self._log_fallback("get_stock_price", "FinMind", "yfinance", f"API 錯誤: {e}")
            need_full_fallback = True

        # ===== 完整備援模式（主要來源完全失敗）=====
        if need_full_fallback:
            logger.info(f"[備援] 使用 yfinance 取得全部股價資料...")
            try:
                fallback_df = self._yfinance.get_stock_price(
                    start_date=start_date,
                    end_date=end_date,
                    stock_ids=stock_ids,
                    market_types=market_types,
                    retry_count=retry_count,
                )
                if not fallback_df.empty:
                    unique_stocks = fallback_df["stock_id"].nunique()
                    logger.info(f"[備援] yfinance 取得 {len(fallback_df)} 筆股價 ({unique_stocks} 檔)")
                    return fallback_df
            except Exception as e:
                logger.error(f"[備援] yfinance 也失敗: {e}")

            logger.error("股價資料取得失敗（主要和備援都失敗）")
            return pd.DataFrame()

        # ===== 補齊模式（主要來源部分成功）=====
        fetched_stocks = set(primary_df["stock_id"].unique())
        missing_stocks = requested_stocks - fetched_stocks

        if not missing_stocks:
            # 沒有缺失，直接返回
            return primary_df

        # 有缺失，嘗試用備援來源補齊
        logger.info(f"[補齊] 發現 {len(missing_stocks)} 檔缺失，使用 yfinance 補齊...")
        logger.debug(f"[補齊] 缺失股票: {sorted(missing_stocks)[:10]}{'...' if len(missing_stocks) > 10 else ''}")

        try:
            # 只請求缺失的股票
            missing_market_types = {
                sid: market_types.get(sid, "twse")
                for sid in missing_stocks
            } if market_types else None

            fill_df = self._yfinance.get_stock_price(
                start_date=start_date,
                end_date=end_date,
                stock_ids=list(missing_stocks),
                market_types=missing_market_types,
                retry_count=retry_count,
            )

            if not fill_df.empty:
                fill_count = fill_df["stock_id"].nunique()
                logger.info(f"[補齊] yfinance 補齊 {len(fill_df)} 筆股價 ({fill_count} 檔)")

                # 合併主要來源和補齊資料
                result_df = pd.concat([primary_df, fill_df], ignore_index=True)

                # 記錄補齊事件
                self._log_fallback(
                    "get_stock_price_fill", "FinMind", "yfinance",
                    f"補齊 {fill_count}/{len(missing_stocks)} 檔缺失資料"
                )

                final_count = result_df["stock_id"].nunique()
                logger.info(f"[結果] 合併後共 {len(result_df)} 筆股價 ({final_count} 檔)")
                return result_df
            else:
                logger.warning(f"[補齊] yfinance 無法補齊缺失資料")

        except Exception as e:
            logger.warning(f"[補齊] yfinance 補齊失敗: {e}")

        # 補齊失敗，返回主要來源的資料（部分資料總比沒有好）
        still_missing = len(missing_stocks)
        logger.warning(f"[結果] 返回主要來源資料，仍有 {still_missing} 檔缺失")
        return primary_df

    def get_market_index(
        self,
        start_date: date,
        end_date: Optional[date] = None,
        retry_count: int = 3,
    ) -> pd.DataFrame:
        """
        取得大盤指數

        備援策略:
        - 主要: FinMind（TaiwanStockTotalReturnIndex）
        - 備援: yfinance（^TWII 台灣加權指數）

        觸發備援條件:
        - API 錯誤
        - 資料為空

        Args:
            start_date: 開始日期
            end_date: 結束日期
            retry_count: 重試次數

        Returns:
            DataFrame with columns:
            - date: 日期
            - taiex: 加權指數
        """
        # 主要來源: FinMind
        logger.info("[主要] 使用 FinMind 取得大盤指數...")
        try:
            df = self._finmind.get_market_index(
                start_date=start_date,
                end_date=end_date,
                retry_count=retry_count,
            )

            if df.empty:
                self._log_fallback("get_market_index", "FinMind", "yfinance", "資料為空")
            else:
                logger.info(f"[主要] FinMind 取得 {len(df)} 筆大盤指數")
                return df

        except Exception as e:
            self._log_fallback("get_market_index", "FinMind", "yfinance", f"API 錯誤: {e}")

        # 備援來源: yfinance
        logger.info("[備援] 使用 yfinance 取得大盤指數...")
        try:
            df = self._yfinance.get_market_index(
                start_date=start_date,
                end_date=end_date,
                retry_count=retry_count,
            )
            if not df.empty:
                logger.info(f"[備援] yfinance 取得 {len(df)} 筆大盤指數")
                return df
        except Exception as e:
            logger.error(f"[備援] yfinance 也失敗: {e}")

        # 兩者都失敗
        logger.error("大盤指數取得失敗（主要和備援都失敗）")
        return pd.DataFrame()

    def get_latest_trading_date(
        self, ref_stock: str = "2330", lookback_days: int = 14
    ) -> Optional[date]:
        """查詢資料源實際的最新交易日。

        由資料源決定，能自動跳過颱風假、臨時休市等「交易日曆不知道」的休市日，
        且不依賴 date.today()，排程延遲也不影響。

        Args:
            ref_stock: FinMind 單檔查詢用的指標股（預設台積電 2330）。
                yfinance 備援不用它——備援走多檔投票（見 YFinanceClient）。
            lookback_days: 往前查幾個日曆天（需涵蓋連假，預設 14）

        Returns:
            最新交易日；兩個來源都查不到回傳 None（呼叫端應退回交易日曆）

        Warning:
            兩個資料源各有一種「壞得不誠實」的方式，這裡的寫法都是踩坑換來的：

            1. FinMind 必須用 get_latest_trading_date()（帶 data_id 單檔查），
               不能走 get_stock_price()：後者不帶 data_id、一律拉全市場，而台股
               全市場單日就約 4 萬筆、已逼近 FinMind 單次上限，只要查超過一天就
               會被「靜默截斷」（仍回 success，卻只給區間第一天）。2026-07-14
               就因此把最新交易日誤判成 6/30、拿兩週前的資料跑篩選。

            2. yfinance 會為「休市日」捏造資料：2026-07-10 台股因颱風全日休市，
               yfinance 卻給了該日 OHLC（全部等於 7/09 收盤價）。2026-07-13 曾
               因此被騙去抓「7/10」，把整批假股價寫進 DB。
               → 破綻是 **成交量 = 0**（休市日不可能有成交），所以 yfinance 備援
                 只採信 Volume > 0 的日期。真實交易日即使 Close 還是 NaN
                 （盤中/未結算），Volume 仍然 > 0。
        """
        # 主要來源：FinMind（單檔查詢，能正確反映颱風/臨時休市）
        try:
            latest = self._finmind.get_latest_trading_date(
                ref_stock=ref_stock, lookback_days=lookback_days
            )
            if latest:
                return latest
            self._log_fallback(
                "get_latest_trading_date", "FinMind", "yfinance", "查無資料"
            )
        except Exception as e:
            self._log_fallback(
                "get_latest_trading_date", "FinMind", "yfinance", f"API 錯誤: {e}"
            )

        # 備援：yfinance（多檔指標股投票，並濾掉 Volume=0 的假交易日，見上方 Warning）
        try:
            latest = self._yfinance.get_latest_trading_date(
                lookback_days=lookback_days
            )
            if latest:
                logger.info(f"[備援] yfinance 最新交易日（已濾除休市日）= {latest}")
                return latest
        except Exception as e:
            logger.error(f"[備援] yfinance 查詢最新交易日也失敗: {e}")

        return None

    def get_stats(self) -> dict:
        """取得客戶端統計資訊"""
        finmind_stats = self._finmind.get_stats()
        yfinance_stats = self._yfinance.get_stats()

        return {
            "finmind": finmind_stats,
            "yfinance": yfinance_stats,
            "total_requests": (
                finmind_stats.get("total_requests", 0) +
                yfinance_stats.get("total_requests", 0)
            ),
            "error_count": (
                finmind_stats.get("error_count", 0) +
                yfinance_stats.get("error_count", 0)
            ),
            "fallback_count": len(self._fallback_log),
        }

    def get_error_log(self) -> list[dict]:
        """取得錯誤日誌（合併兩個客戶端）"""
        finmind_errors = self._finmind.get_error_log()
        yfinance_errors = self._yfinance.get_error_log()

        # 標記來源
        for err in finmind_errors:
            err["source"] = "finmind"
        for err in yfinance_errors:
            err["source"] = "yfinance"

        return finmind_errors + yfinance_errors

    def get_fallback_log(self) -> list[dict]:
        """取得備援事件日誌"""
        return self._fallback_log.copy()
