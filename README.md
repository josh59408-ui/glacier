# 藏鋒 ZF_TrendPicking

台股 + 美股技術分析**每日自動篩選系統**。每個交易日抓取股價，跑出 **VCP 強勢股** 與 **三線開花** 兩組清單，輸出到 Google Sheet 與 GitHub Pages 前端網站。台股與美股完全獨立隔離。

| 系統 | 主程式 | 資料庫 | 設定檔 |
|------|--------|--------|--------|
| 台股 | `python main.py` | `data/zf_trend.db` | `config/settings.py` |
| 美股 | `python us_main.py` | `data/zf_trend_us.db` | `config/us_settings.py` |

> 完整說明見 [`docs/`](docs/)（規格書、[流程圖解](docs/06-pipeline-flow.md)）、[`CLAUDE.md`](CLAUDE.md)（開發指南）、[`HANDOVER.md`](HANDOVER.md)（技術轉移）。

---

## 環境設定檔（多環境機制）

### 設計理念：一份程式碼、多套環境

同一套程式碼可對應多套環境設定（不同的 API Token、Google Sheet、GitHub repo）。透過 **`APP_ENV` 環境變數**決定載入哪一個 `.env` 檔，無需修改任何程式碼。

典型情境：**同一個 GitHub 帳號、要操作不同 repo**（例如自己的 repo 與甲方開的 repo），只需切換設定檔，不必切換 token 或帳號。

### `.env` 檔案家族

| 檔案 | 用途 | 進 Git？ |
|------|------|:-------:|
| `.env.example` | 範本，列出所有變數與說明（**唯一進版控的**） | ✅ |
| `.env` | 單一環境使用；也是未設 `APP_ENV` 時的預設 | ❌ |
| `.env.self` | 多環境：你自己的 token / Sheet / GitHub | ❌ |
| `.env.client` | 多環境：甲方的 token / Sheet / GitHub | ❌ |

`.gitignore` 規則確保**只有範本進版控**，實際機密永不外流：

```gitignore
.env            # 忽略預設環境檔
.env.*          # 忽略所有 .env.<名稱>
!.env.example   # 但範本例外放行
```

### 選檔邏輯（Python 端）

`config/settings.py` 與 `config/us_settings.py` 在載入時決定讀哪個檔（[`settings.py:15-20`](config/settings.py)）：

```python
# 多環境支援：設 APP_ENV=client 則讀 .env.client；未設或找不到則回退讀 .env（向後相容）
_app_env = os.getenv("APP_ENV", "").strip()
_env_file = BASE_DIR / f".env.{_app_env}" if _app_env else BASE_DIR / ".env"
if not _env_file.exists():
    _env_file = BASE_DIR / ".env"   # 找不到指定檔就回退
load_dotenv(_env_file)
```

決策流程：

```
讀取環境變數 APP_ENV
        │
        ├─ 有值（如 "client"）──► 目標 = .env.client ──┐
        │                                             ├─ 檔案存在？
        └─ 空值 ────────────────► 目標 = .env         │     ├─ 是 → 載入該檔
                                                      │     └─ 否 → 回退載入 .env
                                                      ▼
                                            load_dotenv(目標)
```

**向後相容**：完全不設 `APP_ENV`、也不建 `.env.<名稱>` 時，行為等同單純讀 `.env`，與舊版一致。

---

### 用法一：單一環境（最簡）

```bash
cp .env.example .env      # 複製範本
# 編輯 .env 填入實際的 Token / Sheet ID
python main.py daily      # 直接執行，Python 自動讀 .env
```

### 用法二：多環境切換

**1. 各環境各建一份設定檔**（放專案根，已被 `.gitignore` 擋住）：

```bash
cp .env.example .env.self     # 你自己的 token / Sheet / GitHub
cp .env.example .env.client   # 甲方的 token / Sheet / GitHub
```

每份 `.env.<名稱>` 除了應用設定，可再填 GitHub 區塊（見下方變數清單的 `GH_REPO` / `GIT_REMOTE`）。

**2. 用 `scripts/use-env.sh` 一鍵切換**：

```bash
source scripts/use-env.sh self      # 切到自己的環境
source scripts/use-env.sh client    # 切到甲方的環境
```

> ⚠️ **必須用 `source`**（不能 `./use-env.sh` 或 `bash use-env.sh`），否則設定不會留在當前 shell。

切換時 `use-env.sh` 會做三件事：

1. 把 `.env.<名稱>` 的所有變數載入當前 shell；
2. `export APP_ENV=<名稱>` → 之後執行 `python main.py` / `us_main.py` 時，Python 依上述邏輯自動讀對應的 `.env.<名稱>`；
3. 依該檔的 GitHub 區塊，設定 `gh` 預設目標 repo（`GH_REPO`）並提示 `git push` 用的 remote（`GIT_REMOTE`）——因為是**同一 GitHub 帳號、不同 repo**，所以不需切換認證，只切目標。

切換後畫面範例：

```
✅ 已切換到環境 [client]
   APP_ENV=client  →  Python 會自動讀 .env.client
   gh 目標 repo = client_account/ZF_TrendPicking
   git push 用： git push client main
```

---

## 環境變數清單

複製 `.env.example` 後依下表填入。

| 類別 | 變數 | 說明 |
|------|------|------|
| **台股** | `FINMIND_TOKEN` | FinMind API Token（[finmindtrade.com](https://finmindtrade.com/) 註冊；美股不需要） |
| | `SHEET_ID_COMPANY_MASTER` | 台股公司主檔 Sheet ID |
| | `SHEET_ID_TW_VCP` | 台股 VCP Sheet ID |
| | `SHEET_ID_TW_SANXIAN` | 台股三線開花 Sheet ID |
| | `SHEET_ID_VERIFICATION` | 台股驗證 Sheet ID |
| **美股** | `US_SHEET_ID_COMPANY_MASTER` | 美股公司主檔 Sheet ID |
| | `US_SHEET_ID_VCP` | 美股 VCP Sheet ID |
| | `US_SHEET_ID_SANXIAN` | 美股三線開花 Sheet ID |
| | `US_SHEET_ID_VERIFICATION` | 美股驗證 Sheet ID |
| | `US_DATA_PROVIDER` | 美股資料源，預設 `free`（yfinance，免金鑰）；`polygon`/`eodhd`/`twelvedata` 為付費擴充預留 |
| **共用** | `GOOGLE_CREDENTIALS_PATH` | Google 服務帳號金鑰路徑（台股美股共用同一把，預設 `credentials.json`） |
| | `LOG_LEVEL` | 日誌級別 `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| **GitHub**<br>（多環境用） | `GH_REPO` | 此環境目標 repo（`owner/repo`），`gh run list` / `gh workflow run` 自動對它操作 |
| | `GIT_REMOTE` | `git push` 用的 remote 名稱（自己的通常是 `origin`；甲方 repo 可自訂如 `client`） |

> 單一環境使用時，`GH_REPO` / `GIT_REMOTE` 可留空。

---

## 快速開始

```bash
# 1. 虛擬環境（每次操作前務必啟用）
source .venv/bin/activate

# 2. 設定環境（擇一）
cp .env.example .env                    # 單一環境
# 或 source scripts/use-env.sh self     # 多環境

# 3. 首次初始化
python main.py init                     # 台股
python us_main.py init                  # 美股（約 30-60 分鐘）

# 4. 每日篩選
python main.py daily
python us_main.py daily
```

更多指令與維運腳本見 [`docs/05-operations-guide.md`](docs/05-operations-guide.md)。
