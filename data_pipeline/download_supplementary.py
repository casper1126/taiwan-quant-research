"""
download_supplementary.py
─────────────────────────────────────────────────────────────
從 FinMind 下載 Task 1 的補充資料集，寫入 SQLite：

  TaiwanStockMarginPurchaseShortSale → margin_trading（融資融券餘額）
  TaiwanStockPrice, data_id="TAIEX"  → market_index（大盤指數）

三大法人買賣超（institutional_investors）已經有專屬的
data_pipeline/download_institutional.py 在維護，這裡不重複實作。

事故後重寫的三個修正（詳見 docs/PROJECT_STATUS.md 的診斷紀錄）：
  1. 互斥鎖（download_lock.py）：同一時間只允許一個下載程序使用 FINMIND_TOKEN，
     避免多個 process 各自以為自己有完整配額，加總超過帳號實際上限。
  2. 402 判定修正：原本 resp.raise_for_status() 會在檢查 JSON status 欄位之前
     先對 HTTP 402 拋出例外，導致「配額用盡」被 generic 的 HTTPError 分支接住，
     只做幾秒鐘的重試就放棄、被上層誤判成「沒有資料」。現在直接檢查
     resp.status_code == 402，並用專屬的 QuotaExhaustedError 往上傳，
     跟「200 但真的没資料」的空 DataFrame 明確分開。
  3. 402 不重試、改成休眠到下個時間窗口自動恢復：偵測到配額用盡就記錄目前的
     checkpoint、計算距離下個整點還有多久、sleep 到那個時間點自動醒來繼續
     （期間定期印心跳 log），不需要人重新執行指令。連續 3 個時間窗口醒來後
     立刻又用盡，才視為異常，正常結束 process 並提示需要人工確認。

速率控制（0.5s/call，同一時間窗口內滿 550 次後主動休眠）用 RateLimiter，
時間窗口長度可調（預設 3600 秒＝1 小時，測試時可以調成幾十秒驗證
休眠/恢復邏輯，不用真的等一小時）。

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
from download_lock import download_lock
from finmind_common import QuotaExhaustedError, MAX_QUOTA_RETRY_CYCLES, sleep_with_heartbeat

# ── 設定 ──────────────────────────────────────────────────────
DB_PATH        = "data/taiwan_stock.db"
FINMIND_URL    = "https://api.finmindtrade.com/api/v4/data"
DATASET_MARGIN = "TaiwanStockMarginPurchaseShortSale"
DATASET_PRICE  = "TaiwanStockPrice"
INDEX_ID       = "TAIEX"
DEFAULT_START  = "2015-01-01"

PER_CALL_SLEEP = 0.5     # 秒/次
HOURLY_LIMIT   = 550     # 每個時間窗口的呼叫上限
WINDOW_SECONDS = 3600    # 時間窗口長度（預設 1 小時；測試時可調小）


# ══════════════════════════════════════════════════════════════
# 固定節奏速率限制器（0.5s/call，時間窗口內滿 550 次後主動休眠）
# ══════════════════════════════════════════════════════════════

class RateLimiter:
    """
    每次呼叫固定 sleep per_call_sleep 秒；同一個時間窗口內已呼叫滿
    limit 次，就主動睡到下一個窗口邊界（而不是等撞到 402 才知道）。

    window_seconds 預設 3600（對齊到整點），測試時可以調小（例如 20 秒）
    搭配調低 limit，在幾十秒內就能驗證完整的休眠/恢復流程。
    """

    def __init__(self, per_call_sleep: float = PER_CALL_SLEEP,
                 limit: int = HOURLY_LIMIT, window_seconds: int = WINDOW_SECONDS):
        self.per_call_sleep = per_call_sleep
        self.limit = limit
        self.window_seconds = window_seconds
        self.call_count = 0
        self.window_start = self._current_window()

    def _current_window(self) -> float:
        now = time.time()
        return now - (now % self.window_seconds)

    def next_window_start(self) -> float:
        return self.window_start + self.window_seconds

    def reset_window(self) -> None:
        """外部（休眠恢復後）呼叫，強制重新對齊到目前的時間窗口。"""
        self.window_start = self._current_window()
        self.call_count = 0

    def acquire(self) -> None:
        now = time.time()
        if now - self.window_start >= self.window_seconds:
            self.reset_window()

        if self.call_count >= self.limit:
            wait = self.next_window_start() - time.time()
            if wait > 0:
                logger.warning(
                    f"⚠️  本時間窗口已呼叫 {self.call_count} 次（上限 {self.limit}），"
                    f"主動休眠 {wait:.0f} 秒..."
                )
                time.sleep(wait + 0.5)
            self.reset_window()

        time.sleep(self.per_call_sleep)
        self.call_count += 1


_rate_limiter = RateLimiter()


def _hibernate_until_next_window(processed: int, total: int, cycle: int) -> None:
    """遇到配額用盡時呼叫：印出清楚訊息、休眠到下個時間窗口、恢復限速器狀態。"""
    wake_at = datetime.fromtimestamp(_rate_limiter.next_window_start()) + timedelta(seconds=5)
    logger.warning(
        f"⏸️  額度耗盡於 {datetime.now():%H:%M:%S}，已處理 {processed}/{total} 檔，"
        f"將於 {wake_at:%H:%M:%S} 自動恢復（第 {cycle}/{MAX_QUOTA_RETRY_CYCLES} 次）"
    )
    sleep_with_heartbeat(wake_at, processed, total)
    _rate_limiter.reset_window()
    logger.info(f"▶️  已恢復，從第 {processed + 1} 檔繼續")


# ══════════════════════════════════════════════════════════════
# DB 初始化
# ══════════════════════════════════════════════════════════════

def init_tables(db_path: str) -> None:
    """
    建立 margin_trading / market_index（沿用 schema.py 的單一事實來源）。

    只建立這兩張表自己的索引，不要把 TABLE_INDEXES 整包跑過去——那個清單
    也包含 daily_price、institutional_investors 等表的索引，這個檔案沒有
    責任、也不該假設那些表一定存在（例如針對這兩張表做隔離測試時）。
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(TABLE_DDL["margin_trading"])
        conn.execute(TABLE_DDL["market_index"])
        for idx in TABLE_INDEXES:
            if "margin_trading" in idx or "market_index" in idx:
                conn.execute(idx)
    logger.info("margin_trading / market_index 資料表已確認存在")


