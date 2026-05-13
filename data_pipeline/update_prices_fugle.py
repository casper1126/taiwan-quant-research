"""
data_pipeline/update_prices_fugle.py
─────────────────────────────────────────────────────────────
Fugle MarketData REST API 增量更新台股日 OHLCV 資料

為什麼用 Fugle 取代 FinMind 抓股價：
  ① Fugle 是台灣本土券商 API，OHLCV 來源是台交所 → 即時且準
  ② FinMind 偶爾延遲或有缺漏（特別是免費版）
  ③ Fugle 免費版：60 requests/分鐘，OHLCV 完全免費

Fugle 不替代 FinMind 的部分（這些 Fugle 沒有）：
  - PER / PBR (估值)
  - 月營收
  - 三大法人籌碼
  → 這些繼續用 FinMind

API 規格：
  Endpoint: https://api.fugle.tw/marketdata/v1.0/stock/historical/candles/{symbol}
  Header:   X-API-KEY: 你的_key
  Query:    from=YYYY-MM-DD & to=YYYY-MM-DD
  Response: {"symbol": "2330", "type": "EQUITY", "data": [{"date":"...", "open":..., ...}, ...]}

申請 API key：
  https://developer.fugle.tw/  → 註冊 → 「申請 API Key」→ 選擇免費方案

設定：在 .env 加：
  FUGLE_API_KEY=你的_key

使用：
  python data_pipeline/update_prices_fugle.py            # 增量更新到今天
  python data_pipeline/update_prices_fugle.py --force    # 從 2012 年起重抓
  python data_pipeline/update_prices_fugle.py --top 300  # 只更新 top 300 by 流動性
"""
import argparse
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import requests

# 自動讀 .env
try:
    from dotenv import load_dotenv
    ROOT = Path(__file__).parent.parent
    load_dotenv(dotenv_path=ROOT / ".env")
except ImportError:
    ROOT = Path(__file__).parent.parent

# ── 設定 ─────────────────────────────────────────────────────
API_KEY  = os.getenv("FUGLE_API_KEY", "").strip()
BASE_URL = "https://api.fugle.tw/marketdata/v1.0/stock/historical/candles"
DB_PATH  = ROOT / "data" / "taiwan_stock.db"

# Rate limiting（免費版 60/min，留 buffer 用 50）
RATE_LIMIT_PER_MIN = 50
SLEEP_PER_CALL     = 60.0 / RATE_LIMIT_PER_MIN   # 1.2 秒/次

# 預設「活躍宇宙」大小
DEFAULT_TOP_N = 300
WARMUP_START  = "2012-01-01"


# ══════════════════════════════════════════════════════════════
# 1. Fugle API 呼叫
# ══════════════════════════════════════════════════════════════

def fetch_candles(symbol: str, start: str, end: str,
                   max_retries: int = 3) -> List[Dict]:
    """
    從 Fugle 抓股票歷史 K 棒。

    Returns: [{"date": "2026-04-17", "open": 900, ..., "volume": 12345}, ...]
    錯誤處理：
      - 401: API_KEY 無效 → raise
      - 404: 股票不存在或無資料 → 回 []
      - 429: rate limit → sleep 60s 後 retry
      - 500/503: server error → exponential backoff
    """
    url = f"{BASE_URL}/{symbol}"
    params  = {"from": start, "to": end}
    headers = {"X-API-KEY": API_KEY}

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt >= max_retries:
                raise
            time.sleep(2 ** attempt)
            continue

        status = resp.status_code
        if status == 401:
            raise RuntimeError(
                "❌ FUGLE_API_KEY 無效（401）。檢查 .env 是否正確。"
            )
        if status == 404:
            return []
        if status == 429:
            print(f"  ⏸️  {symbol}: rate limit，等 60 秒...")
            time.sleep(60)
            continue
        if status >= 500:
            if attempt >= max_retries:
                resp.raise_for_status()
            time.sleep(2 ** attempt)
            continue

        resp.raise_for_status()
        body = resp.json()
        return body.get("data", [])

    return []


# ══════════════════════════════════════════════════════════════
# 2. DB 互動
# ══════════════════════════════════════════════════════════════

def get_last_date_per_stock(conn: sqlite3.Connection) -> Dict[str, str]:
    """每檔股票的 daily_price 最新日期。"""
    cur = conn.execute(
        "SELECT stock_id, MAX(date) FROM daily_price GROUP BY stock_id"
    )
    return dict(cur.fetchall())


