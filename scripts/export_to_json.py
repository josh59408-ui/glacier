"""
匯出篩選結果為 JSON 檔，供 GitHub Pages 靜態網站使用

用法：
    python scripts/export_to_json.py

輸出：
    site/data/filter_results.json
"""
import json
import math
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

# 專案根目錄
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

OUTPUT_DIR = BASE_DIR / "site" / "data"


def safe_round(value, digits=2):
    """將數值安全地四捨五入，過濾 None/inf/nan"""
    if value is None:
        return None
    try:
        f = float(value)
        if math.isinf(f) or math.isnan(f):
            return None
        return round(f, digits)
    except (ValueError, TypeError):
        return None


def export_tw(db_path: str) -> list[dict]:
    """匯出台股篩選結果"""
    if not os.path.exists(db_path):
        print(f"⚠️ 台股 DB 不存在: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 檢查 indicator_json 欄位是否存在
    cursor.execute("PRAGMA table_info(filter_result)")
    columns = {row[1] for row in cursor.fetchall()}
    has_indicator = "indicator_json" in columns
    ind_col = ", fr.indicator_json" if has_indicator else ""

    cursor.execute(f"""
        SELECT
            fr.filter_date,
            fr.filter_type,
            fr.stock_id,
            fr.stock_name,
            fr.industry_category,
            fr.return_20d,
            fr.is_strong_list,
            fr.is_new_high_list,
            fr.today_price,
            fr.second_high_55d,
            fr.gap_ratio
            {ind_col}
        FROM filter_result fr
        ORDER BY fr.filter_date DESC, fr.stock_id
    """)

    results = []
    for row in cursor.fetchall():
        record = {
            "market": "tw",
            "date": row["filter_date"],
            "type": row["filter_type"],
            "stock_id": row["stock_id"],
            "stock_name": row["stock_name"],
            "industry": row["industry_category"] or "-",
        }

        if row["filter_type"] == "vcp":
            record["return_20d"] = safe_round(
                row["return_20d"] * 100 if row["return_20d"] is not None else None
            )
            record["is_strong"] = bool(row["is_strong_list"])
            record["is_new_high"] = bool(row["is_new_high_list"])
        else:
            record["today_price"] = safe_round(row["today_price"])
            record["second_high"] = safe_round(row["second_high_55d"])
            record["gap_ratio"] = safe_round(
                row["gap_ratio"] * 100 if row["gap_ratio"] is not None else None
            )

        # 指標值
        ind_raw = row["indicator_json"] if "indicator_json" in row.keys() else None
        if ind_raw:
            try:
                record["ind"] = json.loads(ind_raw)
            except (json.JSONDecodeError, TypeError):
                pass

        results.append(record)

    conn.close()
    print(f"✅ 台股: {len(results)} 筆")
    return results


def export_us(db_path: str) -> list[dict]:
    """匯出美股篩選結果"""
    if not os.path.exists(db_path):
        print(f"⚠️ 美股 DB 不存在: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 檢查 indicator_json 欄位是否存在
    cursor.execute("PRAGMA table_info(us_filter_result)")
    columns = {row[1] for row in cursor.fetchall()}
    has_indicator = "indicator_json" in columns
    ind_col = ", fr.indicator_json" if has_indicator else ""

    cursor.execute(f"""
        SELECT
            fr.filter_date,
            fr.filter_type,
            fr.stock_id,
            fr.stock_name,
            fr.sector,
            fr.return_20d,
            fr.is_strong_list,
            fr.is_new_high_list,
            fr.today_price,
            fr.second_high_55d,
            fr.gap_ratio
            {ind_col}
        FROM us_filter_result fr
        ORDER BY fr.filter_date DESC, fr.stock_id
    """)

    results = []
    for row in cursor.fetchall():
        record = {
            "market": "us",
            "date": row["filter_date"],
            "type": row["filter_type"],
            "stock_id": row["stock_id"],
            "stock_name": row["stock_name"],
            "industry": row["sector"] or "-",
        }

        if row["filter_type"] == "vcp":
            record["return_20d"] = safe_round(
                row["return_20d"] * 100 if row["return_20d"] is not None else None
            )
            record["is_strong"] = bool(row["is_strong_list"])
            record["is_new_high"] = bool(row["is_new_high_list"])
        else:
            record["today_price"] = safe_round(row["today_price"])
            record["second_high"] = safe_round(row["second_high_55d"])
            record["gap_ratio"] = safe_round(
                row["gap_ratio"] * 100 if row["gap_ratio"] is not None else None
            )

        # 指標值
        ind_raw = row["indicator_json"] if "indicator_json" in row.keys() else None
        if ind_raw:
            try:
                record["ind"] = json.loads(ind_raw)
            except (json.JSONDecodeError, TypeError):
                pass

        results.append(record)

    conn.close()
    print(f"✅ 美股: {len(results)} 筆")
    return results


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    tw_db = str(BASE_DIR / "data" / "zf_trend.db")
    us_db = str(BASE_DIR / "data" / "zf_trend_us.db")

    tw_results = export_tw(tw_db)
    us_results = export_us(us_db)

    all_results = tw_results + us_results

    # 建立索引：stock_id -> 出現日期清單（加速前端搜尋）
    stock_index: dict[str, dict] = {}
    for r in all_results:
        sid = r["stock_id"]
        if sid not in stock_index:
            stock_index[sid] = {
                "stock_id": sid,
                "stock_name": r["stock_name"],
                "market": r["market"],
                "industry": r["industry"],
                "appearances": [],
            }
        appearance = {
            "date": r["date"],
            "type": r["type"],
            **(
                {
                    "return_20d": r.get("return_20d"),
                    "is_strong": r.get("is_strong"),
                    "is_new_high": r.get("is_new_high"),
                }
                if r["type"] == "vcp"
                else {
                    "today_price": r.get("today_price"),
                    "second_high": r.get("second_high"),
                    "gap_ratio": r.get("gap_ratio"),
                }
            ),
        }
        if "ind" in r:
            appearance["ind"] = r["ind"]
        stock_index[sid]["appearances"].append(appearance)

    output = {
        "generated_at": date.today().isoformat(),
        "total_records": len(all_results),
        "total_stocks": len(stock_index),
        "stocks": stock_index,
    }

    output_path = OUTPUT_DIR / "filter_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = output_path.stat().st_size / 1024
    print(f"✅ 匯出完成: {output_path} ({size_kb:.1f} KB)")
    print(f"   共 {len(all_results)} 筆紀錄, {len(stock_index)} 檔股票")


if __name__ == "__main__":
    main()
