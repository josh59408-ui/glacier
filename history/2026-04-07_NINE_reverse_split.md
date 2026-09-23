# NINE 反向合股導致異常報酬率

**日期**: 2026-04-07
**市場**: 美股
**股票**: NINE (Nine Energy Service)
**嚴重度**: 高 — 進入篩選清單，return_20d 顯示 72900%

---

## 現象

美股每日驗證報告出現：
```
[FAIL] 指標合理性: 1 筆異常: ['NINE return_20d=72900.00%']
```

NINE 出現在 VCP 篩選清單中，20 日報酬率 72900%（729 倍），明顯異常。

## 根因分析

### DB 中的資料

```
2026-03-30  close=0.0120  vol=0       ← 合股前（penny stock）
2026-03-31  close=8.2000  vol=83200   ← 合股後（正常股價）
```

DB 混合了合股前（0.012）和合股後（8.20）的價格，導致 return_20d 計算出 72900%。

### 為什麼分割偵測沒抓到

分割偵測的比對邏輯：
```
DB 前一日收盤價 vs yfinance 剛下載的前一日收盤價
→ 差異 > 1% → 標記為分割
```

NINE 的問題：yfinance 在合股後**直接刪除了合股前的所有歷史**。

```
排程下載 start_date=3/30, end_date=3/31
→ yfinance 只回傳 3/31 的資料（8.20）
→ 3/30 的 fresh 價格不存在
→ _fresh_prev_day_prices 裡沒有 NINE
→ 偵測邏輯跳過 NINE
```

### yfinance 資料驗證

```python
yf.Ticker('NINE').history(period='max')
# 結果：只有 5 筆（2026-03-31 ~ 2026-04-07）
# 3/30 之前的歷史完全消失，連 auto_adjust=False 也拿不到
```

Yahoo Finance 對某些 penny stock 合股/SPAC 合併，會徹底移除舊公司歷史。

## 修正措施

### 1. 分割偵測第二層檢查（根本修正）

**commit**: `d80adf8`

新增方法 2：當 yfinance 沒有回傳前一日資料時，用 DB 前一日 vs 今日比對。

```python
# DB 有前一日但 yfinance 沒回傳的股票
missing_from_fresh = set(db_prices.keys()) - set(fresh_prices.keys())
for stock_id in missing_from_fresh:
    ratio = today_close / db_prev
    if ratio > 1.5 or ratio < 0.67:
        # 標記為分割
```

### 2. 分割後刪除舊資料（清理修正）

**commit**: `4afa94f`

偵測到分割後，先 DELETE DB 中該股票的所有舊資料，再 INSERT 新的。
避免 upsert 只覆蓋有新資料的日期，舊的 0.012 殘留。

### 3. 異常報酬率過濾（防護修正）

**commit**: `4a8cb8d`

VCP 篩選增加安全過濾：
- `return_20d > 500%` 或 `< -90%` → 排除篩選
- 即使 DB 有殘留異常資料，也不會進入清單

## 影響範圍

- 只影響 NINE 一檔
- 4/7 的美股 VCP Sheet 有 NINE 且 return_20d 異常
- 其他指標（MA、高低點）也可能失真（0.012 混入計算）

## 預防機制

| 防護層 | 機制 | 效果 |
|--------|------|------|
| 偵測層 | 方法 1（DB vs fresh）+ 方法 2（DB vs today） | 兩層比對，漏網率降低 |
| 清理層 | 偵測後 DELETE → INSERT | 舊資料不殘留 |
| 過濾層 | return_20d > 500% 排除 | 最後防線 |
| 驗證層 | DailyVerifier 指標合理性檢查 | 即使進入清單也會 FAIL 告警 |

## 相關檔案

- `tasks/us_daily_task.py` — `_detect_and_refresh_splits()` 方法 1+2
- `calculators/us_vcp_filter.py` — 異常報酬率過濾
- `calculators/vcp_filter.py` — 台股同步加入過濾
- `utils/us_split_detector.py` — 分割偵測核心邏輯
