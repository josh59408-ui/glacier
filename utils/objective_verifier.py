"""
客觀驗證模組

用獨立資料來源（yfinance）驗證篩選結果的正確性，
並讀回 Google Sheet 確認匯出完整。

四層驗證：
  L1. 價格準確性 — DB 收盤價 vs yfinance 獨立抓取
  L2. 獨立重算 — 從 yfinance 原始資料重新計算 MA/指標
  L3. Sheet 回讀 — 讀回 Google Sheet 比對行數和數值
  L4. 歷史一致性 — 篩選數量趨勢異常偵測
"""

import json
import random
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import gspread
import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger


@dataclass(frozen=True)
class LayerResult:
    """單一驗證層的結果"""

    name: str
    passed: bool
    summary: str
    detail: dict = field(default_factory=dict)


class ObjectiveVerifier:
    """客觀驗證器"""

    def __init__(self, db, market: str = "tw"):
        self.db = db
        self.market = market
        self.label = "台股" if market == "tw" else "美股"

    def verify_all(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
        market_return: float,
        exporter=None,
    ) -> dict:
        """
        執行四層客觀驗證

        Returns:
            {"passed": bool, "layers": [...], "report": str}
        """
        logger.info(f"=== [{self.label}] 開始客觀驗證 {target_date} ===")

        layers = []

        # L1: 價格準確性
        try:
            l1 = self._verify_price_accuracy(
                target_date, vcp_results, sanxian_results
            )
            layers.append(l1)
        except Exception as e:
            logger.warning(f"L1 價格準確性驗證失敗: {e}")
            layers.append(
                LayerResult("L1 價格準確性", False, f"執行失敗: {e}")
            )

        # L2: 獨立重算
        try:
            l2 = self._verify_independent_calc(
                target_date, vcp_results, sanxian_results, market_return
            )
            layers.append(l2)
        except Exception as e:
            logger.warning(f"L2 獨立重算驗證失敗: {e}")
            layers.append(
                LayerResult("L2 獨立重算", False, f"執行失敗: {e}")
            )

        # L3: Sheet 回讀
        try:
            l3 = self._verify_sheet_roundtrip(
                target_date, vcp_results, sanxian_results, exporter
            )
            layers.append(l3)
        except Exception as e:
            logger.warning(f"L3 Sheet 回讀驗證失敗: {e}")
            layers.append(
                LayerResult("L3 Sheet 回讀", False, f"執行失敗: {e}")
            )

        # L4: 歷史一致性
        try:
            l4 = self._verify_historical_consistency(
                target_date, vcp_results, sanxian_results
            )
            layers.append(l4)
        except Exception as e:
            logger.warning(f"L4 歷史一致性驗證失敗: {e}")
            layers.append(
                LayerResult("L4 歷史一致性", False, f"執行失敗: {e}")
            )

        # 產出報告
        all_passed = all(lr.passed for lr in layers)
        report = self._format_report(target_date, layers, all_passed)
        logger.info(report)

        # 寫入驗證 Sheet
        if exporter:
            try:
                self._write_to_sheet(
                    target_date, layers, all_passed, exporter
                )
            except Exception as e:
                logger.warning(f"寫入驗證 Sheet 失敗: {e}")

        return {
            "passed": all_passed,
            "layers": [
                {
                    "name": lr.name,
                    "passed": lr.passed,
                    "summary": lr.summary,
                }
                for lr in layers
            ],
            "report": report,
        }

    # ==================== L1: 價格準確性 ====================

    def _verify_price_accuracy(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
    ) -> LayerResult:
        """L1: 從 yfinance 獨立抓價格，比對 DB 收盤價"""
        all_stocks = list(
            {r["stock_id"] for r in vcp_results}
            | {r["stock_id"] for r in sanxian_results}
        )
        if not all_stocks:
            return LayerResult(
                "L1 價格準確性", True, "無篩選結果，跳過"
            )

        sample_size = min(15, len(all_stocks))
        sampled = random.sample(all_stocks, sample_size)

        # 從 DB 取收盤價
        db_prices = self._get_db_prices(target_date, sampled)

        # 從 yfinance 獨立抓
        yf_prices = self._get_yf_prices(target_date, sampled)

        if not yf_prices:
            return LayerResult(
                "L1 價格準確性", False, "yfinance 無法取得價格"
            )

        # 比對
        mismatches = []
        matched = 0
        for stock_id in sampled:
            db_p = db_prices.get(stock_id)
            yf_p = yf_prices.get(stock_id)
            if db_p is None or yf_p is None or yf_p == 0:
                continue

            diff = abs(db_p - yf_p) / yf_p
            if diff > 0.02:
                mismatches.append(
                    {
                        "stock_id": stock_id,
                        "db": round(db_p, 2),
                        "yf": round(yf_p, 2),
                        "diff_pct": round(diff * 100, 2),
                    }
                )
            else:
                matched += 1

        total_compared = matched + len(mismatches)
        passed = len(mismatches) <= 2
        summary = f"{matched}/{total_compared} 檔 < 2%"
        if mismatches:
            summary += f", {len(mismatches)} 檔偏差過大"

        return LayerResult(
            "L1 價格準確性",
            passed,
            summary,
            {"mismatches": mismatches},
        )

    # ==================== L2: 獨立重算 ====================

    def _verify_independent_calc(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
        market_return: float,
    ) -> LayerResult:
        """L2: 從 yfinance 下載原始資料，獨立計算 MA 和條件"""
        mismatches = []
        matched = 0

        # 抽 VCP 3 檔
        vcp_sample = random.sample(
            vcp_results, min(3, len(vcp_results))
        ) if vcp_results else []
        for r in vcp_sample:
            result = self._independent_vcp_check(
                r["stock_id"], target_date, market_return
            )
            if result is None:
                continue
            if result["match"]:
                matched += 1
            else:
                mismatches.append(result)
            time.sleep(0.3)

        # 抽三線 2 檔
        sanxian_sample = random.sample(
            sanxian_results, min(2, len(sanxian_results))
        ) if sanxian_results else []
        for r in sanxian_sample:
            result = self._independent_sanxian_check(
                r["stock_id"], target_date
            )
            if result is None:
                continue
            if result["match"]:
                matched += 1
            else:
                mismatches.append(result)
            time.sleep(0.3)

        total = matched + len(mismatches)
        if total == 0:
            return LayerResult(
                "L2 獨立重算", True, "無法取得資料，跳過"
            )

        passed = len(mismatches) == 0
        summary = f"{matched}/{total} 檔決策一致"
        if mismatches:
            summary += f", {len(mismatches)} 檔不一致"

        return LayerResult(
            "L2 獨立重算",
            passed,
            summary,
            {"mismatches": mismatches},
        )

    def _independent_vcp_check(
        self, stock_id: str, target_date: date, market_return: float
    ) -> Optional[dict]:
        """獨立計算 VCP 條件（不用專案的 calculator）"""
        ticker_str = self._to_yf_ticker(stock_id)
        try:
            hist = yf.Ticker(ticker_str).history(
                start=target_date - timedelta(days=400),
                end=target_date + timedelta(days=1),
            )
        except Exception:
            return None

        if hist.empty or len(hist) < 200:
            return None

        hist = hist.sort_index()
        close = hist["Close"]

        # 獨立計算 MA
        ma50 = close.rolling(50).mean().iloc[-1]
        ma150 = close.rolling(150).mean().iloc[-1]
        ma200 = close.rolling(200).mean().iloc[-1]
        today_close = close.iloc[-1]

        # 條件 1: 多頭排列
        cond1 = (
            today_close > ma50 and ma50 > ma150 and ma150 > ma200
        )

        # 條件 2: MA200 向上
        ma200_series = close.rolling(200).mean()
        ma200_20ago = (
            ma200_series.iloc[-21] if len(ma200_series) >= 21 else None
        )
        cond2 = (
            ma200_20ago is not None
            and not np.isnan(ma200_20ago)
            and ma200_series.iloc[-1] > ma200_20ago
        )

        # 條件 3: 打敗大盤
        return_20d = (
            (close.iloc[-1] / close.iloc[-21] - 1)
            if len(close) >= 21
            else 0
        )
        cond3 = return_20d > market_return

        # 條件 4: 離 250 日盤中最低點 ≥ 30%（收盤 > low_250d × 1.30，強勢/新高共用）
        from config.settings import VCP_PARAMS
        from config.us_settings import US_VCP_PARAMS

        low_mult = (
            US_VCP_PARAMS["low_250d_mult"]
            if self.market == "us"
            else VCP_PARAMS["low_250d_mult"]
        )
        low = hist["Low"]
        low250 = low.iloc[-250:].min() if len(low) >= 1 else None
        cond4 = (
            low250 is not None
            and not np.isnan(low250)
            and today_close > low250 * low_mult
        )

        is_strong = cond1 and cond2 and cond3 and cond4

        # 新高條件：近 5 日最高價 == 近 250 交易日最高價（250 日高點落在最近 5 日內）
        high = hist["High"]
        h5 = high.iloc[-5:].max() if len(high) >= 1 else None
        h250 = high.iloc[-250:].max() if len(high) >= 1 else None
        is_new_high = (
            h5 is not None
            and h250 is not None
            and not np.isnan(h5)
            and not np.isnan(h250)
            and h5 >= h250
            and cond3
            and cond4
        )

        should_pass = is_strong or is_new_high
        # 篩選結果中有這檔 → 系統判定通過
        system_passed = True

        return {
            "stock_id": stock_id,
            "type": "vcp",
            "match": should_pass == system_passed,
            "independent": should_pass,
            "system": system_passed,
            "detail": {
                "cond1": cond1,
                "cond2": cond2,
                "cond3": cond3,
                "cond4": cond4,
                "is_strong": is_strong,
                "is_new_high": is_new_high,
            },
        }

    def _independent_sanxian_check(
        self, stock_id: str, target_date: date
    ) -> Optional[dict]:
        """獨立計算三線開花條件"""
        ticker_str = self._to_yf_ticker(stock_id)
        try:
            hist = yf.Ticker(ticker_str).history(
                start=target_date - timedelta(days=120),
                end=target_date + timedelta(days=1),
            )
        except Exception:
            return None

        if hist.empty or len(hist) < 55:
            return None

        close = hist["Close"].sort_index()

        ma8 = close.rolling(8).mean().iloc[-1]
        ma21 = close.rolling(21).mean().iloc[-1]
        ma55 = close.rolling(55).mean().iloc[-1]
        today_close = close.iloc[-1]

        cond1 = (
            today_close > ma8 and ma8 > ma21 and ma21 > ma55
        )

        high_55d = close.iloc[-55:].max()
        cond2 = today_close >= high_55d

        should_pass = cond1 and cond2
        system_passed = True

        return {
            "stock_id": stock_id,
            "type": "sanxian",
            "match": should_pass == system_passed,
            "independent": should_pass,
            "system": system_passed,
            "detail": {
                "cond1": cond1,
                "cond2": cond2,
                "close": round(today_close, 2),
                "ma8": round(ma8, 2),
                "ma21": round(ma21, 2),
                "ma55": round(ma55, 2),
            },
        }

    # ==================== L3: Sheet 回讀 ====================

    def _verify_sheet_roundtrip(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
        exporter,
    ) -> LayerResult:
        """L3: 讀回 Google Sheet 比對"""
        if exporter is None or exporter.client is None:
            return LayerResult(
                "L3 Sheet 回讀", True, "無 Sheet 連線，跳過"
            )

        if self.market == "tw":
            from config.settings import SHEET_IDS as sheet_ids
            vcp_key = "tw_vcp"
            sanxian_key = "tw_sanxian"
        else:
            from config.us_settings import US_SHEET_IDS as sheet_ids
            vcp_key = "vcp"
            sanxian_key = "sanxian"

        tab_name = target_date.strftime("%y%m%d")
        issues = []

        # 檢查 VCP Sheet
        vcp_sheet_id = sheet_ids.get(vcp_key)
        if vcp_sheet_id:
            vcp_issue = self._check_sheet_tab(
                exporter, vcp_sheet_id, tab_name,
                len(vcp_results), "VCP"
            )
            if vcp_issue:
                issues.append(vcp_issue)

        # 檢查三線 Sheet
        sanxian_sheet_id = sheet_ids.get(sanxian_key)
        if sanxian_sheet_id:
            sanxian_issue = self._check_sheet_tab(
                exporter, sanxian_sheet_id, tab_name,
                len(sanxian_results), "三線開花"
            )
            if sanxian_issue:
                issues.append(sanxian_issue)

        if not issues:
            vcp_count = len(vcp_results)
            sanxian_count = len(sanxian_results)
            return LayerResult(
                "L3 Sheet 回讀",
                True,
                f"VCP {vcp_count}行 三線 {sanxian_count}行 一致",
            )

        return LayerResult(
            "L3 Sheet 回讀",
            False,
            "; ".join(issues),
            {"issues": issues},
        )

    def _check_sheet_tab(
        self,
        exporter,
        sheet_id: str,
        tab_name: str,
        expected_rows: int,
        label: str,
    ) -> Optional[str]:
        """檢查 Sheet 分頁的行數"""
        try:
            sheet = exporter.client.open_by_key(sheet_id)
            ws = sheet.worksheet(tab_name)
            all_values = ws.get_all_values()
            # 第一行是標題，資料行 = 總行 - 1
            actual_rows = len(all_values) - 1 if all_values else 0

            if actual_rows != expected_rows:
                return (
                    f"{label}: 預期 {expected_rows} 行, "
                    f"實際 {actual_rows} 行"
                )
            return None
        except gspread.WorksheetNotFound:
            return f"{label}: 分頁 {tab_name} 不存在"
        except Exception as e:
            return f"{label}: 讀取失敗 ({e})"

    # ==================== L4: 歷史一致性 ====================

    def _verify_historical_consistency(
        self,
        target_date: date,
        vcp_results: list[dict],
        sanxian_results: list[dict],
    ) -> LayerResult:
        """L4: 比對今日篩選數量 vs 過去 20 天平均"""
        filter_table = (
            "filter_result"
            if self.market == "tw"
            else "us_filter_result"
        )

        import sqlite3

        conn = sqlite3.connect(self.db.db_path)

        # 過去 20 天每天的 VCP 和三線筆數
        rows = conn.execute(
            f"""
            SELECT filter_date, filter_type, COUNT(*) as cnt
            FROM {filter_table}
            WHERE filter_date >= ?
              AND filter_date < ?
            GROUP BY filter_date, filter_type
            """,
            (
                (target_date - timedelta(days=40)).isoformat(),
                target_date.isoformat(),
            ),
        ).fetchall()
        conn.close()

        if not rows:
            return LayerResult(
                "L4 歷史一致性", True, "無歷史資料，跳過"
            )

        # 計算平均
        vcp_counts = [
            r[2] for r in rows if r[1] == "vcp"
        ]
        sanxian_counts = [
            r[2] for r in rows if r[1] == "sanxian"
        ]

        today_vcp = len(vcp_results)
        today_sanxian = len(sanxian_results)

        issues = []
        detail = {}

        if vcp_counts:
            avg_vcp = sum(vcp_counts) / len(vcp_counts)
            detail["vcp_avg_20d"] = round(avg_vcp, 1)
            detail["vcp_today"] = today_vcp
            if avg_vcp > 0 and abs(today_vcp - avg_vcp) / avg_vcp > 0.5:
                issues.append(
                    f"VCP {today_vcp} (均值 {avg_vcp:.0f}, "
                    f"偏差 {abs(today_vcp - avg_vcp) / avg_vcp * 100:.0f}%)"
                )

        if sanxian_counts:
            avg_sanxian = sum(sanxian_counts) / len(sanxian_counts)
            detail["sanxian_avg_20d"] = round(avg_sanxian, 1)
            detail["sanxian_today"] = today_sanxian
            if avg_sanxian > 0 and abs(today_sanxian - avg_sanxian) / avg_sanxian > 0.5:
                issues.append(
                    f"三線 {today_sanxian} (均值 {avg_sanxian:.0f}, "
                    f"偏差 {abs(today_sanxian - avg_sanxian) / avg_sanxian * 100:.0f}%)"
                )

        if issues:
            return LayerResult(
                "L4 歷史一致性",
                False,
                "數量異常: " + "; ".join(issues),
                detail,
            )

        vcp_avg = detail.get("vcp_avg_20d", 0)
        sanxian_avg = detail.get("sanxian_avg_20d", 0)
        return LayerResult(
            "L4 歷史一致性",
            True,
            f"VCP {today_vcp} (均 {vcp_avg:.0f}) "
            f"三線 {today_sanxian} (均 {sanxian_avg:.0f}) 正常",
            detail,
        )

    # ==================== 報告與輸出 ====================

    def _format_report(
        self,
        target_date: date,
        layers: list[LayerResult],
        all_passed: bool,
    ) -> str:
        """格式化驗證報告"""
        lines = [f"=== [{self.label}] {target_date} 客觀驗證 ==="]
        for lr in layers:
            status = "PASS" if lr.passed else "FAIL"
            lines.append(f"[{status}] {lr.name}: {lr.summary}")
        conclusion = "全部通過" if all_passed else "有項目未通過"
        lines.append(f"結論: {conclusion}")
        lines.append("=" * 50)
        return "\n".join(lines)

    def _write_to_sheet(
        self,
        target_date: date,
        layers: list[LayerResult],
        all_passed: bool,
        exporter,
    ) -> None:
        """寫入驗證結果到 Google Sheet 的「驗證日誌」分頁"""
        if self.market == "tw":
            from config.settings import SHEET_IDS as sheet_ids
        else:
            from config.us_settings import US_SHEET_IDS as sheet_ids
        sheet_id = sheet_ids.get("verification")
        if not sheet_id:
            return

        sheet = exporter.client.open_by_key(sheet_id)

        # 取得或建立「驗證日誌」分頁
        tab_name = "驗證日誌"
        try:
            ws = sheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(
                title=tab_name, rows=1000, cols=8
            )
            # 寫標題
            ws.update(
                "A1:H1",
                [["日期", "L1 價格", "L2 重算", "L3 Sheet",
                  "L4 歷史", "結論", "詳情", "市場"]],
            )

        # 組合資料行
        layer_map = {}
        for lr in layers:
            status = "PASS" if lr.passed else "FAIL"
            layer_map[lr.name[:2]] = f"{status} ({lr.summary})"

        detail_json = json.dumps(
            {lr.name: lr.detail for lr in layers if lr.detail},
            ensure_ascii=False,
            default=str,
        )

        conclusion = "PASS" if all_passed else "FAIL"
        row = [
            target_date.isoformat(),
            layer_map.get("L1", "-"),
            layer_map.get("L2", "-"),
            layer_map.get("L3", "-"),
            layer_map.get("L4", "-"),
            conclusion,
            detail_json[:1000],  # Sheet 欄位長度限制
            self.label,
        ]

        # 追加到最後一行
        ws.append_row(row, value_input_option="RAW")
        logger.info(f"驗證結果已寫入 Sheet「{tab_name}」")

    # ==================== 工具方法 ====================

    def _to_yf_ticker(self, stock_id: str) -> str:
        """轉換為 yfinance ticker"""
        if self.market == "us":
            return stock_id

        # 台股：查 stock_type 決定 suffix
        import sqlite3

        try:
            conn = sqlite3.connect(self.db.db_path)
            row = conn.execute(
                "SELECT stock_type FROM stock_info WHERE stock_id = ?",
                (stock_id,),
            ).fetchone()
            conn.close()
            if row and row[0] == "tpex":
                return f"{stock_id}.TWO"
        except Exception:
            pass
        return f"{stock_id}.TW"

    def _get_db_prices(
        self, target_date: date, stock_ids: list[str]
    ) -> dict[str, float]:
        """從 DB 取收盤價"""
        import sqlite3

        price_table = (
            "daily_price"
            if self.market == "tw"
            else "us_daily_price"
        )
        conn = sqlite3.connect(self.db.db_path)
        prices = {}
        for sid in stock_ids:
            row = conn.execute(
                f"SELECT close_price FROM {price_table} "
                f"WHERE stock_id = ? AND date = ?",
                (sid, target_date.isoformat()),
            ).fetchone()
            if row and row[0]:
                prices[sid] = float(row[0])
        conn.close()
        return prices

    def _get_yf_prices(
        self, target_date: date, stock_ids: list[str]
    ) -> dict[str, float]:
        """從 yfinance 獨立抓收盤價"""
        tickers = [self._to_yf_ticker(sid) for sid in stock_ids]
        ticker_map = dict(zip(tickers, stock_ids))

        try:
            df = yf.download(
                " ".join(tickers),
                start=target_date,
                end=target_date + timedelta(days=1),
                progress=False,
                auto_adjust=False,
                threads=True,
            )
        except Exception as e:
            logger.warning(f"yfinance batch download 失敗: {e}")
            return {}

        if df.empty:
            return {}

        prices = {}
        for ticker_str, stock_id in ticker_map.items():
            try:
                if len(tickers) == 1:
                    price = float(df["Close"].iloc[0])
                else:
                    price = float(df["Close"][ticker_str].iloc[0])
                if not np.isnan(price) and price > 0:
                    prices[stock_id] = price
            except (KeyError, IndexError):
                continue

        return prices
