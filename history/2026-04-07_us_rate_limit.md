# 美股 yfinance Rate Limit 導致股價不完整

**日期**: 2026-04-07
**市場**: 美股
**嚴重度**: 中 — 股價只抓到 4,956 筆（正常 ~7,000）

---

## 現象

美股每日驗證報告出現：
```
[FAIL] 股價筆數: 4,956 筆 (< 7,000)
```

大量批次下載時 yfinance 回傳 `YFRateLimitError('Too Many Requests')`，
從字母 O 開始的批次幾乎全部失敗（O~W 區段）。

## 根因

美股約 7,000 檔股票，分批次下載（每批 100 檔，間隔 5 秒）。
yfinance（Yahoo Finance）無官方 rate limit 規範，但高頻請求會觸發 429 Too Many Requests。

當天從字母 O 開始連續觸發 rate limit，約 2,000 檔股票沒有被下載到。

## 影響

- 缺失的股票不會出現在篩選結果中（不是算錯，是完全沒算）
- 如果某檔股票本應符合 VCP 或三線開花條件，會被漏掉
- 客觀驗證（L1~L4）全部 PASS，因為驗證只檢查有被篩選出來的股票

## 修正措施

**commit**: `4a8cb8d`

在 `us_daily_task._fetch_and_save_prices()` 中加入重試機制：

```
第一次下載 → 檢查筆數是否 ≥ 5,000
→ 不足 → 找出缺失的股票 → 等 60 秒 → 只重試缺的
→ 還不足 → 等 120 秒 → 再重試
→ 最多 3 次
```

重試只下載缺失的股票（不是全部重跑），避免再次觸發 rate limit。

## 相關參數

| 參數 | 值 | 設定位置 |
|------|-----|---------|
| `US_BATCH_SIZE` | 100 | `config/us_settings.py` |
| `US_BATCH_INTERVAL` | 5 秒 | `config/us_settings.py` |
| `MIN_PRICE_COUNT` | 5,000 | `us_daily_task.py` |
| `MAX_RETRY` | 3 | `us_daily_task.py` |
| 重試間隔 | 60s, 120s, 180s | 遞增 |

## 後續觀察

- 觀察重試機制是否能在 3 次內補齊到 5,000 筆以上
- 如果持續不足，考慮調大 `US_BATCH_INTERVAL`（如 10 秒）
- 或調小 `US_BATCH_SIZE`（如 50 檔/批）

## 相關檔案

- `tasks/us_daily_task.py` — `_fetch_and_save_prices()` 重試邏輯
- `api/us_stock_client_free.py` — `get_stock_price()` 批次下載
- `config/us_settings.py` — 批次參數設定
