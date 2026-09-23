"""
美股技術分析篩選系統設定檔
完全獨立於台股設定，確保不影響現有功能
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
# 美股使用獨立的 SQLite 資料庫
US_SQLITE_DB_PATH = os.getenv(
    "US_SQLITE_DB_PATH",
    str(BASE_DIR / "data" / "zf_trend_us.db")
)

# ==================== 資料來源設定 ====================
# 資料來源選擇（改這一行即可切換免費/付費 API）
# "free" - yfinance + NASDAQ FTP（免費）
# "polygon" - Polygon.io（$199/月）
# "eodhd" - EODHD（$99.99/月）
# "twelvedata" - Twelve Data（$29起/月）
US_DATA_PROVIDER = os.getenv("US_DATA_PROVIDER", "free")

# 付費 API Key（日後擴充用）
US_POLYGON_API_KEY = os.getenv("US_POLYGON_API_KEY", "")
US_EODHD_API_KEY = os.getenv("US_EODHD_API_KEY", "")
US_TWELVEDATA_API_KEY = os.getenv("US_TWELVEDATA_API_KEY", "")

# ==================== 美股大盤指數 ====================
# S&P 500 作為美股大盤指數（對應台股的 TAIEX）
US_MARKET_INDEX = "^GSPC"

# ==================== Google Sheet 設定 ====================
# 美股使用完全獨立的 3 個新 Sheet
US_SHEET_IDS = {
    "company_master": os.getenv("US_SHEET_ID_COMPANY_MASTER", ""),
    "vcp": os.getenv("US_SHEET_ID_VCP", ""),
    "sanxian": os.getenv("US_SHEET_ID_SANXIAN", ""),
    "volume_surge": os.getenv("US_SHEET_ID_VOLUME_SURGE", ""),
    "verification": os.getenv("US_SHEET_ID_VERIFICATION", ""),
}

# Google 憑證（與台股共用）
GOOGLE_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_CREDENTIALS_PATH",
    str(BASE_DIR / "credentials.json")
)

# ==================== API 效能設定 ====================
# yfinance 批次下載設定（約 8000+ 檔美股）
US_BATCH_SIZE = int(os.getenv("US_BATCH_SIZE", "40"))  # 每批下載股票數（保守設定避免 rate limit）
US_BATCH_INTERVAL = int(os.getenv("US_BATCH_INTERVAL", "15"))  # 批次間隔（秒）
US_MAX_WORKERS = int(os.getenv("US_MAX_WORKERS", "2"))  # 平行下載 worker 數（用於 sector/industry 抓取）

# 股票清單來源
NASDAQ_FTP_URL = "ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqtraded.txt"

# ==================== 技術指標參數 ====================
# VCP 篩選參數（與台股相同邏輯）
US_VCP_PARAMS = {
    "ma50_period": 50,
    "ma150_period": 150,
    "ma200_period": 200,
    "lookback_20d": 20,
    "lookback_52w": 260,    # 52週 = 52×5 個交易日
    "new_high_tolerance": 0.01,  # 1% 容差
    "low_250d_mult": 1.30,  # 強勢/新高共用門檻：收盤價 > 近250日盤中最低價 × 1.30（離低點至少 30%）
}

# 三線開花篩選參數（與台股相同邏輯）
US_SANXIAN_PARAMS = {
    "ma8_period": 8,
    "ma21_period": 21,
    "ma55_period": 55,
}

# ==================== 重試設定 ====================
_max_retries_str = os.getenv("US_MAX_RETRIES", "3")
try:
    _max_retries = int(_max_retries_str)
    if _max_retries < 0:
        _max_retries = 3
except ValueError:
    _max_retries = 3

US_RETRY_CONFIG = {
    "max_retries": _max_retries,
    "retry_intervals": [300, 600, 3600],  # 5分/10分/1小時 (秒)
}

# ==================== 排程設定 ====================
# 美股收盤時間：美東 16:00 = 台北 05:00（夏令）/ 06:00（冬令）
# 建議在美股收盤後 1-2 小時執行
US_SCHEDULE_CONFIG = {
    "daily_task_time": "08:00",  # 台北時間（美股收盤後）
    "monthly_task_day": 1,
    "monthly_task_time": "09:00",
}

# ==================== 日誌設定 ====================
US_LOG_CONFIG = {
    "level": os.getenv("US_LOG_LEVEL", "INFO"),
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": str(BASE_DIR / "logs" / "zf_trend_us.log"),
}

# ==================== GitHub Actions 設定 ====================
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "false").lower() == "true"

if IS_GITHUB_ACTIONS:
    US_SQLITE_DB_PATH = os.getenv(
        "US_SQLITE_DB_PATH",
        str(BASE_DIR / "data" / "zf_trend_us.db")
    )


def get_us_client():
    """
    工廠方法：根據設定返回對應的美股 API 客戶端

    Returns:
        USStockClientBase 實例
    """
    from api.us_stock_client_free import USStockClientFree

    if US_DATA_PROVIDER == "free":
        return USStockClientFree()
    elif US_DATA_PROVIDER == "polygon":
        # 預留付費版
        from api.us_stock_client_paid import USStockClientPolygon
        return USStockClientPolygon(US_POLYGON_API_KEY)
    elif US_DATA_PROVIDER == "eodhd":
        from api.us_stock_client_paid import USStockClientEODHD
        return USStockClientEODHD(US_EODHD_API_KEY)
    elif US_DATA_PROVIDER == "twelvedata":
        from api.us_stock_client_paid import USStockClientTwelveData
        return USStockClientTwelveData(US_TWELVEDATA_API_KEY)
    else:
        # 預設使用免費版
        return USStockClientFree()