def get_active_universe(conn: sqlite3.Connection,
                         top_n: int,
                         lookback_days: int = 252) -> List[str]:
    """
    依「過去 252 天平均成交金額」取 top_n 檔活躍股。
    避免每次抓 2000 檔太慢且無意義（小型股不交易）。
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    cur = conn.execute("""
        SELECT stock_id, AVG(close * volume) AS dv
        FROM daily_price
        WHERE date > ?
        GROUP BY stock_id
        HAVING dv > 0
        ORDER BY dv DESC
        LIMIT ?
    """, (cutoff, top_n))
    return [r[0] for r in cur.fetchall()]


def upsert_rows(conn: sqlite3.Connection,
                 rows: List[Tuple]) -> int:
    """寫入 daily_price 表（INSERT OR REPLACE）。"""
    if not rows:
        return 0
    conn.executemany("""
        INSERT OR REPLACE INTO daily_price
        (date, stock_id, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    return len(rows)


# ══════════════════════════════════════════════════════════════
# 3. 主流程
# ══════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true",
                    help=f"從 {WARMUP_START} 起重抓（覆蓋現有資料）")
    p.add_argument("--top", type=int, default=DEFAULT_TOP_N,
                    help=f"活躍宇宙大小（預設 {DEFAULT_TOP_N}）")
    p.add_argument("--symbol", type=str,
                    help="只更新這檔股票（測試用，例：2330）")
    return p.parse_args()


def main():
    args = parse_args()

    # ── 前置檢查 ────────────────────────────────────────
    if not API_KEY:
        print("❌ FUGLE_API_KEY 未設定，請在 .env 加入：")
        print("    FUGLE_API_KEY=你的_key")
        print("申請：https://developer.fugle.tw/")
        sys.exit(1)

    if not DB_PATH.exists():
        print(f"❌ 找不到 DB：{DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    today = date.today().strftime("%Y-%m-%d")

    # ── 確定要抓哪些股票 ───────────────────────────────
    if args.symbol:
        symbols = [args.symbol.strip()]
        print(f"🎯 單檔模式：{args.symbol}")
    else:
        symbols = get_active_universe(conn, top_n=args.top)
        print(f"📊 活躍宇宙：{len(symbols)} 檔（top {args.top} by 過去 252 日成交額）")

    # ── 確定每檔的起始日期 ─────────────────────────────
    last_dates = get_last_date_per_stock(conn)
    print(f"📅 今天：{today}")
    if args.force:
        start_for_each = {s: WARMUP_START for s in symbols}
        print(f"🔥 --force 模式：從 {WARMUP_START} 重抓")
    else:
        start_for_each = {}
        already_latest = 0
        for s in symbols:
            last = last_dates.get(s, "2011-12-31")
            next_day = (datetime.strptime(last, "%Y-%m-%d")
                          + timedelta(days=1)).strftime("%Y-%m-%d")
            if next_day > today:
                already_latest += 1
                continue
            start_for_each[s] = next_day
        print(f"⏭️  跳過已是最新的 {already_latest} 檔")
        print(f"📥 需要更新 {len(start_for_each)} 檔")

    # ── 逐檔抓 ──────────────────────────────────────────
    new_rows: List[Tuple] = []
    success_count = 0
    error_count   = 0
    error_log     = []
    t_start = time.time()

    for i, sid in enumerate(start_for_each.keys(), 1):
        start = start_for_each[sid]

        try:
            candles = fetch_candles(sid, start, today)
            for c in candles:
                d = c.get("date")
                o = c.get("open")
                h = c.get("high")
                l = c.get("low")
                cl = c.get("close")
                v = c.get("volume", 0)
                if d and cl is not None:
                    new_rows.append((d, sid, o, h, l, cl, v))
            success_count += 1
        except RuntimeError:
            raise   # 401 直接停掉
        except Exception as e:
            error_count += 1
            error_log.append(f"  ⚠️ {sid}: {e}")

        # Rate limiting
        time.sleep(SLEEP_PER_CALL)

        # 進度
        if i % 25 == 0 or i == len(start_for_each):
            elapsed = time.time() - t_start
            eta = elapsed / i * (len(start_for_each) - i) if i > 0 else 0
            print(f"  進度 {i}/{len(start_for_each)} | "
                  f"成功 {success_count} | 失敗 {error_count} | "
                  f"已用 {elapsed:.0f}s | 剩 ~{eta:.0f}s")

    # ── 寫入 DB ─────────────────────────────────────────
    n_inserted = upsert_rows(conn, new_rows)
    elapsed_total = time.time() - t_start

    print()
    print("=" * 60)
    print("  ✅ Fugle 增量更新完成")
    print("=" * 60)
    print(f"  總耗時：{elapsed_total:.0f} 秒")
    print(f"  成功 / 失敗：{success_count} / {error_count}")
    print(f"  寫入 daily_price：{n_inserted:,} 筆")

    # 印出最新日期摘要
    cur = conn.execute("SELECT MAX(date), COUNT(DISTINCT stock_id) FROM daily_price")
    latest_date, n_stocks = cur.fetchone()
    print(f"  DB 最新日期：{latest_date}")
    print(f"  DB 涵蓋股票：{n_stocks:,}")

    if error_log:
        print(f"\n  錯誤摘要（前 10 個）：")
        for line in error_log[:10]:
            print(line)

    conn.close()


if __name__ == "__main__":
    main()
