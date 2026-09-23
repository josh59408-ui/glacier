"""
資料驗證腳本 — 檢查篩選結果的完整性與正確性

用法：
    source .venv/bin/activate
    python scripts/verify_data.py              # 完整驗證
    python scripts/verify_data.py --quick       # 快速檢查（跳過抽樣重算）

驗證層面：
  1. 資料完整性 — 每個交易日都有篩選結果
  2. 篩選正確性 — 隨機抽樣重算，比對是否一致
  3. 數值合理性 — 無 NaN/Infinity，數值在合理範圍
  4. 新/舊標記邏輯 — 前一天有的 stock_id 標舊
"""
import argparse
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger

from calculators.vcp_filter import VCPFilter, calculate_market_return
from calculators.sanxian_filter import SanxianFilter
from config.settings import SQLITE_DB_PATH
from data.sqlite_database import SQLiteDatabase
from utils.trading_calendar import TradingCalendar


class DataVerifier:
    def __init__(self, db: SQLiteDatabase):
        self.db = db
        self.conn = sqlite3.connect(SQLITE_DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.errors = []
        self.warnings = []

    def run_all(self, quick: bool = False, sample_size: int = 5):
        """執行所有驗證"""
        logger.info("=== 開始資料驗證 ===")

        self.check_completeness()
        self.check_value_sanity()
        self.check_new_old_logic(sample_size=3)

        if not quick:
            self.check_filter_accuracy(sample_size=sample_size)

        self._print_report()

    # ==================== 1. 資料完整性 ====================

    def check_completeness(self):
        """檢查每個交易日是否都有篩選結果"""
        logger.info("--- 1. 資料完整性檢查 ---")

        # 取得有股價的交易日範圍
        row = self.conn.execute(
            "SELECT MIN(date), MAX(date) FROM daily_price"
        ).fetchone()
        price_min, price_max = row[0], row[1]

        # 取得所有有篩選結果的日期
        filter_dates = set(
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT filter_date FROM filter_result"
            )
        )

        # 取得所有有股價的交易日（從有足夠歷史資料開始）
        price_dates = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT date FROM daily_price WHERE date >= '2025-07-01' ORDER BY date"
            )
        ]

        missing = [d for d in price_dates if d not in filter_dates]

        logger.info(f"股價資料範圍: {price_min} ~ {price_max}")
        logger.info(f"有股價的交易日: {len(price_dates)} 天")
        logger.info(f"有篩選結果的日期: {len(filter_dates)} 天")

        if missing:
            self.warnings.append(
                f"有 {len(missing)} 個交易日缺少篩選結果: "
                f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
            )
            logger.warning(f"缺少 {len(missing)} 天篩選結果")
        else:
            logger.info("✅ 所有交易日都有篩選結果")

        # 檢查每天是否同時有 VCP 和 sanxian
        for fd in list(filter_dates)[:]:
            types = set(
                r[0]
                for r in self.conn.execute(
                    "SELECT DISTINCT filter_type FROM filter_result WHERE filter_date = ?",
                    (fd,),
                )
            )
            if types != {"vcp", "sanxian"}:
                self.warnings.append(f"{fd} 只有 {types}，缺少篩選類型")

        logger.info("✅ 完整性檢查完成")

    # ==================== 2. 數值合理性 ====================

    def check_value_sanity(self):
        """檢查數值是否在合理範圍"""
        logger.info("--- 2. 數值合理性檢查 ---")

        # 檢查 return_20d 範圍
        rows = self.conn.execute("""
            SELECT filter_date, stock_id, return_20d
            FROM filter_result
            WHERE filter_type = 'vcp' AND return_20d IS NOT NULL
        """).fetchall()

        bad_return = []
        for r in rows:
            val = float(r["return_20d"])
            if abs(val) > 5.0:  # 超過 500% 漲跌幅
                bad_return.append((r["filter_date"], r["stock_id"], val))

        if bad_return:
            self.warnings.append(
                f"return_20d 超過 ±500%: {len(bad_return)} 筆，"
                f"例: {bad_return[:3]}"
            )
        else:
            logger.info("✅ return_20d 範圍正常")

        # 檢查 gap_ratio 範圍
        rows = self.conn.execute("""
            SELECT filter_date, stock_id, gap_ratio
            FROM filter_result
            WHERE filter_type = 'sanxian' AND gap_ratio IS NOT NULL
        """).fetchall()

        bad_gap = []
        for r in rows:
            val = float(r["gap_ratio"])
            if abs(val) > 2.0:  # 超過 200%
                bad_gap.append((r["filter_date"], r["stock_id"], val))

        if bad_gap:
            self.warnings.append(
                f"gap_ratio 超過 ±200%: {len(bad_gap)} 筆，"
                f"例: {bad_gap[:3]}"
            )
        else:
            logger.info("✅ gap_ratio 範圍正常")

        # 檢查 NULL 值比例
        total = self.conn.execute(
            "SELECT COUNT(*) FROM filter_result WHERE filter_type = 'vcp'"
        ).fetchone()[0]
        null_return = self.conn.execute(
            "SELECT COUNT(*) FROM filter_result WHERE filter_type = 'vcp' AND return_20d IS NULL"
        ).fetchone()[0]

        if total > 0:
            null_pct = null_return / total * 100
            if null_pct > 10:
                self.warnings.append(
                    f"VCP 有 {null_pct:.1f}% 的 return_20d 為 NULL"
                )
            else:
                logger.info(f"✅ VCP NULL 比例正常 ({null_pct:.1f}%)")

        logger.info("✅ 數值合理性檢查完成")

    # ==================== 3. 新/舊標記邏輯 ====================

    def check_new_old_logic(self, sample_size: int = 3):
        """驗證新/舊標記邏輯是否正確"""
        logger.info("--- 3. 新/舊標記邏輯檢查 ---")

        filter_dates = sorted(
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT filter_date FROM filter_result ORDER BY filter_date"
            )
        )

        if len(filter_dates) < 2:
            logger.warning("日期不足，跳過新/舊檢查")
            return

        # 抽樣幾組連續日期做檢查
        check_pairs = []
        for i in range(1, len(filter_dates)):
            prev_d = date.fromisoformat(filter_dates[i - 1])
            curr_d = date.fromisoformat(filter_dates[i])
            # 只檢查前一天確實是前一交易日的情況
            expected_prev = TradingCalendar.get_previous_trading_day(curr_d)
            if expected_prev and expected_prev == prev_d:
                check_pairs.append((filter_dates[i - 1], filter_dates[i]))

        if not check_pairs:
            logger.warning("找不到連續交易日，跳過新/舊檢查")
            return

        sampled = random.sample(check_pairs, min(sample_size, len(check_pairs)))

        for prev_date, curr_date in sampled:
            for ftype in ("vcp", "sanxian"):
                prev_ids = set(
                    r[0]
                    for r in self.conn.execute(
                        "SELECT stock_id FROM filter_result WHERE filter_date = ? AND filter_type = ?",
                        (prev_date, ftype),
                    )
                )
                curr_ids = set(
                    r[0]
                    for r in self.conn.execute(
                        "SELECT stock_id FROM filter_result WHERE filter_date = ? AND filter_type = ?",
                        (curr_date, ftype),
                    )
                )

                new_count = len(curr_ids - prev_ids)
                old_count = len(curr_ids & prev_ids)
                logger.info(
                    f"  {curr_date} {ftype}: "
                    f"共 {len(curr_ids)} 檔 (新 {new_count} / 舊 {old_count})"
                )

        logger.info("✅ 新/舊標記邏輯檢查完成")

    # ==================== 4. 篩選正確性（抽樣重算）====================

    def check_filter_accuracy(self, sample_size: int = 5):
        """隨機抽樣重算篩選結果，比對是否一致"""
        logger.info(f"--- 4. 篩選正確性檢查（抽樣 {sample_size} 天）---")

        filter_dates = sorted(
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT filter_date FROM filter_result ORDER BY filter_date"
            )
        )

        if not filter_dates:
            logger.warning("無篩選結果，跳過")
            return

        sampled = random.sample(
            filter_dates, min(sample_size, len(filter_dates))
        )

        vcp_filter = VCPFilter()
        sanxian_filter = SanxianFilter()
        stock_info = self.db.get_stock_info_dict()
        valid_ids = set(stock_info.keys())

        for date_str in sorted(sampled):
            target = date.fromisoformat(date_str)
            start = target - timedelta(days=365)

            price_df = self.db.get_daily_prices(start, target)
            market_df = self.db.get_market_index(start, target)

            if price_df.empty:
                self.warnings.append(f"{date_str}: 無股價資料，無法驗證")
                continue

            price_df = price_df[price_df["stock_id"].isin(valid_ids)]
            market_return = calculate_market_return(
                market_df, target, lookback=20
            )

            # 重算 VCP
            vcp_df = vcp_filter.filter(price_df, market_return, target)
            recalc_vcp_ids = set(vcp_df["stock_id"].tolist()) if not vcp_df.empty else set()

            db_vcp_ids = set(
                r[0]
                for r in self.conn.execute(
                    "SELECT stock_id FROM filter_result WHERE filter_date = ? AND filter_type = 'vcp'",
                    (date_str,),
                )
            )

            # 重算三線開花
            sanxian_df = sanxian_filter.filter(price_df, target)
            recalc_san_ids = set(sanxian_df["stock_id"].tolist()) if not sanxian_df.empty else set()

            db_san_ids = set(
                r[0]
                for r in self.conn.execute(
                    "SELECT stock_id FROM filter_result WHERE filter_date = ? AND filter_type = 'sanxian'",
                    (date_str,),
                )
            )

            # 比對
            vcp_match = recalc_vcp_ids == db_vcp_ids
            san_match = recalc_san_ids == db_san_ids

            vcp_diff = (recalc_vcp_ids - db_vcp_ids) | (db_vcp_ids - recalc_vcp_ids)
            san_diff = (recalc_san_ids - db_san_ids) | (db_san_ids - recalc_san_ids)

            status_vcp = "✅" if vcp_match else f"❌ 差異 {len(vcp_diff)} 檔"
            status_san = "✅" if san_match else f"❌ 差異 {len(san_diff)} 檔"

            logger.info(
                f"  {date_str}: VCP {status_vcp} ({len(db_vcp_ids)} 檔), "
                f"三線 {status_san} ({len(db_san_ids)} 檔)"
            )

            if not vcp_match:
                only_recalc = recalc_vcp_ids - db_vcp_ids
                only_db = db_vcp_ids - recalc_vcp_ids
                if only_recalc:
                    self.errors.append(
                        f"{date_str} VCP: 重算有但 DB 無: {list(only_recalc)[:5]}"
                    )
                if only_db:
                    self.errors.append(
                        f"{date_str} VCP: DB 有但重算無: {list(only_db)[:5]}"
                    )

            if not san_match:
                only_recalc = recalc_san_ids - db_san_ids
                only_db = db_san_ids - recalc_san_ids
                if only_recalc:
                    self.errors.append(
                        f"{date_str} 三線: 重算有但 DB 無: {list(only_recalc)[:5]}"
                    )
                if only_db:
                    self.errors.append(
                        f"{date_str} 三線: DB 有但重算無: {list(only_db)[:5]}"
                    )

        logger.info("✅ 篩選正確性檢查完成")

    # ==================== 報告 ====================

    def _print_report(self):
        """輸出驗證報告"""
        logger.info("")
        logger.info("=" * 50)
        logger.info("驗證報告")
        logger.info("=" * 50)

        if not self.errors and not self.warnings:
            logger.info("✅ 所有檢查通過，資料品質良好")
        else:
            if self.errors:
                logger.error(f"❌ 錯誤: {len(self.errors)} 項")
                for e in self.errors:
                    logger.error(f"  - {e}")

            if self.warnings:
                logger.warning(f"⚠️ 警告: {len(self.warnings)} 項")
                for w in self.warnings:
                    logger.warning(f"  - {w}")

        logger.info("=" * 50)

        self.conn.close()
        return len(self.errors) == 0


def main():
    parser = argparse.ArgumentParser(description="驗證篩選資料的完整性與正確性")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="快速檢查（跳過抽樣重算）",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=5,
        help="抽樣重算的天數（預設 5）",
    )
    args = parser.parse_args()

    db = SQLiteDatabase()
    verifier = DataVerifier(db)
    ok = verifier.run_all(quick=args.quick, sample_size=args.sample)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
