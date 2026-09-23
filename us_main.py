#!/usr/bin/env python3
"""
美股技術分析篩選系統主程式
完全獨立於台股，使用獨立的資料庫和設定

使用方式:
    # 執行每日任務（自動跳過假日）
    python us_main.py daily

    # 執行指定日期的每日任務
    python us_main.py daily 2026-01-23

    # 強制在假日執行（使用最近交易日資料）
    python us_main.py daily --force

    # 執行每月任務（更新公司主檔）
    python us_main.py monthly

    # 初始化系統（首次執行）
    python us_main.py init

    # 啟動排程
    python us_main.py schedule

    # 健康檢查
    python us_main.py health

    # 補齊歷史資料
    python us_main.py backfill [天數]
"""
import warnings

# 忽略第三方庫的棄用警告
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*Timestamp.utcnow.*")

import sys
from datetime import date, timedelta
from pathlib import Path

import schedule
import time

from loguru import logger

# 設定專案路徑
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.us_settings import US_LOG_CONFIG, US_SCHEDULE_CONFIG, get_us_client
from data.us_database import USSQLiteDatabase
from exporters.us_google_sheet import USGoogleSheetExporter
from tasks.us_daily_task import USDailyTask, run_us_daily_task
from tasks.us_monthly_task import USMonthlyTask, run_us_monthly_task


