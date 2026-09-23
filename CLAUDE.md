# ZF_TrendPicking 專案指南

## 虛擬環境（重要！）

**啟動專案前必須先啟用虛擬環境：**

```bash
source .venv/bin/activate
```

所有 Python 指令都需要在虛擬環境中執行。

---

## 專案架構

此專案同時支援 **台股** 和 **美股** 技術分析篩選，兩者完全獨立隔離。

### 台股系統
- **主程式**：`python main.py`
- **資料庫**：`data/zf_trend.db`
- **設定**：`config/settings.py`

### 美股系統
- **主程式**：`python us_main.py`
- **資料庫**：`data/zf_trend_us.db`（獨立）
- **設定**：`config/us_settings.py`

### 前端查詢網站
- **網址**：GitHub Pages 自動部署
- **架構**：純靜態 HTML + JS，資料拆分為 `index.json` + 月份 JSON + 指標 JSON
- **資料產生**：`scripts/export_to_json_v2.py`（從 DB 匯出拆分 JSON）
- **部署流程**：`.github/workflows/deploy-site.yml`（每日篩選完成後自動觸發）

---

## 常用指令

### 台股
```bash
source .venv/bin/activate

# 初始化
python main.py init

# 每日篩選
python main.py daily

# 健康檢查
python main.py health
```

### 美股
```bash
source .venv/bin/activate

# 初始化（首次執行，約 30-60 分鐘）
python us_main.py init

# 每日篩選
python us_main.py daily

# 健康檢查
python us_main.py health
```

### 前端 JSON 匯出
```bash
# 從 DB 產生拆分 JSON（月份 + 指標）
python scripts/export_to_json_v2.py
```

### 資料維護腳本
```bash
# 補齊所有交易日篩選結果（台股）
python scripts/backfill_all_trading_days.py

# 補齊所有交易日篩選結果（美股）
python scripts/backfill_all_trading_days_us.py

# 補齊美股歷史股價（回溯至 2024-05）
python scripts/backfill_us_prices.py

# 修復缺失的 indicator_json
python scripts/fix_missing_indicators.py
python scripts/fix_missing_indicators.py --us

# 重新匯出篩選結果到 Google Sheet（從 DB 讀取，不重算）
python scripts/reexport_all_dates.py --from-db

# 資料驗證（4 層檢查）
python scripts/verify_data.py
python scripts/verify_data.py --us
```

---

## 環境變數

在 `.env` 中設定：

```env
# === 台股 ===
FINMIND_TOKEN=<FinMind API Token>
SHEET_ID_COMPANY_MASTER=<台股公司主檔 Sheet ID>
SHEET_ID_TW_VCP=<台股 VCP Sheet ID>
SHEET_ID_TW_SANXIAN=<台股三線開花 Sheet ID>
SHEET_ID_VERIFICATION=<台股驗證 Sheet ID>

# === 美股 ===
US_SHEET_ID_COMPANY_MASTER=<美股公司主檔 Sheet ID>
US_SHEET_ID_VCP=<美股 VCP Sheet ID>
US_SHEET_ID_SANXIAN=<美股三線開花 Sheet ID>
US_SHEET_ID_VERIFICATION=<美股驗證 Sheet ID>
```

---

## 前端架構（v2 拆分 JSON）

前端採用 lazy loading 架構，避免一次載入所有資料：

| 檔案 | 大小 | 說明 |
|------|------|------|
| `site/data/index.json` | ~1.2 MB | 股票主檔 + 月份清單 + 資料範圍 |
| `site/data/months/{YYYY-MM}.json` | ~1-2 MB/月 | 該月篩選結果 |
| `site/data/indicators/{YYYY-MM}.json` | ~3 MB/月 | 指標 tooltip 資料（點擊 tag 時載入） |

前端特性：
- 首次載入只下載 `index.json` + 最近 2 個月
- 搜尋股票使用 `STOCK_INDEX` 反向索引（O(1) 查找）
- 搜尋輸入 300ms debounce + 限制 50 筆結果
- Tag 指標 tooltip 按月快取，點擊時才載入
- 新/舊股票標記（與前一交易日比較）
- 排序：綜合、新股優先、20日漲幅、突破差距
- Google Sheet 匯出也有新/舊股票背景色標記

---

## Google Sheet 匯出邏輯

### 排序規則
Sheet 匯出時先依**顏色**排序（新股在前、舊股在後），再在每組內依原始指標排序：
- **VCP**：新股（白色）按近 20 日漲幅降冪 → 舊股（灰色）按近 20 日漲幅降冪
- **三線開花**：新股按差距比例降冪 → 舊股按差距比例降冪

若無歷史比較資料（`prev_stock_ids` 為 None），則退回純指標排序。

