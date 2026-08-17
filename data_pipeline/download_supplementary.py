"""
download_supplementary.py
─────────────────────────────────────────────────────────────
從 FinMind 下載 Task 1 的補充資料集，寫入 SQLite：

  TaiwanStockMarginPurchaseShortSale → margin_trading（融資融券餘額）
  TaiwanStockPrice, data_id="TAIEX"  → market_index（大盤指數）

三大法人買賣超（institutional_investors）已經有專屬的
data_pipeline/download_institutional.py 在維護（斷點續傳/並行/配額都已完成，
且資料已達 Task 1 驗收標準），這裡不重複實作，避免同一份邏輯有兩套實作。

速率控制（照 CLAUDE_CODE_TASKS.md Task 1b 規格，跟 download_institutional.py
的動態配額演算法不同，這裡用文件指定的固定節奏）：
  - 每次 API 呼叫固定 sleep 0.5 秒
  - 同一個「整點時段」內累積滿 550 次呼叫 → 休眠到下個整點

使用方式：
  python download_supplementary.py                  ← 下載全部（增量，融資+TAIEX）
  python download_supplementary.py --sid 2330        ← 只下載指定股票的融資融券
  python download_supplementary.py --force           ← 忽略斷點，強制重新下載
  python download_supplementary.py --skip-margin      ← 只下載 TAIEX
  python download_supplementary.py --skip-index       ← 只下載融資融券

環境變數：
  FINMIND_TOKEN  ← FinMind API token（免費版 600 次/小時）
"""

import os
import sys
import time
import sqlite3
import argparse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import pandas as pd
import requests
from loguru import logger
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from schema import TABLE_DDL, TABLE_INDEXES

# ── 設定 ──────────────────────────────────────────────────────
DB_PATH        = "data/taiwan_stock.db"
FINMIND_URL    = "https://api.finmindtrade.com/api/v4/data"
DATASET_MARGIN = "TaiwanStockMarginPurchaseShortSale"
DATASET_PRICE  = "TaiwanStockPrice"
INDEX_ID       = "TAIEX"
DEFAULT_START  = "2015-01-01"

PER_CALL_SLEEP = 0.5   # 秒/次
HOURLY_LIMIT   = 550   # 每個整點時段的呼叫上限


# ══════════════════════════════════════════════════════════════
# 固定節奏速率限制器（0.5s/call，每小時 550 次後 sleep 到整點）
# ══════════════════════════════════════════════════════════════

class SimpleRateLimiter:
    """
    每次呼叫固定 sleep PER_CALL_SLEEP 秒；若同一個整點時段內已呼叫滿
    HOURLY_LIMIT 次，就睡到下一個整點才繼續（比 download_institutional.py
    的動態配額演算法更保守、也更貼近任務書字面規格）。
    """

    def __init__(self, per_call_sleep: float = PER_CALL_SLEEP,
                 hourly_limit: int = HOURLY_LIMIT):
        self.per_call_sleep = per_call_sleep
        self.hourly_limit   = hourly_limit
        self.call_count     = 0
        self.hour_start     = self._current_hour()

    @staticmethod
    def _current_hour() -> datetime:
        return datetime.now().replace(minute=0, second=0, microsecond=0)

    def acquire(self) -> None:
        now_hour = self._current_hour()
        if now_hour != self.hour_start:
            self.hour_start = now_hour
            self.call_count = 0

        if self.call_count >= self.hourly_limit:
            next_hour = self.hour_start + timedelta(hours=1)
            wait = (next_hour - datetime.now()).total_seconds()
            if wait > 0:
                logger.warning(
                    f"⚠️  本小時已呼叫 {self.call_count} 次（上限 {self.hourly_limit}），"
                    f"休眠 {wait:.0f} 秒至 {next_hour:%H:%M}..."
                )
                time.sleep(wait + 1)
            self.hour_start = self._current_hour()
            self.call_count = 0

        time.sleep(self.per_call_sleep)
        self.call_count += 1


_rate_limiter = SimpleRateLimiter()