def setup_logging():
    """設定美股日誌"""
    log_path = Path(US_LOG_CONFIG["file"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger.add(
        US_LOG_CONFIG["file"],
        level=US_LOG_CONFIG["level"],
        format=US_LOG_CONFIG["format"],
        rotation="1 day",
        retention="30 days",
    )


def cmd_init():
    """
    初始化美股系統

    執行:
    1. 建立美股資料表
    2. 取得美股股票清單
    3. 取得歷史股價（252 天）
    """
    logger.info("=== 初始化美股系統 ===")

    # 初始化元件
    db = USSQLiteDatabase()
    client = get_us_client()

    # 建立美股資料表
    logger.info("建立美股資料表...")
    db.create_tables()

    # 取得美股股票清單
    logger.info("取得美股股票清單...")
    stock_df = client.get_stock_info()
    if stock_df.empty:
        logger.error("無法取得美股股票清單，請檢查網路連線")
        return
    db.upsert_stock_info(stock_df)
    logger.info(f"已儲存 {len(stock_df)} 檔美股股票資訊")

    # 取得美股歷史股價（批量下載）
    logger.info("取得美股歷史股價（批量下載，約需 30-60 分鐘）...")
    end_date = date.today()
    start_date = end_date - timedelta(days=365)

    # 取得所有股票代號
    stock_ids = db.get_stock_ids()

    # 批次查詢股價
    price_df = client.get_stock_price(
        start_date, end_date,
        stock_ids=stock_ids
    )
    if not price_df.empty:
        db.upsert_daily_price(price_df)
        logger.info(f"已儲存 {len(price_df)} 筆美股股價資料")

    # 取得美股大盤指數（S&P 500）
    logger.info("取得美股大盤指數 (S&P 500)...")
    market_df = client.get_market_index(start_date, end_date)
    if not market_df.empty:
        db.upsert_market_index(market_df)
        logger.info(f"已儲存 {len(market_df)} 筆美股大盤指數")

    logger.info("=== 美股初始化完成 ===")

    # 顯示統計
    stats = client.get_stats()
    db_stats = db.get_stats()
    logger.info(f"API 呼叫次數: {stats['total_requests']}")
    logger.info(f"資料庫大小: {db_stats['db_size']}")
    logger.info(f"股票數: {db_stats['stock_count']}")
    logger.info(f"股價筆數: {db_stats['price_count']}")


def cmd_daily(target_date: date = None, skip_non_trading_day: bool = True):
    """執行美股每日任務"""
    task = USDailyTask()
    result = task.run(target_date, skip_non_trading_day=skip_non_trading_day)

    if result.get("skipped"):
        logger.info(f"美股每日任務跳過: {result.get('reason', '非交易日')}")
    elif result["success"]:
        logger.info(f"美股每日任務成功: VCP {result['vcp_count']} 檔, 三線開花 {result['sanxian_count']} 檔")
    else:
        logger.error(f"美股每日任務失敗: {result['errors']}")

    return result


def cmd_monthly():
    """執行美股每月任務"""
    result = run_us_monthly_task()

    if result["success"]:
        logger.info(f"美股每月任務成功: 更新 {result['stock_count']} 檔股票")
    else:
        logger.error(f"美股每月任務失敗: {result['errors']}")


def cmd_schedule():
    """啟動美股排程"""
    logger.info("=== 啟動美股排程模式 ===")

    # 每日任務
    def safe_daily():
        try:
            cmd_daily()
        except Exception as e:
            logger.error(f"美股每日任務排程執行失敗: {e}")

    daily_time = US_SCHEDULE_CONFIG["daily_task_time"]
    schedule.every().day.at(daily_time).do(safe_daily)
    logger.info(f"美股每日任務排程: {daily_time}")

    # 每月任務（每月 1 日）
    monthly_day = US_SCHEDULE_CONFIG["monthly_task_day"]
    monthly_time = US_SCHEDULE_CONFIG["monthly_task_time"]

    def monthly_job():
        try:
            if date.today().day == monthly_day:
                cmd_monthly()
        except Exception as e:
            logger.error(f"美股每月任務排程執行失敗: {e}")

    schedule.every().day.at(monthly_time).do(monthly_job)
    logger.info(f"美股每月任務排程: 每月 {monthly_day} 日 {monthly_time}")

    # 執行排程
    logger.info("美股排程已啟動，等待執行...")
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except KeyboardInterrupt:
            logger.info("收到中斷信號，美股排程停止")
            break
        except Exception as e:
            logger.error(f"美股排程執行異常: {e}")
            time.sleep(60)


def cmd_health():
    """美股健康檢查"""
    logger.info("=== 美股健康檢查 ===")

    # 美股資料庫檢查
    db = USSQLiteDatabase()
    db_ok = db.health_check()
    logger.info(f"美股 SQLite 資料庫: {'✓ 正常' if db_ok else '✗ 異常'}")
    if db_ok:
        try:
            stats = db.get_stats()
        except Exception:
            # 資料表尚未建立
            db.create_tables()
            stats = db.get_stats()
        logger.info(f"  - 檔案大小: {stats['db_size']}")
        logger.info(f"  - 股票數: {stats['stock_count']}")
        logger.info(f"  - 股價筆數: {stats['price_count']}")

    # 美股 Google Sheet 檢查
    exporter = USGoogleSheetExporter()
    sheet_ok = exporter.health_check()
    logger.info(f"美股 Google Sheet: {'✓ 正常' if sheet_ok else '✗ 未連線'}")

    # 美股 API 檢查
    try:
        client = get_us_client()
        api_ok = client.health_check()
    except Exception as e:
        api_ok = False
        logger.error(f"美股 API 檢查失敗: {e}")

    logger.info(f"美股 API (yfinance): {'✓ 正常' if api_ok else '✗ 異常'}")

    # 總結
    all_ok = db_ok and api_ok
    logger.info(f"美股總體狀態: {'✓ 正常' if all_ok else '✗ 需要處理'}")

    return all_ok


def cmd_backfill(days: int = 30):
    """
    補齊美股歷史資料

    Args:
        days: 補齊天數
    """
    logger.info(f"=== 補齊 {days} 天美股歷史資料 ===")

    db = USSQLiteDatabase()
    client = get_us_client()

    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    # 取得所有股票代號
    stock_ids = db.get_stock_ids()

    if not stock_ids:
        logger.warning("尚無美股股票清單，請先執行 'python us_main.py init'")
        return

    # 取得股價（批量下載）
    price_df = client.get_stock_price(
        start_date, end_date,
        stock_ids=stock_ids
    )
    if not price_df.empty:
        db.upsert_daily_price(price_df)
        logger.info(f"已補齊 {len(price_df)} 筆美股股價資料")

    # 取得大盤指數
    market_df = client.get_market_index(start_date, end_date)
    if not market_df.empty:
        db.upsert_market_index(market_df)
        logger.info(f"已補齊 {len(market_df)} 筆美股大盤指數")

    logger.info("=== 美股補齊完成 ===")


def main():
    """美股主程式進入點"""
    setup_logging()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    if command == "init":
        cmd_init()
    elif command == "daily":
        target_date = None
        force = False
        for arg in sys.argv[2:]:
            if arg == "--force":
                force = True
            else:
                try:
                    target_date = date.fromisoformat(arg)
                except ValueError:
                    print(f"日期格式錯誤: {arg}，請使用 YYYY-MM-DD 格式")
                    sys.exit(1)
        cmd_daily(target_date, skip_non_trading_day=not force)
    elif command == "monthly":
        cmd_monthly()
    elif command == "schedule":
        cmd_schedule()
    elif command == "health":
        cmd_health()
    elif command == "backfill":
        days = 30
        if len(sys.argv) > 2:
            try:
                days = int(sys.argv[2])
                if days <= 0:
                    print("天數必須為正整數")
                    sys.exit(1)
            except ValueError:
                print(f"天數格式錯誤: {sys.argv[2]}，請輸入正整數")
                sys.exit(1)
        cmd_backfill(days)
    else:
        print(f"未知指令: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
