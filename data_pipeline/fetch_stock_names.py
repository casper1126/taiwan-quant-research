"""
data_pipeline/fetch_stock_names.py
─────────────────────────────────────────────────────────────
從 FinMind 抓 TaiwanStockInfo（股票代號 + 名稱）→ 存 data/stock_names.json

用途：
  讓交易清單顯示「2330 台積電」而不只是「2330」

使用：
  export FINMIND_TOKEN="你的 token"
  python data_pipeline/fetch_stock_names.py

輸出：
  data/stock_names.json
  格式: {"2330": "台積電", "2317": "鴻海", ...}

只用 1 個 FinMind API call。
"""
import json
import os
import sys
from pathlib import Path

import requests

# 嘗試自動讀 .env（讓使用者不用手動 export）
try:
    from dotenv import load_dotenv
    ROOT = Path(__file__).parent.parent
    load_dotenv(dotenv_path=ROOT / ".env")
except ImportError:
    pass

OUT_PATH = Path("data/stock_names.json")


FALLBACK_NAMES = {
    # 常見大型股 fallback（FinMind 失敗時用）
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2308": "台達電",
    "2412": "中華電", "3045": "台灣大", "4904": "遠傳",
    "2891": "中信金", "2882": "國泰金", "2881": "富邦金",
    "2884": "玉山金", "2885": "元大金", "2886": "兆豐金", "2887": "台新金",
    "2890": "永豐金", "2892": "第一金", "2883": "開發金",
    "2002": "中鋼", "1301": "台塑", "1303": "南亞", "1326": "台化",
    "2382": "廣達", "2357": "華碩", "2376": "技嘉", "2353": "宏碁",
    "2303": "聯電", "2337": "旺宏", "2347": "聯強", "2049": "上銀",
    "3702": "大聯大", "3711": "日月光投控", "2207": "和泰車", "2615": "萬海",
    "2603": "長榮", "2609": "陽明", "2610": "華航", "2618": "長榮航",
}


def fetch_finmind() -> dict:
    """從 FinMind 抓 TaiwanStockInfo。"""
    token = os.getenv("FINMIND_TOKEN", "").strip()
    if not token:
        print("⚠️  FINMIND_TOKEN 未設定，使用 hardcoded fallback（37 檔）")
        return FALLBACK_NAMES.copy()

    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockInfo",
        "token": token,
    }
    print("📡 從 FinMind 抓 TaiwanStockInfo...")
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"⚠️  FinMind 失敗：{e}")
        print("    使用 hardcoded fallback")
        return FALLBACK_NAMES.copy()

    if data.get("status") != 200:
        print(f"⚠️  FinMind 回傳 {data.get('status')}：{data.get('msg', '')}")
        print("    使用 hardcoded fallback")
        return FALLBACK_NAMES.copy()

    rows = data.get("data", [])
    print(f"✅ 抓到 {len(rows):,} 筆")

    # 取 stock_id + stock_name（去除 ETF / index 雜訊）
    name_map = {}
    for row in rows:
        sid = str(row.get("stock_id", "")).strip()
        name = str(row.get("stock_name", "")).strip()
        if sid and name and sid.isdigit():
            # 同一個 sid 可能有多個（上市/上櫃）→ 第一個贏
            if sid not in name_map:
                name_map[sid] = name

    return name_map


def main():
    name_map = fetch_finmind()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(name_map, f, ensure_ascii=False, indent=2, sort_keys=True)

    print(f"\n💾 {OUT_PATH}")
    print(f"   共 {len(name_map):,} 檔")

    # Sample
    print("\n📋 範例（10 檔）：")
    samples = ["2330", "2317", "3045", "2412", "2002",
               "1301", "2454", "2382", "2308", "2891"]
    for sid in samples:
        n = name_map.get(sid, "(找不到)")
        print(f"  {sid}  {n}")


if __name__ == "__main__":
    main()