# ══════════════════════════════════════════════════════════════
# DB 初始化
# ══════════════════════════════════════════════════════════════

def init_tables(db_path: str) -> None:
    """建立 margin_trading / market_index（沿用 schema.py 的單一事實來源）。"""
    with sqlite3.connect(db_path) as conn:
        conn.execute(TABLE_DDL["margin_trading"])
        conn.execute(TABLE_DDL["market_index"])
        for idx in TABLE_INDEXES:
            conn.execute(idx)
    logger.info("margin_trading / market_index 資料表已確認存在")


# ══════════════════════════════════════════════════════════════
# FinMind API
# ══════════════════════════════════════════════════════════════

def _fetch(dataset: str, data_id: str, start_date: str, end_date: str,
           token: str, retries: int = 3) -> pd.DataFrame:
    """通用 FinMind 呼叫：固定節奏速率限制 + 3 次重試 + 402 特殊處理。"""
    params = {
        "dataset":    dataset,
        "data_id":    data_id,
        "start_date": start_date,
        "end_date":   end_date,
        "token":      token,
    }

    for attempt in range(1, retries + 1):
        _rate_limiter.acquire()
        try:
            resp = requests.get(FINMIND_URL, params=params, timeout=30)
            resp.raise_for_status()
            payload = resp.json()
            status = payload.get("status")

            if status == 200 and payload.get("data"):
                return pd.DataFrame(payload["data"])

            if status == 402:
                logger.warning(
                    f"    [{data_id}] FinMind 402（配額用盡，attempt {attempt}/{retries}），"
                    f"休眠至下個整點..."
                )
                next_hour = _rate_limiter.hour_start + timedelta(hours=1)
                wait = max(60.0, (next_hour - datetime.now()).total_seconds() + 1)
                time.sleep(wait)
                _rate_limiter.hour_start = _rate_limiter._current_hour()
                _rate_limiter.call_count = 0
                continue

            # 無資料或其他訊息（非致命）
            logger.debug(f"    [{data_id}] 無新資料或訊息：{payload.get('message', '')}")
            return pd.DataFrame()

        except requests.exceptions.Timeout:
            logger.warning(f"    [{data_id}] Timeout（attempt {attempt}/{retries}），重試...")
            time.sleep(2 ** attempt)
        except requests.exceptions.HTTPError as e:
            logger.warning(f"    [{data_id}] HTTP 錯誤（{e.response.status_code}），重試...")
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"    [{data_id}] 異常：{type(e).__name__}: {e}")
            return pd.DataFrame()

    logger.warning(f"    [{data_id}] 最終失敗（{retries} 次重試後）")
    return pd.DataFrame()


# ══════════════════════════════════════════════════════════════
# margin_trading（融資融券）
# ══════════════════════════════════════════════════════════════

