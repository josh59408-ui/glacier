# ZF_TrendPicking 技術轉移與初始化手冊

> **文件用途**：這份文件是給「**完全沒接觸過本專案的接手工程師**」看的。照著從上到下做一次，你就能用**自己的帳號**把整套系統（台股 + 美股篩選 + Google Sheet 匯出 + 前端網站 + GitHub 自動化）獨立跑起來，完全不依賴原作者的任何帳號。
>
> **閱讀方式**：第 0~2 章先讀懂「你接手的是什麼、要準備哪些帳號」，第 3 章（Part A~H）是手把手實作，請**依序執行**。每一步都有可直接複製的指令。
>
> 最後更新：2026-06-27

---

## 目錄

- [0. 系統總覽：你接手的是什麼](#0-系統總覽你接手的是什麼)
- [1. 資料流與自動化架構](#1-資料流與自動化架構)
- [2. 你需要準備的帳號與資產（總清單）](#2-你需要準備的帳號與資產總清單)
- [Part A — 本機開發環境](#part-a--本機開發環境)
- [Part B — 申請 FinMind API Token](#part-b--申請-finmind-api-token)
- [Part C — 建立 Google Cloud 服務帳號](#part-c--建立-google-cloud-服務帳號最關鍵最容易卡)
- [Part D — 建立 8 個 Google Sheet 並授權](#part-d--建立-8-個-google-sheet-並授權)
- [Part E — 設定 .env 與 credentials.json](#part-e--設定-env-與-credentialsjson)
- [Part F — 初始化資料庫](#part-f--初始化資料庫)
- [Part G — 本機驗證（確認真的會動）](#part-g--本機驗證確認真的會動)
- [Part H — GitHub 端設定（雲端自動化）](#part-h--github-端設定雲端自動化)
- [4. 自動化排程一覽](#4-自動化排程一覽)
- [5. 日常運維指令速查](#5-日常運維指令速查)
- [6. 安全交接清單（技術轉讓專用，務必執行）](#6-安全交接清單技術轉讓專用務必執行)
- [7. 疑難排解 FAQ](#7-疑難排解-faq)
- [附錄 A：環境變數總表](#附錄-a環境變數總表)
- [附錄 B：8 個 Google Sheet 對照表](#附錄-b8-個-google-sheet-對照表)
- [附錄 C：專案檔案結構](#附錄-c專案檔案結構)
- [附錄 D：關鍵踩雷集](#附錄-d關鍵踩雷集)

---

## 0. 系統總覽：你接手的是什麼

ZF_TrendPicking（藏鋒）是一套**股票技術分析篩選系統**，每天自動從股市抓資料、跑兩種選股策略、把結果寫到 Google Sheet 和一個查詢網站。

它同時支援兩個市場，**兩者完全獨立隔離**（各自的入口程式、資料庫、設定、Google Sheet）：

| | 台股系統 | 美股系統 |
|---|---|---|
| 主程式 | `python main.py` | `python us_main.py` |
| 資料庫 | `data/zf_trend.db`（SQLite） | `data/zf_trend_us.db`（SQLite） |
| 設定檔 | `config/settings.py` | `config/us_settings.py` |
| 資料來源 | FinMind API + yfinance 備援 | yfinance（免費，免金鑰） |

**兩種選股策略**：
- **VCP**（價格收縮型態）：找強勢、接近 52 週新高、量縮整理的股票。
- **三線開花**：找短中長期均線（8/21/55）糾結後即將發散的股票。

**三個產出**：
1. **Google Sheet**：每天把選股結果寫到試算表（給人看、可分享）。
2. **前端查詢網站**：GitHub Pages 上的靜態網站（`https://<你的帳號>.github.io/<repo名>/`）。
3. **SQLite 資料庫**：所有歷史股價與篩選結果的真實儲存體。

---

## 1. 資料流與自動化架構

```
                 ┌─────────────────────────────────────────────┐
                 │            GitHub Actions（雲端排程）          │
                 │  每個交易日自動觸發，runner 是無狀態的         │
                 └─────────────────────────────────────────────┘
                                    │
   ① 從 GitHub Release 下載昨天的 DB 備份並還原
                                    │
                                    ▼
   ②  抓股票清單 ──→ 抓當日股價 ──→ 偵測除權息/分割 ──→ 跑 VCP + 三線篩選
        (FinMind/yfinance)         (存入 SQLite DB)              │
                                    │                            ▼
   ③ 把篩選結果寫到 Google Sheet（gspread + 服務帳號）       存入 filter_result 表
                                    │
   ④ 把 DB gzip 壓縮後上傳回 GitHub Release（給明天用）
                                    │
                                    ▼
   ⑤ deploy-site workflow：從 DB 匯出精簡 JSON → 部署到 GitHub Pages
```

**關鍵設計**：GitHub Actions 的執行環境（runner）每次跑完就銷毀，沒有持久硬碟。所以資料庫不是放在 repo 裡，而是：
- **存放在 GitHub Release**（永久保存），台股用 tag `db-backup`、美股用 tag `us-db-backup`。
- 每次排程**開頭下載還原、結尾壓縮上傳覆蓋**，形成資料接力。
- ⚠️ 因此**絕對不能同時跑兩個同市場的 workflow**，會搶同一份 DB 備份互相覆蓋。

---

## 2. 你需要準備的帳號與資產（總清單）

開始動手前，先把這些帳號辦好。全部都有免費額度，本專案在免費範圍內即可運作。

| # | 要準備什麼 | 用途 | 哪裡辦 | 費用 |
|---|---|---|---|---|
| 1 | **Python 3.11+** | 跑程式 | python.org / Homebrew | 免費 |
| 2 | **Git + GitHub 帳號** | 版控、雲端自動化 | github.com | 免費 |
| 3 | **GitHub CLI（`gh`）** | 操作 Release、觸發 workflow | cli.github.com | 免費 |
| 4 | **FinMind API Token** | 台股股票清單來源 | [finmindtrade.com](https://finmindtrade.com/) | 免費（限流 600 次/小時） |
| 5 | **Google Cloud 帳號** | 建立服務帳號寫 Google Sheet | [console.cloud.google.com](https://console.cloud.google.com/) | 免費 |
| 6 | **Google 服務帳號 JSON 金鑰** | 程式以此身分寫 Sheet | 在 GCP 建立（Part C） | 免費 |
| 7 | **8 個 Google Sheet** | 存放選股結果（台股4 + 美股4） | Google 試算表 | 免費 |

> **不需要**：美股的 yfinance 完全免費、免註冊、免金鑰。`config/us_settings.py` 裡的 `US_POLYGON_API_KEY` / `US_EODHD_API_KEY` / `US_TWELVEDATA_API_KEY` 都是**日後擴充用的預留欄位，現在不用辦**。

---

## Part A — 本機開發環境

### A1. 安裝必要工具

```bash
# macOS（用 Homebrew）
brew install python@3.11 git gh

# 確認版本（Python 需 3.11 以上；實測 3.13 也可用）
python3 --version
git --version
gh --version
```

> Windows 請到各官網下載安裝；後續指令以 macOS / Linux 的 bash 為準，Windows 請用 WSL 或 Git Bash。

### A2. 取得程式碼

```bash
# 用你自己的 GitHub 帳號登入（不要沿用原作者的 token，見第 6 章）
gh auth login

# 取得程式碼（二選一）
#  (a) 如果原作者把 repo 轉移給你：
git clone https://github.com/<你的帳號>/ZF_TrendPicking.git
#  (b) 如果你拿到的是壓縮檔/匯出檔：解壓後 cd 進去，再 git init 接上你自己的 repo
cd ZF_TrendPicking
```

### A3. 建立虛擬環境並安裝套件

```bash
cd ZF_TrendPicking

# 建立虛擬環境
python3.11 -m venv .venv

# 啟用虛擬環境（⚠️ 之後每次操作前都要先做這一步！）
source .venv/bin/activate

# 安裝所有依賴套件
pip install -r requirements.txt
```

> **最重要的習慣**：本專案所有 Python 指令都必須在虛擬環境內執行。每開一個新終端機，第一件事就是 `source .venv/bin/activate`（提示字元前面會出現 `(.venv)`）。

主要套件（`requirements.txt`）：`pandas`、`numpy`、`yfinance(>=1.0,<2.0)`、`sqlalchemy`、`gspread`、`google-auth`、`python-dotenv`、`loguru`。

> ⚠️ yfinance 被刻意鎖在 `<2.0.0`，因為 2.x 有破壞性變更。**不要自行升級到 2.x**。

---

## Part B — 申請 FinMind API Token

FinMind 提供台股的股票清單與還原權息價（用於除權息偵測）。**美股不需要這個。**

1. 到 [https://finmindtrade.com/](https://finmindtrade.com/) 註冊帳號。
2. 登入後在會員中心取得你的 **API Token**（一長串字串）。
3. 先把它記下來，等到 [Part E](#part-e--設定-env-與-credentialsjson) 會填進 `.env` 的 `FINMIND_TOKEN`。

> 免費版限流約 600 次/小時，本專案的批次設計已在此限制內。

---

## Part C — 建立 Google Cloud 服務帳號（最關鍵、最容易卡）

程式要自動寫 Google Sheet，是透過一個「**服務帳號（Service Account）**」的身分。服務帳號就像一個**機器人 Google 帳號**，有自己的 email，你要把這個 email 加進每個 Sheet 的共用清單，程式才寫得進去。

> 本專案**只用服務帳號模式**，不需要 OAuth、不會跳出瀏覽器授權、不會產生 `token.json`。你只需要一個 JSON 金鑰檔。

### C1. 建立 Google Cloud 專案

1. 登入 [Google Cloud Console](https://console.cloud.google.com/)。

2. 頂端專案選單 →「新增專案」→ 取個名字（例如 `zf-trend`）→ 建立。
   ![image-20260627164324003](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627164324003.png)

   ![image-20260627164835920](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627164835920.png)

### C2. 啟用兩個 API（⚠️ 兩個都要，不是只有 Sheets）

到「**API 和服務 → 程式庫**」，搜尋並各按一次「啟用」：
![image-20260627164913292](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627164913292.png)

![image-20260627165004968](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165004968.png)
![image-20260627165049810](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165049810.png)

1. **Google Sheets API** ← 讀寫儲存格
   ![image-20260627165125122](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165125122.png)

   ![image-20260627165151187](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165151187.png)

2. **Google Drive API** ← `gspread` 開啟試算表（`open_by_key`）、建立分頁時底層走 Drive
   ![image-20260627165256141](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165256141.png)

   ![image-20260627165417237](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165417237.png)

> 🔴 **最常見的踩雷點**：只開了 Sheets API、沒開 Drive API，結果程式在開啟 Sheet 時噴 `403`。**兩個一定都要開。**（程式的權限範圍 `SCOPES` 同時宣告了 spreadsheets + drive，見 `exporters/google_sheet.py`。）

### C3. 建立服務帳號

1. 到「**IAM 與管理 → 服務帳號**」→「**建立服務帳號**」。
   ![image-20260627165601087](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165601087.png)

   ![image-20260627165714383](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165714383.png)
2. 取個名字（例如 `googlesheettrend`）→ 建立並繼續 → 角色可略過 → 完成。
   ![image-20260627165753272](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165753272.png)
3. 建好後會得到一個 email，長得像：
   ```
   googlesheettrend@<你的專案id>.iam.gserviceaccount.com
   ```
   ![image-20260627165829798](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165829798.png)
   
   **把這個 email 記下來**，Part D 要用它授權每個 Sheet。

> 📌 原作者現有資產的服務帳號是 `googlesheettrend@zf-trend.iam.gserviceaccount.com`（專案 id `zf-trend`）。你自建後會是**你自己的 email**，以你的為準。

### C4. 下載 JSON 金鑰，存成 credentials.json

1. 點進剛建立的服務帳號 →「**金鑰**」分頁 →「**新增金鑰 → 建立新的金鑰 → JSON**」→ 下載。
   ![image-20260627165901968](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627165901968.png)

   ![image-20260627170016575](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627170016575.png)

   ![image-20260627170039013](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627170039013.png)
2. 把下載的檔案改名為 **`credentials.json`**，放到**專案根目錄**（與 `main.py` 同層）。
   ![image-20260627170204070](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627170204070.png)

```bash
# 例如下載到 ~/Downloads 後
mv ~/Downloads/zf-trend-xxxxxx.json /path/to/ZF_TrendPicking/credentials.json
```

> 🔒 `credentials.json` 含真實私鑰，已被 `.gitignore` 排除

---

## Part D — 建立 8 個 Google Sheet 並授權

本系統會寫入 **8 個** Google Sheet（台股 4 個 + 美股 4 個）。

### D1. 建立 8 個空白試算表

到 [Google 試算表](https://sheets.google.com/) 建立 8 個新試算表，建議命名如下（名字隨意，重點是別搞混）：

| # | 建議名稱 | 對應環境變數 |
|---|---|---|
| 1 | 台股-公司主檔 | `SHEET_ID_COMPANY_MASTER` |
| 2 | 台股-VCP | `SHEET_ID_TW_VCP` |
| 3 | 台股-三線開花 | `SHEET_ID_TW_SANXIAN` |
| 4 | 台股-驗證 | `SHEET_ID_VERIFICATION` |
| 5 | 美股-公司主檔 | `US_SHEET_ID_COMPANY_MASTER` |
| 6 | 美股-VCP | `US_SHEET_ID_VCP` |
| 7 | 美股-三線開花 | `US_SHEET_ID_SANXIAN` |
| 8 | 美股-驗證 | `US_SHEET_ID_VERIFICATION` |

### D2. 把服務帳號 email 加為「編輯者」

對**每一個** Sheet：右上「共用」→ 貼上 Part C3 的服務帳號 email → 權限選 **編輯者** → 傳送（可取消「通知」勾選）。
![image-20260627170313500](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627170313500.png)

![image-20260627170406263](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627170406263.png)

![image-20260627170432319](/Users/wanghongxiang/Library/Application Support/typora-user-images/image-20260627170432319.png)

> 🔴 漏掉這步 = 程式跑得動但 Sheet 完全沒更新（程式找不到憑證或沒權限時只會印 warning、不會 crash）。8 個都要加。

### D3. 取得每個 Sheet 的 ID

Sheet 的 ID 是網址中間那段：

```
https://docs.google.com/spreadsheets/d/【這一段就是 SHEET_ID】/edit#gid=0
```

把 8 個 ID 都複製下來，Part E 要填進 `.env`。

---

## Part E — 設定 .env 與 credentials.json

### E1. 建立 .env

```bash
cp .env.example .env
```

然後編輯 `.env`，填入你前面取得的值。完整範本如下（`.env.example` 已更新成這個格式）：

```env
# === 台股 ===
FINMIND_TOKEN=你的_FinMind_Token
SHEET_ID_COMPANY_MASTER=台股公司主檔的SheetID
SHEET_ID_TW_VCP=台股VCP的SheetID
SHEET_ID_TW_SANXIAN=台股三線開花的SheetID
SHEET_ID_VERIFICATION=台股驗證的SheetID

# === 美股 ===
US_SHEET_ID_COMPANY_MASTER=美股公司主檔的SheetID
US_SHEET_ID_VCP=美股VCP的SheetID
US_SHEET_ID_SANXIAN=美股三線開花的SheetID
US_SHEET_ID_VERIFICATION=美股驗證的SheetID

# === 共用 ===
GOOGLE_CREDENTIALS_PATH=credentials.json
US_DATA_PROVIDER=free      # 美股資料源，free=yfinance（免費），保持 free 即可
LOG_LEVEL=INFO
```

### E2. 確認 credentials.json 已就位

```bash
ls -l credentials.json   # 應該看到檔案存在於專案根目錄
```

### E3. 檢查清單

- [ ] `.env` 內 9 個值（1 個 FinMind Token + 8 個 Sheet ID）都填好了
- [ ] `credentials.json` 在專案根目錄
- [ ] 8 個 Sheet 都已把服務帳號 email 加為編輯者
- [ ] Google Cloud 已啟用 Sheets API **和** Drive API

---

## Part F — 初始化資料庫

有兩條路線，**強烈建議路線 2**（如果原作者有提供 DB 備份）。

### 路線 1：從零初始化（全新、無歷史資料）

`init` 會自動建立資料表，再抓取股票清單與約一年的歷史股價。

```bash
source .venv/bin/activate

# 台股初始化（建表 + 股票清單 + 365 天股價 + 大盤指數）
python main.py init

# 美股初始化（⚠️ 約需 30~60 分鐘，因為要抓 ~8000 檔的歷史股價）
python us_main.py init
```

> 美股慢是正常的：約 8000 檔股票，yfinance 採保守批次（每批 40 檔、間隔 15 秒、2 個 worker），以避免被 Yahoo 限流。
>
> 初始化會自動建表（SQLAlchemy `create_tables()`），**不需要**手動執行根目錄那個 `init.sql`（那是早期 PostgreSQL/Docker 留下的，現行架構不用）。

### 路線 2：從 DB 備份還原（推薦，省好幾小時）

如果原作者交付了 `zf_trend_full.db.gz`（台股）與 `zf_trend_us.db.gz`（美股），或它們還在 GitHub Release：

```bash
# 從你的 GitHub Release 下載並還原（前提：Release 已有備份）
gh release download db-backup    -p 'zf_trend_full.db.gz' -D /tmp --clobber
gunzip -c /tmp/zf_trend_full.db.gz > data/zf_trend.db

gh release download us-db-backup -p 'zf_trend_us.db.gz'   -D /tmp --clobber
gunzip -c /tmp/zf_trend_us.db.gz > data/zf_trend_us.db
```

若是拿到實體檔案，直接 `gunzip` 解壓到 `data/` 即可：

```bash
gunzip -c 原作者給的_zf_trend_full.db.gz > data/zf_trend.db
gunzip -c 原作者給的_zf_trend_us.db.gz   > data/zf_trend_us.db
```

> DB 檔很大（台股約 3.9 GB、美股約 950 MB），已被 `.gitignore` 排除，不進版控。

---

## Part G — 本機驗證（確認真的會動）

依序執行，每一步都要通過再往下。

### G1. 健康檢查（DB / Google Sheet / API 連線）

```bash
source .venv/bin/activate
python main.py health
python us_main.py health
```

預期看到 DB 連線正常、Google Sheet 連線正常（✓）、API 正常。
若 Google Sheet 顯示失敗 → 回去檢查 Part C/D（API 沒開、email 沒授權、Sheet ID 填錯）。

### G2. 跑一次每日篩選（會實際寫進 Google Sheet）

```bash
# --force：即使今天不是交易日，也用最近一個交易日的資料跑一次
python main.py daily --force
python us_main.py daily --force
```

跑完打開你的 Google Sheet，應該看到以日期命名的新分頁（例如 `260627`）和選股結果。

### G3. 產生前端 JSON 並預覽網站

```bash
# 從兩個 DB 匯出前端要吃的精簡 JSON 到 site/data/
python scripts/export_to_json_v2.py

# 本機開啟前端（用簡單的 http server，因為前端用 fetch 載 JSON）
cd site && python -m http.server 8000
# 瀏覽器開 http://localhost:8000
```

看到網站能載入、能查詢股票，本機驗證就完成了。

---

## Part H — GitHub 端設定（雲端自動化）

讓系統每天自動跑，需要在 GitHub repo 設定好 Secrets、權限與 Pages。

### H1. 建立你自己的 repo

把程式碼推到**你自己帳號**下的 repo（不要沿用原作者的）。

```bash
gh repo create <你的帳號>/ZF_TrendPicking --private --source=. --remote=origin --push
```

### H2. 設定 10 個 Repository Secrets

到 repo 的 **Settings → Secrets and variables → Actions → New repository secret**，逐一建立以下 **10 個**（名稱要完全一致）：

| Secret 名稱 | 值 | 用於哪些 workflow |
|---|---|---|
| `GOOGLE_CREDENTIALS_JSON` | **整個 credentials.json 的內容**（全部貼上） | daily / us-daily / monthly / us-monthly / export-stock |
| `FINMIND_TOKEN` | 你的 FinMind Token | daily / monthly |
| `SHEET_ID_COMPANY_MASTER` | 台股公司主檔 Sheet ID | daily / monthly |
| `SHEET_ID_TW_VCP` | 台股 VCP Sheet ID | daily / monthly |
| `SHEET_ID_TW_SANXIAN` | 台股三線開花 Sheet ID | daily / monthly |
| `SHEET_ID_VERIFICATION` | 台股驗證 Sheet ID | daily / monthly / export-stock |
| `US_SHEET_ID_COMPANY_MASTER` | 美股公司主檔 Sheet ID | us-daily / us-monthly |
| `US_SHEET_ID_VCP` | 美股 VCP Sheet ID | us-daily / us-monthly |
| `US_SHEET_ID_SANXIAN` | 美股三線開花 Sheet ID | us-daily / us-monthly |
| `US_SHEET_ID_VERIFICATION` | 美股驗證 Sheet ID | us-daily / us-monthly |

> `GOOGLE_CREDENTIALS_JSON` 的設法：直接 `cat credentials.json`，把整段（含大括號）複製貼進 secret 的值。workflow 執行時會把它還原成 runner 上的 `credentials.json` 檔。
>
> `GITHUB_TOKEN` **不用建**，那是 GitHub 自動提供的（用於上傳/下載 Release DB、部署 Pages）。
> 本專案**沒有用到 Repository Variables 或 Environments**，全部走 Secrets。

用 CLI 也可以快速設定：

```bash
gh secret set GOOGLE_CREDENTIALS_JSON < credentials.json
gh secret set FINMIND_TOKEN
gh secret set SHEET_ID_COMPANY_MASTER
# ...其餘 Sheet ID 同理（指令會提示你貼上值）
```

### H3. 開啟 Actions 寫入權限

到 **Settings → Actions → General → Workflow permissions**，選 **Read and write permissions**（DB 備份需要建立/更新 Release，需要寫入權限）。
若 repo 是 fork 來的，確認 Actions 已 **Enable**。

### H4. 設定 GitHub Pages 來源（前端網站）

到 **Settings → Pages → Build and deployment → Source**，選 **GitHub Actions**（**不是** Deploy from a branch）。

> 🔴 沒設這個，`deploy-site` workflow 會部署失敗。前端全部用相對路徑，所以掛在 `https://<你的帳號>.github.io/<repo名>/` 子路徑可正常運作。

### H5. 初始化 Release 的 DB 備份

排程第一次跑時若找不到 DB 備份，會自動 `python main.py init` 重建（美股要等 30-60 分）。為了省時間，建議**手動把本機 DB 上傳到 Release**：

```bash
# 台股
gzip -c data/zf_trend.db > /tmp/zf_trend_full.db.gz
gh release create db-backup /tmp/zf_trend_full.db.gz    --title "DB Backup" --notes "initial" || \
gh release upload db-backup /tmp/zf_trend_full.db.gz --clobber

# 美股
gzip -c data/zf_trend_us.db > /tmp/zf_trend_us.db.gz
gh release create us-db-backup /tmp/zf_trend_us.db.gz --title "US DB Backup" --notes "initial" || \
gh release upload us-db-backup /tmp/zf_trend_us.db.gz --clobber
```

### H6. 手動觸發測試

```bash
# 手動跑一次台股每日（force 忽略假日檢查）
gh workflow run daily.yml --field target_date=2026-06-27 --field force=true

# 看執行狀態
gh run list --workflow=daily.yml

# 跑完後手動觸發前端部署
gh workflow run deploy-site.yml
```

確認 Sheet 有更新、網站能開，GitHub 端就完成了。

---

## 4. 自動化排程一覽

所有排程都在 GitHub Actions（`.github/workflows/`）。時間換算：**台灣時間 = UTC + 8**。

| Workflow | 檔案 | 排程（UTC） | 台灣時間 | 做什麼 |
|---|---|---|---|---|
| 台股每日篩選 | `daily.yml` | 週一~五 09:45 | **17:45** | 抓股價→篩選→寫 Sheet→備份 DB |
| 美股每日篩選 | `us-daily.yml` | 週一~五 21:30 | **隔日 05:30** | 同上（美股） |
| 台股每月更新 | `monthly.yml` | 每月 1 日 01:00 | 每月 1 日 **09:00** | 更新公司主檔清單 |
| 美股每月更新 | `us-monthly.yml` | 每月 1 日 01:30 | 每月 1 日 **09:30** | 更新公司主檔 + 補產業分類 |
| 前端部署 | `deploy-site.yml` | 台股/美股每日完成後自動 | — | 匯出 JSON → 部署 Pages |
| 匯出單股 | `export-stock.yml` | 僅手動 | — | 匯出單一股票到驗證 Sheet |
| 排程測試 | `test-schedule.yml` | 每天 16:15 | 00:15 | 僅測試排程連通性，**可刪除** |

> ⚠️ **不可同時跑兩個同市場的 workflow**（例如同時補跑兩天台股），會搶同一個 Release DB 備份互相覆蓋。補跑多天要**逐個等完成**再觸發下一個。
>
> ⚠️ GitHub 對「連續 60 天沒有 commit」的 repo 會自動停用排程，需偶爾 push 或到 Actions 頁手動重新啟用。

---

## 5. 日常運維指令速查

```bash
# 每次操作前都要先啟用虛擬環境
source .venv/bin/activate

# === 健康檢查 ===
python main.py health
python us_main.py health

# === 手動補跑某一天（雲端）===
gh workflow run daily.yml    --field target_date=2026-03-28 --field force=true
gh workflow run us-daily.yml --field target_date=2026-03-28 --field force=true

# === 本機補資料 ===
python scripts/backfill_all_trading_days.py        # 補齊台股缺漏交易日的篩選結果
python scripts/backfill_all_trading_days_us.py     # 補齊美股
python scripts/backfill_us_prices.py --since 2024-05-01   # 補美股歷史股價

# === 修復資料 ===
python scripts/fix_missing_indicators.py           # 修復缺失的 indicator_json（台股）
python scripts/fix_missing_indicators.py --us      # 美股

# === 資料驗證（4 層檢查）===
python scripts/verify_data.py
python scripts/verify_data.py --us

# === 前端 JSON 匯出 ===
python scripts/export_to_json_v2.py

# === 從線上下載最新 DB（檢查雲端真實資料用）===
gh release download db-backup    -p 'zf_trend_full.db.gz' -D /tmp --clobber && gunzip -f /tmp/zf_trend_full.db.gz
gh release download us-db-backup -p 'zf_trend_us.db.gz'   -D /tmp --clobber && gunzip -f /tmp/zf_trend_us.db.gz
```

> 💡 本機 DB 不會自動更新（排程跑在雲端）。要檢查「線上實際資料」時，務必先從 Release 下載最新 DB，不要看本機過時的。

---

## 6. 安全交接清單（技術轉讓專用，務必執行）

技術轉讓時，賣方的舊憑證必須全部失效、買方換上自己的。**這一章請逐項確認。**

### 交接時要做的事

- [ ] **撤銷上述外洩 PAT**，買方用自己的 `gh auth login`。
- [ ] **輪換 Google 服務帳號金鑰**：買方建立**自己的** Google Cloud 專案 + 服務帳號（Part C），不沿用賣方的 `zf-trend` 專案。舊金鑰交接後應停用/刪除。
- [ ] **8 個 Google Sheet 改用買方自己建立的**（或由賣方轉移擁有權給買方帳號），並把買方服務帳號 email 設為編輯者。
- [ ] **FinMind Token 換成買方自己的帳號**（Part B）。
- [ ] **GitHub repo 轉移給買方帳號**，或買方重新建立 repo 並推送，再重設 10 個 Secrets（Part H2）。
- [ ] 確認 `credentials.json`、`.env` **沒有**被誤上傳到任何 repo（兩者都在 `.gitignore`，但交接時再確認一次：`git ls-files | grep -E 'credentials|\.env$'` 應為空）。
- [ ] 交接後賣方應從本機移除專案的 `credentials.json` 與 `.env`。

---

## 7. 疑難排解 FAQ

**Q：程式跑完但 Google Sheet 沒有任何更新？**
A：最常見三種原因 ——（1）`credentials.json` 不在根目錄或路徑錯；（2）服務帳號 email 沒被加進該 Sheet 的編輯者；（3）Google Drive API 沒啟用。程式找不到憑證時只印 warning 不會 crash，所以容易誤以為成功。先跑 `python main.py health` 確認 Sheet 連線。

**Q：開啟 Sheet 時噴 403 / PermissionError？**
A：99% 是 Drive API 沒開，或 email 沒授權。回 [Part C2](#c2-啟用兩個-api兩個都要不是只有-sheets)、[Part D2](#d2-把服務帳號-email-加為編輯者)。

**Q：美股 init 卡很久是當掉了嗎？**
A：沒有，~8000 檔股票 + 保守批次（避免限流），30-60 分鐘是正常的。可看 `logs/` 確認還在跑。

**Q：雲端排程某天的資料怪怪的 / 沒更新？**
A：先確認沒有「同市場兩個 workflow 同時跑」。再從 Release 下載當天 DB 檢查（見第 5 章）。美股股價當天筆數低於門檻（約 6500）會被判定殘缺並自動重抓，最多重試 3 次。

**Q：GitHub Pages 部署失敗？**
A：確認 Settings → Pages → Source 設為 **GitHub Actions**（[Part H4](#h4-設定-github-pages-來源前端網站)）。

**Q：排程突然不跑了？**
A：GitHub 對 60 天無 commit 的 repo 會停用 scheduled workflow。隨便 push 一個 commit 或到 Actions 頁手動 re-enable。

**Q：yfinance 報錯一堆？**
A：確認版本在 `>=1.0,<2.0`（`pip show yfinance`）。不要升級到 2.x。台股與美股各有獨立的 yfinance client（`api/yfinance_client.py` vs `api/us_stock_client_free.py`），若要改其中一個的邏輯，記得兩端都要對齊。

---

## 附錄 A：環境變數總表

`.env` 由 `config/settings.py`（台股）與 `config/us_settings.py`（美股）讀取。

| 環境變數 | 必填 | 預設值 | 用途 |
|---|---|---|---|
| `FINMIND_TOKEN` | ✅（台股） | 空 | FinMind API（台股股票清單、除權息偵測） |
| `GOOGLE_CREDENTIALS_PATH` | 建議 | `credentials.json` | 服務帳號金鑰路徑 |
| `SHEET_ID_COMPANY_MASTER` | ✅ | 空 | 台股公司主檔 Sheet |
| `SHEET_ID_TW_VCP` | ✅ | 空 | 台股 VCP Sheet |
| `SHEET_ID_TW_SANXIAN` | ✅ | 空 | 台股三線開花 Sheet |
| `SHEET_ID_VERIFICATION` | ✅ | 空 | 台股驗證 Sheet |
| `US_SHEET_ID_COMPANY_MASTER` | ✅ | 空 | 美股公司主檔 Sheet |
| `US_SHEET_ID_VCP` | ✅ | 空 | 美股 VCP Sheet |
| `US_SHEET_ID_SANXIAN` | ✅ | 空 | 美股三線開花 Sheet |
| `US_SHEET_ID_VERIFICATION` | ✅ | 空 | 美股驗證 Sheet |
| `US_DATA_PROVIDER` | — | `free` | 美股資料源；`free`=yfinance（保持即可） |
| `LOG_LEVEL` | — | `INFO` | 日誌級別 DEBUG/INFO/WARNING/ERROR |
| `SQLITE_DB_PATH` | — | `data/zf_trend.db` | 台股 DB 路徑（一般不用改） |
| `US_SQLITE_DB_PATH` | — | `data/zf_trend_us.db` | 美股 DB 路徑（一般不用改） |
| `US_POLYGON_API_KEY` 等 | — | 空 | 付費資料源預留，現在不用 |

> `DATABASE_URL` 是早期 PostgreSQL 殘留，現行 SQLite 架構**不使用**，新 `.env.example` 已移除。

---

## 附錄 B：8 個 Google Sheet 對照表

| 市場 | 環境變數 | 內容 | 分頁命名 |
|---|---|---|---|
| 台股 | `SHEET_ID_COMPANY_MASTER` | 全市場股票清單 + 更新紀錄 | 「台股公司主檔」「台股更新紀錄」 |
| 台股 | `SHEET_ID_TW_VCP` | VCP 每日選股結果 | `YYMMDD` |
| 台股 | `SHEET_ID_TW_SANXIAN` | 三線開花每日選股 | `YYMMDD` |
| 台股 | `SHEET_ID_VERIFICATION` | 四層客觀驗證明細 | `YYMMDD_VCP`、`YYMMDD_三線` |
| 美股 | `US_SHEET_ID_COMPANY_MASTER` | 美股股票清單 + 更新紀錄 | 「美股公司主檔」「美股更新紀錄」 |
| 美股 | `US_SHEET_ID_VCP` | 美股 VCP 每日選股 | `YYMMDD` |
| 美股 | `US_SHEET_ID_SANXIAN` | 美股三線開花每日選股 | `YYMMDD` |
| 美股 | `US_SHEET_ID_VERIFICATION` | 美股驗證明細 | `YYMMDD_VCP`、`YYMMDD_三線` |

---

## 附錄 C：專案檔案結構

```
ZF_TrendPicking/
├── main.py                  # 台股入口（init/daily/monthly/health/backfill/schedule）
├── us_main.py               # 美股入口（同上）
├── config/
│   ├── settings.py          # 台股設定（讀 .env）
│   └── us_settings.py       # 美股設定（讀 .env）
├── api/                     # 資料來源客戶端
│   ├── finmind_client.py    # FinMind（台股清單）
│   ├── hybrid_client.py     # FinMind + yfinance 混合（台股）
│   ├── yfinance_client.py   # yfinance（台股股價）
│   └── us_stock_client_free.py  # yfinance（美股）
├── calculators/             # VCP / 三線開花 篩選演算法
├── data/                    # 資料模型 + SQLite DB（DB 不進版控）
│   ├── models.py            # 台股 4 張表
│   ├── us_models.py         # 美股 5 張表
│   ├── sqlite_database.py   # 台股 DB 操作
│   ├── us_database.py       # 美股 DB 操作
│   ├── zf_trend.db          # 台股 DB（gitignored）
│   └── zf_trend_us.db       # 美股 DB（gitignored）
├── tasks/                   # 每日/每月任務流程
│   ├── daily_task.py / monthly_task.py
│   └── us_daily_task.py / us_monthly_task.py
├── exporters/               # Google Sheet 匯出
│   ├── google_sheet.py / us_google_sheet.py
├── scripts/                 # 維護腳本（backfill / verify / export_to_json_v2 ...）
├── site/                    # 前端靜態網站（index.html + data/，data 由 JSON 匯出產生）
├── utils/                   # 交易日曆、除權息偵測、驗證器等
├── docs/                    # 規格文件 01~05
├── .github/workflows/       # 7 個 GitHub Actions
├── .env                     # 環境變數（gitignored，自己建）
├── .env.example             # 環境變數範本
├── credentials.json         # Google 服務帳號金鑰（gitignored，自己放）
├── requirements.txt         # Python 依賴
├── SETUP.md                 # 簡易設定指南
├── CLAUDE.md                # 給 AI 助手的專案指南
└── HANDOVER.md              # 👈 本文件
```

> 可忽略的歷史遺留：`init.sql`、`Dockerfile`、`docker-compose.yml`、`data/database.py`（PostgreSQL 舊路線，現行不用）、`scripts/export_to_json.py`（v1 舊版，現用 v2）。

---

## 附錄 D：關鍵踩雷集

1. **每次操作前要 `source .venv/bin/activate`**——忘了會用到系統 Python、套件找不到。
2. **Google Drive API 一定要開**（不是只有 Sheets API），否則 `open_by_key` 噴 403。
3. **8 個 Sheet 都要把服務帳號 email 加為編輯者**，漏一個那張就不會更新。
4. **不可同時跑兩個同市場 workflow**，會搶 Release DB 備份互相覆蓋；補跑多天要逐個等完成。
5. **GitHub Pages Source 要設成 GitHub Actions**，不是 branch。
6. **yfinance 鎖 `<2.0`**，不要升級。
7. **本機 DB 會過時**，檢查線上資料要先從 Release 下載最新 DB。
8. **台股/美股的 yfinance client 是兩支獨立檔案**，改邏輯時兩端都要改（2026-05 曾因只改一邊踩雷）。
9. **美股 sector/industry 可能是 NaN**（來自 yfinance），字串欄位要用 `_safe_str()` 處理，不能用 `or` 判斷（NaN 在 Python 是 truthy）。
10. **交接務必撤銷外洩的 PAT、輪換金鑰**（見第 6 章）。

---

*本文件由技術轉移研究產出，涵蓋從零初始化到雲端自動化的完整流程。如程式有更新，請同步維護本文件與 `SETUP.md`。*