### 新/舊股票判定（Google Sheet）
- 與**近 20 個交易日**（不含當天）**同類型**的篩選結果**聯集**比較（VCP 比 VCP、三線比三線）
- 新股（白底）：近 20 個交易日**首次出現**（不在聯集中）
- 舊股（灰底）：近 20 個交易日內**曾出現過**（在聯集中）
- 實作：`_get_recent_stock_ids`（`tasks/daily_task.py`、`tasks/us_daily_task.py`、`scripts/reexport_all_dates.py`，lookback=20 交易日）
- 前端網站（`site/index.html`）的新舊標記也同步為近 20 交易日（跨類型合併比較，實作 `getRecentStockIds`，並擴大月份預載至前兩月以涵蓋比較範圍）

### 安全值處理
- 數值欄位：`_safe_val()` 處理 NaN/inf → 空字串
- 字串欄位（美股 sector/industry）：`_safe_str()` 處理 NaN → `"-"`
  - **注意**：NaN 在 Python 中是 truthy，`nan or "-"` 會回傳 nan，不能用 `or` 判斷

---

## GitHub Actions CI/CD

### Workflow 列表

| Workflow | 觸發時機 | 說明 |
|----------|---------|------|
| `daily.yml` | 週一~五 UTC 09:45 / 手動 | 台股每日篩選 + Sheet 匯出 + DB 備份 |
| `us-daily.yml` | 週一~五 UTC 21:30 / 手動 | 美股每日篩選 + Sheet 匯出 + DB 備份 |
| `monthly.yml` | 每月 1 日 UTC 01:00 / 手動 | 台股每月公司主檔更新 |
| `us-monthly.yml` | 每月 1 日 UTC 01:30 / 手動 | 美股每月公司主檔更新 |
| `deploy-site.yml` | 每日篩選後自動 / 手動 | 前端靜態網站部署到 GitHub Pages |
| `export-stock.yml` | 手動 | 匯出股票資料 |

### 手動觸發
```bash
# 台股指定日期（force 忽略假日檢查）
gh workflow run daily.yml --field target_date=2026-03-28 --field force=true

# 美股指定日期
gh workflow run us-daily.yml --field target_date=2026-03-28 --field force=true
```

### DB 備份機制
- 台股 DB 備份到 Release `db-backup`（`zf_trend_full.db.gz`）
- 美股 DB 備份到 Release `us-db-backup`（`zf_trend_us.db.gz`）
- 每次 daily task 完成後自動壓縮上傳，下次 run 自動下載還原
- **重要**：不可同時平行跑多個相同市場的 workflow，否則 DB 備份會互相覆蓋

### 美股股價完整性保護
`tasks/us_daily_task.py` 中，當天股價筆數低於 `MIN_PRICE_COUNT = 6,500` 筆時視為不完整，會強制重新下載（正常交易日約 6,400~6,800 筆）。避免殘缺 DB 被錯誤跳過。

---

## 美股新增檔案（16 個）

| 檔案 | 用途 |
|------|------|
| `config/us_settings.py` | 美股專用設定 |
| `data/us_models.py` | 美股資料模型 |
| `data/us_database.py` | 美股資料庫操作 |
| `utils/us_trading_calendar.py` | 美股交易日曆 |
| `utils/us_split_detector.py` | 美股分割/合股偵測（第二層：fresh vs DB 比對） |
| `utils/internal_split_detector.py` | 美股內部分割偵測（第三層：DB 相鄰價格跳動 + 白名單） |
| `api/us_stock_client.py` | 美股 API 抽象介面 |
| `api/us_stock_client_free.py` | 免費版（yfinance） |
| `api/us_stock_client_paid.py` | 付費版預留 |
| `calculators/us_moving_average.py` | 美股均線計算 |
| `calculators/us_vcp_filter.py` | 美股 VCP 篩選 |
| `calculators/us_sanxian_filter.py` | 美股三線開花篩選 |
| `tasks/us_daily_task.py` | 美股每日任務 |
| `tasks/us_monthly_task.py` | 美股每月任務 |
| `exporters/us_google_sheet.py` | 美股 Sheet 匯出 |
| `us_main.py` | 美股主程式入口 |

---

## 注意事項

1. **完全隔離**：美股功能不會影響台股，反之亦然
2. **虛擬環境**：每次操作前務必先 `source .venv/bin/activate`
3. **資料來源**：台股使用 FinMind + yfinance 備援，美股使用 yfinance
4. **前端部署**：每日篩選完成後自動觸發 `Deploy Site` workflow，也可手動觸發
5. **新/舊標記**：前端跨類型比較（VCP+三線合併），Google Sheet 同類型獨立比較
6. **不可平行跑同市場 workflow**：多個 run 會搶同一個 Release DB 備份，導致資料互相覆蓋。補跑多天時必須逐個等完成再觸發下一個
7. **美股 NaN 安全**：美股的 sector/industry 可能為 NaN（來自 yfinance），所有字串欄位需用 `_safe_str()` 處理，不可用 `or` 判斷