def _get_last_date_margin(conn: sqlite3.Connection, stock_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) FROM margin_trading WHERE stock_id = ?", (stock_id,)
    ).fetchone()
    if row and row[0]:
        last = datetime.strptime(row[0], "%Y-%m-%d")
        return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def _upsert_margin(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    寫入 margin_trading。FinMind 欄位：
      MarginPurchaseTodayBalance / MarginPurchaseYesterdayBalance → margin_balance / margin_change
      ShortSaleTodayBalance                                       → short_balance
    """
    if df.empty:
        return 0

    df = df.copy()
    required = ["date", "stock_id"]
    for col in required:
        if col not in df.columns:
            logger.warning(f"    缺少欄位：{col}，跳過此批次")
            return 0

    df["date"]     = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["stock_id"] = df["stock_id"].astype(str).str.strip()

    today_bal     = pd.to_numeric(df.get("MarginPurchaseTodayBalance"), errors="coerce")
    yesterday_bal = pd.to_numeric(df.get("MarginPurchaseYesterdayBalance"), errors="coerce")
    short_bal     = pd.to_numeric(df.get("ShortSaleTodayBalance"), errors="coerce")

    df["margin_balance"] = today_bal
    df["short_balance"]  = short_bal
    df["margin_change"]  = today_bal - yesterday_bal

    df = df.dropna(subset=["date", "stock_id"])
    if df.empty:
        return 0
    df = df.drop_duplicates(subset=["date", "stock_id"], keep="last")

    rows = df[["date", "stock_id", "margin_balance", "short_balance", "margin_change"]].values.tolist()
    conn.executemany(
        "INSERT OR REPLACE INTO margin_trading "
        "(date, stock_id, margin_balance, short_balance, margin_change) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def download_margin_one(stock_id: str, db_path: str, token: str,
                         start: str = DEFAULT_START, force: bool = False) -> Dict[str, object]:
    today = date.today().strftime("%Y-%m-%d")
    result = {"stock_id": stock_id, "rows": 0, "status": "ok"}

    with sqlite3.connect(db_path) as conn:
        start_date = start if force else (_get_last_date_margin(conn, stock_id) or start)

    if start_date > today:
        result["status"] = "up_to_date"
        return result

    df = _fetch(DATASET_MARGIN, stock_id, start_date, today, token)
    if df.empty:
        result["status"] = "no_data"
        return result

    with sqlite3.connect(db_path) as conn:
        rows = _upsert_margin(conn, df)
        conn.commit()

    result["rows"] = rows
    return result


# ══════════════════════════════════════════════════════════════
# market_index（TAIEX）
# ══════════════════════════════════════════════════════════════

def _get_last_date_index(conn: sqlite3.Connection, index_id: str = INDEX_ID) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) FROM market_index WHERE index_id = ?", (index_id,)
    ).fetchone()
    if row and row[0]:
        last = datetime.strptime(row[0], "%Y-%m-%d")
        return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def _upsert_index(conn: sqlite3.Connection, df: pd.DataFrame, index_id: str = INDEX_ID) -> int:
    """
    寫入 market_index。TaiwanStockPrice 欄位：
      max/min → high/low，Trading_Volume → volume
    """
    if df.empty:
        return 0

    df = df.copy()
    if "date" not in df.columns:
        return 0

    df["date"]     = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["index_id"] = index_id
    df["open"]     = pd.to_numeric(df.get("open"), errors="coerce")
    df["high"]     = pd.to_numeric(df.get("max"), errors="coerce")
    df["low"]      = pd.to_numeric(df.get("min"), errors="coerce")
    df["close"]    = pd.to_numeric(df.get("close"), errors="coerce")
    df["volume"]   = pd.to_numeric(df.get("Trading_Volume"), errors="coerce")

    df = df.dropna(subset=["date"])
    if df.empty:
        return 0
    df = df.drop_duplicates(subset=["date", "index_id"], keep="last")

    rows = df[["date", "index_id", "open", "high", "low", "close", "volume"]].values.tolist()
    conn.executemany(
        "INSERT OR REPLACE INTO market_index "
        "(date, index_id, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def download_index(db_path: str, token: str, start: str = DEFAULT_START,
                    force: bool = False, index_id: str = INDEX_ID) -> Dict[str, object]:
    today = date.today().strftime("%Y-%m-%d")
    result = {"index_id": index_id, "rows": 0, "status": "ok"}

    with sqlite3.connect(db_path) as conn:
        start_date = start if force else (_get_last_date_index(conn, index_id) or start)

    if start_date > today:
        result["status"] = "up_to_date"
        return result

    df = _fetch(DATASET_PRICE, index_id, start_date, today, token)
    if df.empty:
        result["status"] = "no_data"
        return result

    with sqlite3.connect(db_path) as conn:
        rows = _upsert_index(conn, df, index_id)
        conn.commit()

    result["rows"] = rows
    return result


# ══════════════════════════════════════════════════════════════
# 主下載邏輯
# ══════════════════════════════════════════════════════════════

def get_stock_list(db_path: str) -> List[str]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock_id FROM load_manifest ORDER BY stock_id"
        ).fetchall()
    return [r[0] for r in rows]


def download_all_supplementary(db_path: str, token: str, start: str = DEFAULT_START,
                                force: bool = False, sid_filter: Optional[str] = None,
                                skip_margin: bool = False, skip_index: bool = False) -> None:
    """
    下載 margin_trading（融資融券）與 market_index（TAIEX）。

    Parameters
    ----------
    db_path     : SQLite 路徑
    token       : FinMind API token
    start       : 起始日期（預設 2015-01-01）
    force       : True 時忽略增量，從 start 重新下載
    sid_filter  : 只下載指定股票的融資融券（不影響 TAIEX，指數不分股票）
    skip_margin : 跳過融資融券下載
    skip_index  : 跳過 TAIEX 下載
    """
    init_tables(db_path)

    # ── TAIEX（大盤指數，1 檔，成本很低，不受 --sid 影響）──
    if not skip_index:
        logger.info("📈 下載大盤指數 TAIEX...")
        result = download_index(db_path, token, start, force)
        logger.info(f"   TAIEX：{result['status']}，新增 {result['rows']} 筆")

    # ── margin_trading（融資融券，逐股下載）──
    if not skip_margin:
        stock_ids = [sid_filter] if sid_filter else get_stock_list(db_path)
        if not stock_ids:
            logger.error("❌ load_manifest 為空！請先執行：python run.py --step 1")
            return

        logger.info(
            f"📥 開始下載融資融券資料"
            f"\n   股票數：{len(stock_ids)}"
            f"\n   起始日：{start}"
            f"\n   增量模式：{'否（強制重新下載）' if force else '是'}"
        )

        success, fail, skip, total_rows = 0, 0, 0, 0
        failed_stocks = []
        start_time = time.time()

        for sid in tqdm(stock_ids, desc="融資融券", unit="檔"):
            try:
                result = download_margin_one(sid, db_path, token, start, force)
                status = result["status"]
                rows = result["rows"]
                if status == "ok":
                    success += 1
                    total_rows += rows
                elif status in ("up_to_date", "no_data"):
                    skip += 1
                else:
                    fail += 1
                    failed_stocks.append(sid)
            except Exception as e:
                fail += 1
                failed_stocks.append(sid)
                logger.warning(f"  [{sid}] ✗ 異常：{e}")

        elapsed = time.time() - start_time

        with sqlite3.connect(db_path) as conn:
            total_count, distinct_stocks = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM margin_trading"
            ).fetchone()

        logger.info(
            f"\n✅ 融資融券下載完成"
            f"\n   耗時：{elapsed:.1f}s"
            f"\n   新增：{total_rows:,} 筆"
            f"\n   統計：{distinct_stocks} 檔股票，共 {total_count:,} 筆記錄"
            f"\n   成功：{success} / 失敗：{fail} / 跳過：{skip}"
        )
        if failed_stocks:
            logger.warning(f"⚠️  失敗的股票（{len(failed_stocks)}）：{', '.join(failed_stocks[:10])}")


# ══════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="下載台股融資融券 + TAIEX 大盤指數資料（增量、固定節奏速率控制）"
    )
    parser.add_argument("--sid", type=str, help="只下載指定股票的融資融券，如 2330")
    parser.add_argument("--start", type=str, default=DEFAULT_START,
                         help=f"起始日期，預設 {DEFAULT_START}")
    parser.add_argument("--force", action="store_true", help="忽略斷點續傳，強制從 --start 重新下載")
    parser.add_argument("--skip-margin", action="store_true", help="跳過融資融券")
    parser.add_argument("--skip-index", action="store_true", help="跳過 TAIEX")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    token = os.getenv("FINMIND_TOKEN", "").strip()
    if not token:
        logger.error("❌ 環境變數 FINMIND_TOKEN 未設定")
        sys.exit(1)

    download_all_supplementary(
        db_path     = DB_PATH,
        token       = token,
        start       = args.start,
        force       = args.force,
        sid_filter  = args.sid,
        skip_margin = args.skip_margin,
        skip_index  = args.skip_index,
    )