# ══════════════════════════════════════════════════════════════
# FinMind API
# ══════════════════════════════════════════════════════════════

def _fetch(dataset: str, data_id: str, start_date: str, end_date: str,
           token: str, retries: int = 3) -> pd.DataFrame:
    """
    通用 FinMind 呼叫。

    402（HTTP 狀態碼或 JSON status 欄位）一律視為配額用盡，立刻拋出
    QuotaExhaustedError，不重試——由呼叫端決定要不要休眠等待。
    其他錯誤（timeout、5xx、連線異常）維持 3 次重試 + 指數退避。
    回傳空 DataFrame 現在只代表一種情況：FinMind 明確回應「有資料但是空的」，
    是合法結果，不是失敗。
    """
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

            # 檢查真正的 HTTP 402 狀態碼，必須在 raise_for_status() 之前，
            # 否則 raise_for_status() 會先把它變成 generic HTTPError，
            # 402 就跟其他 HTTP 錯誤混在一起被當成暫時性問題重試。
            if resp.status_code == 402:
                raise QuotaExhaustedError(data_id)

            resp.raise_for_status()
            payload = resp.json()
            status = payload.get("status")

            if status == 402:
                # 保留：以防 FinMind 未來改成 HTTP 200 + JSON status:402 的格式
                raise QuotaExhaustedError(data_id)

            if status == 200:
                return pd.DataFrame(payload.get("data") or [])

            logger.debug(f"    [{data_id}] 無新資料或訊息：{payload.get('message', '')}")
            return pd.DataFrame()

        except QuotaExhaustedError:
            raise

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


