"""
每日自動驗證模組

每日篩選完成後自動執行，驗證結果正確性。
支援台股和美股，產出簡潔的 pass/fail 報告。

驗證項目：
  1. 股價筆數 — 今天抓到的股價 ≥ 預期數量
  2. 篩選結果非空 — VCP + 三線開花都有結果
  3. 即時重算比對 — 用同一份資料重算，結果必須 100% 一致
  4. 指標值合理性 — return_20d、gap_ratio 在合理範圍
  5. 新/舊標記 — 新 + 舊 = 總數
  6. 價格抽樣比對 — 隨機 10 檔跟 yfinance 比對
"""
import random
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger


class VerifyResult:
    """單一驗證項目的結果"""

    def __init__(self, name: str, passed: bool, detail: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        msg = f"[{status}] {self.name}"
        if self.detail:
            msg += f": {self.detail}"
        return msg


class DailyVerifier:
    """每日自動驗證器"""

    def __init__(
        self,
        db,
        market: str = "tw",
        min_price_count: int = 1500,
    ):
        """
        Args:
            db: Database instance (SQLiteDatabase or USSQLiteDatabase)
            market: "tw" or "us"
            min_price_count: 預期最少股價筆數
        """
        self.db = db
        self.market = market
        self.min_price_count = min_price_count
        self.results: list[VerifyResult] = []

    def verify_all(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
        price_count: int,
        market_return: float,
    ) -> bool:
        """
        執行所有驗證項目

        Args:
            target_date: 篩選日期
            vcp_results: VCP 篩選結果
            sanxian_results: 三線開花篩選結果
            price_count: 今天抓到的股價筆數
            market_return: 大盤 20 日報酬率

        Returns:
            True if all passed
        """
        self.results = []
        market_label = "台股" if self.market == "tw" else "美股"
        logger.info(f"=== {market_label} 每日驗證開始: {target_date} ===")

        # 1. 股價筆數
        self._check_price_count(price_count)

        # 2. 篩選結果非空
        self._check_filter_not_empty(vcp_results, sanxian_results)

        # 3. 即時重算比對
        self._check_recalculation(target_date, vcp_results, sanxian_results, market_return)

        # 4. 指標值合理性
        self._check_value_sanity(vcp_results, sanxian_results)

        # 5. 新/舊標記
        self._check_new_old_logic(target_date, vcp_results, sanxian_results)

        # 6. 價格抽樣比對
        self._check_price_sampling(target_date)

        # 輸出報告
        return self._print_report(target_date)

    # ==================== 1. 股價筆數 ====================

    def _check_price_count(self, price_count: int):
        if price_count >= self.min_price_count:
            self.results.append(VerifyResult(
                "股價筆數",
                True,
                f"{price_count:,} 筆 (≥ {self.min_price_count:,})",
            ))
        else:
            self.results.append(VerifyResult(
                "股價筆數",
                False,
                f"{price_count:,} 筆 (< {self.min_price_count:,})",
            ))

    # ==================== 2. 篩選結果非空 ====================

    def _check_filter_not_empty(self, vcp_results, sanxian_results):
        vcp_ok = len(vcp_results) > 0
        san_ok = len(sanxian_results) > 0

        self.results.append(VerifyResult(
            "篩選結果",
            vcp_ok and san_ok,
            f"VCP {len(vcp_results)} 檔, 三線 {len(sanxian_results)} 檔",
        ))

    # ==================== 3. 即時重算比對 ====================

    def _check_recalculation(self, target_date, vcp_results, sanxian_results, market_return):
        """用同一份資料重算，結果必須一致"""
        try:
            start_date = target_date - timedelta(days=365)
            price_df = self.db.get_daily_prices(start_date, target_date)

            if price_df.empty:
                self.results.append(VerifyResult(
                    "即時重算", False, "無歷史股價資料",
                ))
                return

            stock_info = self.db.get_stock_info_dict()
            valid_ids = set(stock_info.keys())
            price_df = price_df[price_df["stock_id"].isin(valid_ids)]

            # 重算 VCP
            if self.market == "tw":
                from calculators.vcp_filter import VCPFilter
                from calculators.sanxian_filter import SanxianFilter
                vcp_filter = VCPFilter()
                sanxian_filter = SanxianFilter()
            else:
                from calculators.us_vcp_filter import USVCPFilter
                from calculators.us_sanxian_filter import USSanxianFilter
                vcp_filter = USVCPFilter()
                sanxian_filter = USSanxianFilter()

            recalc_vcp = vcp_filter.filter(price_df, market_return, target_date)
            recalc_san = sanxian_filter.filter(price_df, target_date)

            recalc_vcp_ids = set(recalc_vcp["stock_id"].tolist()) if not recalc_vcp.empty else set()
            recalc_san_ids = set(recalc_san["stock_id"].tolist()) if not recalc_san.empty else set()

            orig_vcp_ids = {r["stock_id"] for r in vcp_results}
            orig_san_ids = {r["stock_id"] for r in sanxian_results}

            vcp_diff = (recalc_vcp_ids ^ orig_vcp_ids)
            san_diff = (recalc_san_ids ^ orig_san_ids)

            if not vcp_diff and not san_diff:
                self.results.append(VerifyResult(
                    "即時重算", True, "100% 一致",
                ))
            else:
                details = []
                if vcp_diff:
                    details.append(f"VCP 差異 {len(vcp_diff)} 檔: {list(vcp_diff)[:5]}")
                if san_diff:
                    details.append(f"三線 差異 {len(san_diff)} 檔: {list(san_diff)[:5]}")
                self.results.append(VerifyResult(
                    "即時重算", False, "; ".join(details),
                ))

        except Exception as e:
            self.results.append(VerifyResult(
                "即時重算", False, f"重算失敗: {e}",
            ))

    # ==================== 4. 指標值合理性 ====================

    def _check_value_sanity(self, vcp_results, sanxian_results):
        """檢查 return_20d 和 gap_ratio 是否在合理範圍"""
        issues = []

        # VCP: return_20d
        for r in vcp_results:
            val = r.get("return_20d")
            if val is not None and abs(val) > 5.0:
                issues.append(f"{r['stock_id']} return_20d={val:.2%}")

        # 三線: gap_ratio
        for r in sanxian_results:
            val = r.get("gap_ratio")
            if val is not None and abs(val) > 2.0:
                issues.append(f"{r['stock_id']} gap_ratio={val:.2%}")

        if not issues:
            self.results.append(VerifyResult("指標合理性", True, "無異常"))
        else:
            self.results.append(VerifyResult(
                "指標合理性",
                False,
                f"{len(issues)} 筆異常: {issues[:3]}{'...' if len(issues) > 3 else ''}",
            ))

    # ==================== 5. 新/舊標記 ====================

    def _check_new_old_logic(self, target_date, vcp_results, sanxian_results):
        """新 + 舊 = 總數"""
        try:
            if self.market == "tw":
                from utils.trading_calendar import TradingCalendar
                prev_date = TradingCalendar.get_previous_trading_day(target_date)
            else:
                from utils.us_trading_calendar import USMarketCalendar
                prev_date = USMarketCalendar.get_previous_trading_day(target_date)

            if not prev_date:
                self.results.append(VerifyResult(
                    "新/舊標記", True, "無前一交易日，跳過",
                ))
                return

            # 取得前一天的 stock_ids（合併 VCP + 三線）
            import sqlite3
            if self.market == "tw":
                from config.settings import SQLITE_DB_PATH
                db_path = SQLITE_DB_PATH
                table = "filter_result"
            else:
                from config.us_settings import US_SQLITE_DB_PATH
                db_path = US_SQLITE_DB_PATH
                table = "us_filter_result"

            conn = sqlite3.connect(db_path)
            prev_ids = set(
                r[0] for r in conn.execute(
                    f"SELECT DISTINCT stock_id FROM {table} WHERE filter_date = ?",
                    (prev_date.isoformat(),),
                )
            )
            conn.close()

            # 今天的合併 stock_ids
            curr_ids = {r["stock_id"] for r in vcp_results} | {r["stock_id"] for r in sanxian_results}
            new_count = len(curr_ids - prev_ids)
            old_count = len(curr_ids & prev_ids)
            total = len(curr_ids)

            if new_count + old_count == total:
                self.results.append(VerifyResult(
                    "新/舊標記", True,
                    f"新 {new_count} + 舊 {old_count} = 總 {total}",
                ))
            else:
                self.results.append(VerifyResult(
                    "新/舊標記", False,
                    f"新 {new_count} + 舊 {old_count} ≠ 總 {total}",
                ))

        except Exception as e:
            self.results.append(VerifyResult(
                "新/舊標記", False, f"檢查失敗: {e}",
            ))

    # ==================== 6. 價格抽樣比對 ====================

    def _check_price_sampling(self, target_date: date, sample_size: int = 10):
        """隨機 N 檔跟 yfinance 比對"""
        try:
            price_df = self.db.get_daily_prices(target_date, target_date)
            if price_df.empty:
                self.results.append(VerifyResult(
                    "價格抽樣", False, "無當日股價資料",
                ))
                return

            all_stocks = price_df["stock_id"].unique().tolist()

            # 建立 yfinance 代號（台股優先抽上市股票，成功率較高）
            if self.market == "tw":
                market_types = self.db.get_stock_market_types()
                listed = [s for s in all_stocks if market_types.get(s, "上市") in ("上市", "TWSE")]
                pool = listed if len(listed) >= sample_size else all_stocks
                sampled = random.sample(pool, min(sample_size, len(pool)))
                yf_map = {}
                for sid in sampled:
                    mtype = market_types.get(sid, "上市")
                    suffix = ".TW" if mtype in ("上市", "TWSE") else ".TWO"
                    yf_map[f"{sid}{suffix}"] = sid
            else:
                sampled = random.sample(all_stocks, min(sample_size, len(all_stocks)))
                yf_map = {sid: sid for sid in sampled}

            # 下載 yfinance 資料
            tickers = " ".join(yf_map.keys())
            end_date = target_date + timedelta(days=1)
            yf_df = yf.download(
                tickers, start=target_date.isoformat(),
                end=end_date.isoformat(), progress=False,
                auto_adjust=False, threads=True,
            )

            if yf_df is None or yf_df.empty:
                self.results.append(VerifyResult(
                    "價格抽樣", True, "yfinance 無資料（可能假日），跳過",
                ))
                return

            # 比對
            match_count = 0
            mismatch_details = []
            db_map = dict(zip(price_df["stock_id"], price_df["close_price"]))

            for yf_sym, sid in yf_map.items():
                db_close = db_map.get(sid)
                if db_close is None:
                    continue

                try:
                    if isinstance(yf_df.columns, pd.MultiIndex):
                        yf_close = float(yf_df[(yf_sym, "Close")].dropna().iloc[-1])
                    else:
                        yf_close = float(yf_df["Close"].dropna().iloc[-1])
                except (KeyError, IndexError):
                    continue

                diff = abs(db_close - yf_close) / yf_close if yf_close > 0 else 0
                if diff < 0.02:
                    match_count += 1
                else:
                    mismatch_details.append(
                        f"{sid}: DB={db_close:.2f} vs YF={yf_close:.2f} ({diff:.1%})"
                    )

            total_compared = match_count + len(mismatch_details)
            if total_compared == 0:
                self.results.append(VerifyResult(
                    "價格抽樣", True, "無可比對資料，跳過",
                ))
            elif not mismatch_details:
                self.results.append(VerifyResult(
                    "價格抽樣", True,
                    f"{match_count}/{total_compared} 檔 < 2% 差異",
                ))
            else:
                self.results.append(VerifyResult(
                    "價格抽樣", False,
                    f"{match_count}/{total_compared} 通過, "
                    f"差異: {mismatch_details[:3]}",
                ))

        except Exception as e:
            self.results.append(VerifyResult(
                "價格抽樣", True, f"比對跳過: {e}",
            ))

    # ==================== 報告 ====================

    def _print_report(self, target_date: date) -> bool:
        """輸出驗證報告"""
        market_label = "台股" if self.market == "tw" else "美股"
        logger.info("")
        logger.info(f"=== {market_label} {target_date} 每日驗證報告 ===")

        all_passed = True
        for r in self.results:
            if r.passed:
                logger.info(str(r))
            else:
                logger.error(str(r))
                all_passed = False

        if all_passed:
            logger.info(f"結論: 全部通過 ✓")
        else:
            failed = [r for r in self.results if not r.passed]
            logger.error(f"結論: {len(failed)} 項未通過 ✗")

        logger.info("=" * 50)
        return all_passed

    def get_report_text(self, target_date: date) -> str:
        """產出純文字報告（可寫入 Sheet 或 log）"""
        market_label = "台股" if self.market == "tw" else "美股"
        lines = [f"{market_label} {target_date} 驗證報告"]
        for r in self.results:
            lines.append(str(r))

        all_passed = all(r.passed for r in self.results)
        lines.append(f"結論: {'全部通過 ✓' if all_passed else '有未通過項目 ✗'}")
        return "\n".join(lines)
