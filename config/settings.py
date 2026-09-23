"""
台股技術分析篩選系統設定檔
支援 GitHub Actions + SQLite + HybridClient 架構
（HybridClient = FinMind 股票清單 + yfinance 股價資料）
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 專案根目錄
BASE_DIR = Path(__file__).resolve().parent.parent

# 載入環境變數檔案
# 多環境支援：設 APP_ENV=client 則讀 .env.client；未設或找不到則回退讀 .env（向後相容）
_app_env = os.getenv("APP_ENV", "").strip()
_env_file = BASE_DIR / f".env.{_app_env}" if _app_env else BASE_DIR / ".env"
if not _env_file.exists():
    _env_file = BASE_DIR / ".env"
load_dotenv(_env_file)

# ==================== 資料庫設定 ====================
# SQLite 資料庫路徑
SQLITE_DB_PATH = os.getenv(
    "SQLITE_DB_PATH",
    str(BASE_DIR / "data" / "zf_trend.db")
)

# 舊的 PostgreSQL 設定（保留相容性，但不再使用）
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{SQLITE_DB_PATH}"
)

# ==================== API 設定 ====================
# FinMind API 設定
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")

# API 限流設定（FinMind 免費 600 次/小時）
API_CALLS_PER_HOUR = 600

# ==================== Google Sheet 設定 ====================
GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    str(BASE_DIR / "credentials.json")
)

SHEET_IDS = {
    "company_master": os.getenv("SHEET_ID_COMPANY_MASTER", ""),
    "tw_vcp": os.getenv("SHEET_ID_TW_VCP", ""),
    "us_vcp": os.getenv("SHEET_ID_US_VCP", ""),  # 暫不使用
    "tw_sanxian": os.getenv("SHEET_ID_TW_SANXIAN", ""),
    "tw_volume_surge": os.getenv("SHEET_ID_TW_VOLUME_SURGE", ""),
    "us_sanxian": os.getenv("SHEET_ID_US_SANXIAN", ""),  # 暫不使用
    "verification": os.getenv("SHEET_ID_VERIFICATION", ""),
}

# ==================== 技術指標參數 ====================
MA_PERIODS = {
    "short": [8, 21],       # 三線開花短期均線
    "medium": [50, 55],     # 中期均線
    "long": [150, 200],     # 長期均線
}

# VCP 篩選參數
VCP_PARAMS = {
    "ma50_period": 50,
    "ma150_period": 150,
    "ma200_period": 200,
    "lookback_20d": 20,
    "lookback_52w": 260,    # 52週 = 52×5 個交易日
    "new_high_tolerance": 0.01,  # 1% 容差
    "low_250d_mult": 1.30,  # 強勢/新高共用門檻：收盤價 > 近250日盤中最低價 × 1.30（離低點至少 30%）
}

# 三線開花篩選參數
SANXIAN_PARAMS = {
    "ma8_period": 8,
    "ma21_period": 21,
    "ma55_period": 55,
}

# ==================== 重試設定 ====================
_max_retries_str = os.getenv("MAX_RETRIES", "3")
try:
    _max_retries = int(_max_retries_str)
    if _max_retries < 0:
        _max_retries = 3
except ValueError:
    _max_retries = 3

RETRY_CONFIG = {
    "max_retries": _max_retries,
    "retry_intervals": [300, 600, 3600],  # 5分/10分/1小時 (秒)
}

# ==================== 排程設定 ====================
SCHEDULE_CONFIG = {
    "daily_task_time": "17:45",  # 收盤後
    "monthly_task_day": 1,
    "monthly_task_time": "09:00",
}

# ==================== 日誌設定 ====================
LOG_CONFIG = {
    "level": os.getenv("LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": str(BASE_DIR / "logs" / "zf_trend.log"),
}

# ==================== GitHub Actions 設定 ====================
# 是否在 GitHub Actions 環境中執行
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "false").lower() == "true"

# GitHub Actions 中 SQLite 資料庫路徑
if IS_GITHUB_ACTIONS:
    SQLITE_DB_PATH = os.getenv(
        "SQLITE_DB_PATH",
        str(BASE_DIR / "data" / "zf_trend.db")
    )