def _get_min_date_margin(conn: sqlite3.Connection, stock_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MIN(date) FROM margin_trading WHERE stock_id = ?", (stock_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


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
    """
    下載單一股票的融資融券資料。status 可能是：
      "ok"              成功寫入新資料
      "up_to_date"      checkpoint 已經是最新，不用抓
      "no_data"         FinMind 明確回應「有資料但是空的」（例如興櫃股/全額交割股沒有信用交易）
      "quota_exhausted" 402 配額用盡，需要呼叫端休眠後重試——跟 no_data 明確分開，
                        不能被算成「正常跳過」
    """
    today = date.today().strftime("%Y-%m-%d")
    result = {"stock_id": stock_id, "rows": 0, "status": "ok"}

    with sqlite3.connect(db_path) as conn:
        start_date = start if force else (_get_last_date_margin(conn, stock_id) or start)

    if start_date > today:
        result["status"] = "up_to_date"
        return result

    try:
        df = _fetch(DATASET_MARGIN, stock_id, start_date, today, token)
    except QuotaExhaustedError:
        result["status"] = "quota_exhausted"
        return result

    if df.empty:
        result["status"] = "no_data"
        return result

    with sqlite3.connect(db_path) as conn:
        rows = _upsert_margin(conn, df)
        conn.commit()

    result["rows"] = rows
    return result


def download_margin_one_backfill(stock_id: str, db_path: str, token: str,
                                  target_start: str) -> Dict[str, object]:
    """
    往回補單一股票的融資融券資料（跟 download_margin_one 方向相反）：
    只抓「比 DB 現有最早日期更早、直到 target_start」的區間。見
    download_institutional.py 的 download_one_backfill 完整說明，邏輯一致。
    """
    result = {"stock_id": stock_id, "rows": 0, "status": "ok"}

    with sqlite3.connect(db_path) as conn:
        min_date = _get_min_date_margin(conn, stock_id)

    if min_date is None:
        result["status"] = "no_existing_data"
        return result

    if min_date <= target_start:
        result["status"] = "already_covered"
        return result

    end_date = (datetime.strptime(min_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = _fetch(DATASET_MARGIN, stock_id, target_start, end_date, token)
    except QuotaExhaustedError:
        result["status"] = "quota_exhausted"
        return result

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


def _get_min_date_index(conn: sqlite3.Connection, index_id: str = INDEX_ID) -> Optional[str]:
    row = conn.execute(
        "SELECT MIN(date) FROM market_index WHERE index_id = ?", (index_id,)
    ).fetchone()
    return row[0] if row and row[0] else None


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
    """跟 download_margin_one 一樣，status 多了 "quota_exhausted"，跟 no_data 明確分開。"""
    today = date.today().strftime("%Y-%m-%d")
    result = {"index_id": index_id, "rows": 0, "status": "ok"}

    with sqlite3.connect(db_path) as conn:
        start_date = start if force else (_get_last_date_index(conn, index_id) or start)

    if start_date > today:
        result["status"] = "up_to_date"
        return result

    try:
        df = _fetch(DATASET_PRICE, index_id, start_date, today, token)
    except QuotaExhaustedError:
        result["status"] = "quota_exhausted"
        return result

    if df.empty:
        result["status"] = "no_data"
        return result

    with sqlite3.connect(db_path) as conn:
        rows = _upsert_index(conn, df, index_id)
        conn.commit()

    result["rows"] = rows
    return result


def download_index_backfill(db_path: str, token: str, target_start: str,
                             index_id: str = INDEX_ID) -> Dict[str, object]:
    """往回補 TAIEX（只有一個 data_id，不用逐檔迴圈，直接一次查詢）。"""
    result = {"index_id": index_id, "rows": 0, "status": "ok"}

    with sqlite3.connect(db_path) as conn:
        min_date = _get_min_date_index(conn, index_id)

    if min_date is None:
        result["status"] = "no_existing_data"
        return result

    if min_date <= target_start:
        result["status"] = "already_covered"
        return result

    end_date = (datetime.strptime(min_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        df = _fetch(DATASET_PRICE, index_id, target_start, end_date, token)
    except QuotaExhaustedError:
        result["status"] = "quota_exhausted"
        return result

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


def _download_taiex(db_path: str, token: str, start: str, force: bool) -> bool:
    """回傳 True 表示正常結束（含用盡重試上限後放棄），False 表示應該中止整個流程。"""
    cycle = 0
    while True:
        result = download_index(db_path, token, start, force)
        if result["status"] != "quota_exhausted":
            logger.info(f"   TAIEX：{result['status']}，新增 {result['rows']} 筆")
            return True

        cycle += 1
        if cycle > MAX_QUOTA_RETRY_CYCLES:
            logger.error(
                f"❌ TAIEX 下載已嘗試 {MAX_QUOTA_RETRY_CYCLES} 個時間窗口仍配額不足，"
                f"需要人工確認。可執行 python run.py --step 1b 從 checkpoint 續跑。"
            )
            return False
        _hibernate_until_next_window(processed=0, total=1, cycle=cycle)


def _download_margin_all(db_path: str, token: str, start: str, force: bool,
                          sid_filter: Optional[str]) -> None:
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
    failed_stocks: List[str] = []
    start_time = time.time()
    last_heartbeat = time.time()
    quota_cycle = 0

    pbar = tqdm(total=len(stock_ids), desc="融資融券", unit="檔")
    i = 0
    try:
        while i < len(stock_ids):
            sid = stock_ids[i]
            try:
                result = download_margin_one(sid, db_path, token, start, force)
                status = result["status"]

                if status == "quota_exhausted":
                    quota_cycle += 1
                    if quota_cycle > MAX_QUOTA_RETRY_CYCLES:
                        logger.error(
                            f"❌ 已嘗試 {MAX_QUOTA_RETRY_CYCLES} 個時間窗口仍配額不足，需要人工確認。"
                            f"已處理 {i}/{len(stock_ids)} 檔（成功 {success}）。"
                            f"執行 python run.py --step 1b 可從 checkpoint 續跑。"
                        )
                        return
                    _hibernate_until_next_window(processed=i, total=len(stock_ids), cycle=quota_cycle)
                    continue  # 不移動 i，重試同一檔

                if status == "ok":
                    success += 1
                    total_rows += result["rows"]
                elif status in ("up_to_date", "no_data"):
                    skip += 1
                else:
                    fail += 1
                    failed_stocks.append(sid)

            except Exception as e:
                fail += 1
                failed_stocks.append(sid)
                logger.warning(f"  [{sid}] ✗ 異常：{e}")

            quota_cycle = 0
            i += 1
            pbar.update(1)

            if time.time() - last_heartbeat > 600:
                logger.info(f"💓 心跳：{i}/{len(stock_ids)} 檔，✓{success} ⊘{skip} ✗{fail}")
                last_heartbeat = time.time()
    finally:
        pbar.close()

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
        f"\n   成功：{success} / 失敗：{fail} / 跳過（含真無資料）：{skip}"
    )
    if failed_stocks:
        logger.warning(f"⚠️  失敗的股票（{len(failed_stocks)}）：{', '.join(failed_stocks[:10])}")


def _download_taiex_backfill(db_path: str, token: str, target_start: str) -> bool:
    """回傳 True 表示正常結束（含用盡重試上限後放棄），False 表示應該中止整個流程。"""
    cycle = 0
    while True:
        result = download_index_backfill(db_path, token, target_start)
        if result["status"] != "quota_exhausted":
            logger.info(f"   TAIEX 往回補：{result['status']}，新增 {result['rows']} 筆")
            return True

        cycle += 1
        if cycle > MAX_QUOTA_RETRY_CYCLES:
            logger.error(
                f"❌ TAIEX 往回補已嘗試 {MAX_QUOTA_RETRY_CYCLES} 個時間窗口仍配額不足，"
                f"需要人工確認。重新執行同一指令可繼續（已完成的部分不會重抓）。"
            )
            return False
        _hibernate_until_next_window(processed=0, total=1, cycle=cycle)


def _download_margin_backfill_all(db_path: str, token: str, target_start: str,
                                   sid_filter: Optional[str]) -> None:
    stock_ids = [sid_filter] if sid_filter else get_stock_list(db_path)
    if not stock_ids:
        logger.error("❌ load_manifest 為空！請先執行：python run.py --step 1")
        return

    logger.info(
        f"📥 開始往回補融資融券資料"
        f"\n   股票數：{len(stock_ids)}"
        f"\n   目標起始日：{target_start}"
    )

    success, fail, skip, total_rows = 0, 0, 0, 0
    no_existing = 0
    failed_stocks: List[str] = []
    start_time = time.time()
    last_heartbeat = time.time()
    quota_cycle = 0

    pbar = tqdm(total=len(stock_ids), desc="融資融券(往回補)", unit="檔")
    i = 0
    try:
        while i < len(stock_ids):
            sid = stock_ids[i]
            try:
                result = download_margin_one_backfill(sid, db_path, token, target_start)
                status = result["status"]

                if status == "quota_exhausted":
                    quota_cycle += 1
                    if quota_cycle > MAX_QUOTA_RETRY_CYCLES:
                        logger.error(
                            f"❌ 已嘗試 {MAX_QUOTA_RETRY_CYCLES} 個時間窗口仍配額不足，需要人工確認。"
                            f"已處理 {i}/{len(stock_ids)} 檔（成功 {success}）。"
                            f"重新執行同一指令可繼續（已完成的部分不會重抓）。"
                        )
                        return
                    _hibernate_until_next_window(processed=i, total=len(stock_ids), cycle=quota_cycle)
                    continue

                if status == "ok":
                    success += 1
                    total_rows += result["rows"]
                elif status in ("already_covered", "no_data"):
                    skip += 1
                elif status == "no_existing_data":
                    no_existing += 1
                else:
                    fail += 1
                    failed_stocks.append(sid)

            except Exception as e:
                fail += 1
                failed_stocks.append(sid)
                logger.warning(f"  [{sid}] ✗ 異常：{e}")

            quota_cycle = 0
            i += 1
            pbar.update(1)

            if time.time() - last_heartbeat > 600:
                logger.info(
                    f"💓 心跳：{i}/{len(stock_ids)} 檔，"
                    f"✓{success} ⊘{skip} ✗{fail} 略過{no_existing}"
                )
                last_heartbeat = time.time()
    finally:
        pbar.close()

    elapsed = time.time() - start_time

    with sqlite3.connect(db_path) as conn:
        total_count, distinct_stocks = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM margin_trading"
        ).fetchone()
        new_min = conn.execute("SELECT MIN(date) FROM margin_trading").fetchone()[0]

    logger.info(
        f"\n✅ 融資融券往回補完成"
        f"\n   耗時：{elapsed:.1f}s"
        f"\n   新增：{total_rows:,} 筆"
        f"\n   統計：{distinct_stocks} 檔股票，共 {total_count:,} 筆記錄，"
        f"目前最早日期：{new_min}"
        f"\n   成功：{success} / 失敗：{fail} / 已覆蓋或無資料：{skip} / "
        f"略過(原本無此股票資料)：{no_existing}"
    )
    if failed_stocks:
        logger.warning(f"⚠️  失敗的股票（{len(failed_stocks)}）：{', '.join(failed_stocks[:10])}")


def download_all_supplementary_backfill(db_path: str, token: str, target_start: str,
                                         sid_filter: Optional[str] = None,
                                         skip_margin: bool = False,
                                         skip_index: bool = False) -> None:
    """
    往回補資料的批次入口（2026-08-19 資料範圍延伸決定，見
    docs/DATA_AVAILABILITY.md／docs/DECISIONS.md）。只補「比 DB 現有最早
    日期更早、直到 target_start」的區間，不影響既有的 2015-2026 資料。

    全程持有跟 download_all_supplementary() 相同的 download_lock，確保不會
    有一個 process 在往前增量、另一個在往回延伸，同時打同一個 FINMIND_TOKEN。
    """
    with download_lock("download_supplementary.py"):
        init_tables(db_path)

        if not skip_index:
            logger.info("📈 往回補大盤指數 TAIEX...")
            ok = _download_taiex_backfill(db_path, token, target_start)
            if not ok:
                return

        if not skip_margin:
            _download_margin_backfill_all(db_path, token, target_start, sid_filter)


def download_all_supplementary(db_path: str, token: str, start: str = DEFAULT_START,
                                force: bool = False, sid_filter: Optional[str] = None,
                                skip_margin: bool = False, skip_index: bool = False) -> None:
    """
    下載 margin_trading（融資融券）與 market_index（TAIEX）。

    全程持有 download_lock（見 download_lock.py）：同一時間只允許一個下載
    程序執行，避免多個 process 各自以為自己有完整的 FINMIND_TOKEN 配額。

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
    with download_lock("download_supplementary.py"):
        init_tables(db_path)

        if not skip_index:
            logger.info("📈 下載大盤指數 TAIEX...")
            ok = _download_taiex(db_path, token, start, force)
            if not ok:
                return

        if not skip_margin:
            _download_margin_all(db_path, token, start, force, sid_filter)


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
    parser.add_argument(
        "--backfill-to", type=str, default=None,
        help="往回補資料模式：把現有最早日期往前補到這個日期為止"
             "（例如 --backfill-to 2012-05-02），不影響既有資料，忽略 --start/--force"
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    token = os.getenv("FINMIND_TOKEN", "").strip()
    if not token:
        logger.error("❌ 環境變數 FINMIND_TOKEN 未設定")
        sys.exit(1)

    if args.backfill_to:
        download_all_supplementary_backfill(
            db_path      = DB_PATH,
            token        = token,
            target_start = args.backfill_to,
            sid_filter   = args.sid,
            skip_margin  = args.skip_margin,
            skip_index   = args.skip_index,
        )
    else:
        download_all_supplementary(
            db_path     = DB_PATH,
            token       = token,
            start       = args.start,
            force       = args.force,
            sid_filter  = args.sid,
            skip_margin = args.skip_margin,
            skip_index  = args.skip_index,
        )
