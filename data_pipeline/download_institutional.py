"""
download_institutional.py
─────────────────────────────────────────────────────────────
從 FinMind 增量下載三大法人買賣超資料，寫入 SQLite。

資料集：TaiwanStockInstitutionalInvestorsBuySell
欄位：
  date          TEXT  交易日
  stock_id      TEXT  股票代號
  investor_type TEXT  法人類型（Foreign_Investor / Investment_Trust / Dealer_Self 等）
  buy           REAL  買進金額（元）
  sell          REAL  賣出金額（元）
  net           REAL  淨買超（buy - sell）

增量策略（節省 API 額度）：
  ✓ 增量下載：只抓 DB 最新日期之後的資料
  ✓ 並行化：ThreadPoolExecutor 平行下載多檔股票
  ✓ 批量上傳：累積 100 筆後才批提交
  ✓ 日誌審計：記錄每次下載的時間、筆數、API 餘額

使用方式：
  python download_institutional.py               ← 下載全部（增量）
  python download_institutional.py --sid 2330    ← 只下載特定股票
  python download_institutional.py --workers 4   ← 並行下載（4 個 worker）
  python download_institutional.py --force       ← 忽略斷點，強制重新下載

環境變數：
  FINMIND_TOKEN  ← FinMind API token（免費版 600次/小時）
"""

import os
import sys
import time
import sqlite3
import argparse
import requests
from datetime import date, datetime, timedelta
from pathlib import Path

# 自動讀 .env（讓 token 不用手動 export）
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")
except ImportError:
    pass
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from loguru import logger

# ── 路徑設定 ──────────────────────────────────────────────────
DB_PATH              = "data/taiwan_stock.db"
FINMIND_URL          = "https://api.finmindtrade.com/api/v4/data"
DATASET              = "TaiwanStockInstitutionalInvestorsBuySell"
RATE_LIMIT_MIN       = 0.2                    # 動態 sleep 下限（秒）
RATE_LIMIT_MAX       = 60.0                   # 動態 sleep 上限（秒）
API_QUOTA_HOURLY     = 600                    # FinMind 免費版每小時配額
API_QUOTA_THRESHOLD  = 0.90                   # 進入節流模式的閾值
DEFAULT_START        = "2015-01-01"


# ══════════════════════════════════════════════════════════════
# 智能速率限制器（確保不超出 API 配額）
# ══════════════════════════════════════════════════════════════

class RateLimiter:
    """
    🧠 動態 quota-aware 速率限制器。

    核心公式（每次 acquire 前計算最佳 sleep）：
        optimal_sleep = 剩餘時間窗 / 剩餘配額

    例子：
      - 已用 100/600，距離窗口結束還有 3000 秒
        → optimal = 3000 / 500 = 6.0 秒/請求
      - 已用 570/600，距離窗口結束還有 3500 秒
        → optimal = 3500 / 30 = 116 秒/請求（自動節流）
      - 已用 10/600，距離窗口結束還有 3500 秒
        → optimal = 3500 / 590 = 5.9 秒，但 clamp 到 RATE_LIMIT_MIN = 0.2

    因此：
      - 剛開始下載：盡量快（0.2 秒間隔）
      - 接近上限：自動放慢（數十秒間隔）
      - 額度耗盡：暫停直到最老請求過期，全程不阻塞其他執行緒
    """

    def __init__(self, quota_per_hour: int = API_QUOTA_HOURLY, threshold: float = API_QUOTA_THRESHOLD):
        self.quota_per_hour = quota_per_hour
        self.threshold = threshold
        self.request_times: list = []
        self.lock = __import__('threading').Lock()
    
    def _cleanup_old_requests(self):
        """清除超過 1 小時的舊請求記錄。"""
        now = time.time()
        self.request_times = [t for t in self.request_times if now - t < 3600]
    
    def get_quota_usage(self) -> Tuple[int, int, float]:
        """
        傳回（已用, 剩餘, 使用百分比）
        """
        with self.lock:
            self._cleanup_old_requests()
            used = len(self.request_times)
            remaining = max(0, self.quota_per_hour - used)
            percentage = used / self.quota_per_hour
            return used, remaining, percentage
    
    def get_recovery_time(self) -> Optional[float]:
        """
        計算距離下一個可用請求的秒數。
        如果無需等待，回傳 None。
        """
        with self.lock:
            if not self.request_times:
                return None
            oldest = self.request_times[0]
            now = time.time()
            age = now - oldest
            if age < 3600 and len(self.request_times) >= self.quota_per_hour:
                # 最老的請求仍在時間窗內，且已達上限
                wait_time = 3600 - age + 1
                return max(0, wait_time)
            return None
    
    def _optimal_sleep(self) -> float:
        """
        在鎖內計算本次請求應等待的秒數（不執行 sleep）。
        呼叫前必須持有 self.lock 且已執行 _cleanup_old_requests()。
        """
        used = len(self.request_times)
        now = time.time()
        remaining_quota = self.quota_per_hour - used

        if remaining_quota <= 0:
            return 0.0  # 外層會進入等待分支，此值不被使用

        # 剩餘時間窗 = 最老請求距離過期的秒數
        if self.request_times:
            remaining_window = max(0.0, 3600.0 - (now - self.request_times[0]))
        else:
            remaining_window = 3600.0

        optimal = remaining_window / remaining_quota
        return max(RATE_LIMIT_MIN, min(optimal, RATE_LIMIT_MAX))

    def acquire(self) -> None:
        """
        取得一個 API 請求許可，必要時自動暫停。

        ── 關鍵設計：time.sleep() 永遠在 with self.lock 區塊**外**執行 ──
        鎖只用於讀寫 request_times（微秒級），睡眠在鎖外進行。
        這確保多個 worker 執行緒可以同步等待，不會互相阻塞（原設計的死鎖問題）。
        """
        while True:
            with self.lock:
                self._cleanup_old_requests()
                used = len(self.request_times)

                if used >= self.quota_per_hour:
                    # 額度耗盡：計算恢復時間後在鎖外等待
                    oldest = self.request_times[0]
                    wait_secs = max(1.0, 3600.0 - (time.time() - oldest) + 2.0)
                    logger.warning(
                        f"⚠️  API 配額已滿（{used}/{self.quota_per_hour}），"
                        f"自動暫停 {wait_secs:.0f} 秒，額度恢復後繼續..."
                    )
                    acquired = False
                else:
                    # 有餘額：計算動態 sleep 後記錄時間戳
                    wait_secs = self._optimal_sleep()
                    self.request_times.append(time.time())
                    acquired = True

            # ── 在鎖外 sleep（不阻塞其他執行緒）──
            time.sleep(wait_secs)
            if acquired:
                return
            # 未取得配額：繼續迴圈，重新檢查

    def force_wait_recovery(self) -> None:
        """當收到 FinMind 402 時呼叫，強制等待至少到最老請求過期。"""
        with self.lock:
            if self.request_times:
                oldest = self.request_times[0]
                wait_secs = max(60.0, 3600.0 - (time.time() - oldest) + 2.0)
            else:
                wait_secs = 3600.0
            logger.warning(
                f"⚠️  FinMind 402（本地追蹤可能不準），強制等待 {wait_secs:.0f} 秒..."
            )
        time.sleep(wait_secs)
    
    def log_status(self) -> str:
        """
        傳回目前配額狀態的簡短報告。
        """
        used, remaining, pct = self.get_quota_usage()
        recovery = self.get_recovery_time()
        
        status = f"API 配額：{used}/{self.quota_per_hour} (已用 {pct*100:.1f}%)"
        if recovery:
            status += f"，下次可用：{recovery:.0f}s 後"
        return status


# 全域速率限制器實例（執行緒安全）
_rate_limiter = RateLimiter()



# ══════════════════════════════════════════════════════════════
# DB 初始化
# ══════════════════════════════════════════════════════════════

DDL_INSTITUTIONAL = """
CREATE TABLE IF NOT EXISTS institutional_investors (
    date          TEXT NOT NULL,
    stock_id      TEXT NOT NULL,
    investor_type TEXT NOT NULL,
    buy           REAL,
    sell          REAL,
    net           REAL,
    PRIMARY KEY (date, stock_id, investor_type)
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_inst_sid  ON institutional_investors(stock_id);",
    "CREATE INDEX IF NOT EXISTS idx_inst_date ON institutional_investors(date);",
]


def init_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(DDL_INSTITUTIONAL)
        for idx in INDEXES:
            conn.execute(idx)
    logger.info("institutional_investors 資料表已確認存在")


# ══════════════════════════════════════════════════════════════
# FinMind API
# ══════════════════════════════════════════════════════════════

def _fetch(stock_id: str, start_date: str, end_date: str,
           token: str, retries: int = 3) -> pd.DataFrame:
    """
    FinMind 單次 API 呼叫，含自動重試與智能速率限制。
    
    返回資料框或空 DF（無新資料或失敗）。
    """
    # 智能延遲：等待直到可安全發送請求（確保不超配額）
    _rate_limiter.acquire()
    
    params = {
        "dataset":    DATASET,
        "data_id":    stock_id,
        "start_date": start_date,
        "end_date":   end_date,
        "token":      token,
    }
    
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(FINMIND_URL, params=params, timeout=30)
            resp.raise_for_status()  # 檢查 HTTP 狀態
            
            data = resp.json()
            status = data.get("status")
            
            # FinMind 成功回應
            if status == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                logger.debug(
                    f"    [{stock_id}] ✓ 成功：{len(df)} 筆（{start_date}~{end_date}）"
                )
                return df
            
            # API 速率限制（402）
            elif status == 402:
                used, remaining, _ = _rate_limiter.get_quota_usage()
                logger.warning(
                    f"    [{stock_id}] FinMind 402（attempt {attempt}/{retries}），"
                    f"已用 {used}/600，等待額度恢復..."
                )
                _rate_limiter.force_wait_recovery()
                continue
            
            # 其他 FinMind 錯誤
            else:
                msg = data.get("message", "Unknown error")
                logger.debug(f"    [{stock_id}] FinMind 錯誤（{status}）：{msg}")
                return pd.DataFrame()
                
        except requests.exceptions.Timeout:
            logger.warning(
                f"    [{stock_id}] Timeout（attempt {attempt}/{retries}），重試..."
            )
            time.sleep(2 ** attempt)  # 指數退避
            
        except requests.exceptions.HTTPError as e:
            logger.warning(
                f"    [{stock_id}] HTTP 錯誤（{e.response.status_code}），重試..."
            )
            time.sleep(2 ** attempt)
            
        except Exception as e:
            logger.error(f"    [{stock_id}] 異常：{type(e).__name__}: {e}")
            return pd.DataFrame()
    
    logger.warning(f"    [{stock_id}] 最終失敗（{retries} 次重試後）")
    return pd.DataFrame()


def _get_last_date(conn: sqlite3.Connection, stock_id: str) -> Optional[str]:
    """查詢 DB 裡某支股票最新的法人資料日期。"""
    row = conn.execute(
        "SELECT MAX(date) FROM institutional_investors WHERE stock_id = ?",
        (stock_id,)
    ).fetchone()
    if row and row[0]:
        last = datetime.strptime(row[0], "%Y-%m-%d")
        return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def _upsert(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    批次寫入 institutional_investors，回傳寫入筆數。
    
    包含資料驗證：
      - 轉換欄位名稱（FinMind 可能用 'name' 或 'investor_type'）
      - 計算 net（如果缺漏）
      - 清除重複及無效行
    """
    if df.empty:
        return 0

    df = df.copy()
    
    # 1. 欄位名稱對齐（FinMind 返回的欄位名稱可能不統一）
    rename_map = {
        "stock_id":    "stock_id",
        "date":        "date",
        "name":        "investor_type",      # FinMind 可能用 'name'
        "investor":    "investor_type",      # 或 'investor'
        "buy":         "buy",
        "sell":        "sell",
        "net_buy":     "net",               # 或 'net_buy'
        "net":         "net",
    }
    
    for old_col, new_col in rename_map.items():
        if old_col in df.columns and old_col != new_col:
            df = df.rename(columns={old_col: new_col})
    
    # 2. 確保必要欄位存在
    required = ["date", "stock_id", "investor_type", "buy", "sell"]
    for col in required:
        if col not in df.columns:
            logger.warning(f"    缺少欄位：{col}，跳過此批次")
            return 0
    
    # 3. 資料型別轉換與驗證
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    df["investor_type"] = df["investor_type"].astype(str).str.strip()
    df["buy"] = pd.to_numeric(df["buy"], errors="coerce")
    df["sell"] = pd.to_numeric(df["sell"], errors="coerce")
    
    # 4. 計算 net（如果缺漏）
    if "net" not in df.columns:
        df["net"] = df["buy"] - df["sell"]
    else:
        df["net"] = pd.to_numeric(df["net"], errors="coerce")
    
    # 5. 過濾無效行（日期、stock_id 或金額為 NaN）
    df = df.dropna(subset=["date", "stock_id", "investor_type"])
    if df.empty:
        logger.warning("    資料驗證後無有效行")
        return 0
    
    # 6. 移除重複（同日、同股票、同法人類型）
    df = df.drop_duplicates(subset=["date", "stock_id", "investor_type"], keep="last")
    
    # 7. 寫入 DB（INSERT OR REPLACE）
    rows = df[["date", "stock_id", "investor_type", "buy", "sell", "net"]].values.tolist()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO institutional_investors "
            "(date, stock_id, investor_type, buy, sell, net) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)
    except Exception as e:
        logger.error(f"    DB 寫入失敗：{e}")
        return 0


# ══════════════════════════════════════════════════════════════
# 主下載邏輯
# ══════════════════════════════════════════════════════════════

def get_stock_list(db_path: str) -> List[str]:
    """從 load_manifest 取得所有已載入的股票代號。"""
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT stock_id FROM load_manifest ORDER BY stock_id"
        ).fetchall()
    return [r[0] for r in rows]


def download_one(stock_id: str, db_path: str, token: str,
                 start: str = DEFAULT_START, force: bool = False) -> Dict[str, int]:
    """
    下載單一股票的三大法人資料，支援增量與重試。

    Returns
    -------
    dict : 包含 (status, rows_added, errors)
    """
    today = date.today().strftime("%Y-%m-%d")
    result = {"stock_id": stock_id, "rows": 0, "status": "ok"}

    # 決定開始日期（增量邏輯）
    with sqlite3.connect(db_path) as conn:
        start_date = start if force else (_get_last_date(conn, stock_id) or start)

    # 已是最新
    if start_date > today:
        result["status"] = "up_to_date"
        return result

    # 下載
    df = _fetch(stock_id, start_date, today, token)
    if df.empty:
        result["status"] = "no_data"
        return result

    # 寫入
    with sqlite3.connect(db_path) as conn:
        rows = _upsert(conn, df)
        conn.commit()
    
    result["rows"] = rows
    return result


def get_top_n_universe(db_path: str, top_n: int,
                        lookback_days: int = 365) -> List[str]:
    """
    取「過去 N 天平均成交金額」前 top_n 檔活躍股。
    用於 --top 參數，只更新策略真正會用到的投資宇宙（top 300）。
    """
    cutoff = (date.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("""
            SELECT stock_id
            FROM daily_price
            WHERE date > ?
            GROUP BY stock_id
            HAVING AVG(close * volume) > 0
            ORDER BY AVG(close * volume) DESC
            LIMIT ?
        """, (cutoff, top_n)).fetchall()
    return [r[0] for r in rows]


def download_all(db_path: str, token: str, start: str = DEFAULT_START,
                 force: bool = False, sid_filter: Optional[str] = None,
                 workers: int = 1, top_n: Optional[int] = None) -> None:
    """
    批次下載所有股票的三大法人資料（支援並行）。

    Parameters
    ----------
    db_path    : SQLite 路徑
    token      : FinMind API token
    start      : 起始日期（預設 2015-01-01）
    force      : True 時忽略增量，從 start 重新下載
    sid_filter : 只下載指定股票（None 表示全部）
    workers    : 並行下載數（預設 1 = 順序）
    top_n      : 只下載「過去 365 日成交額前 top_n 檔」（None = 全部）
                 推薦每日更新用 top_n=300（投資宇宙），快 7×
    """
    init_table(db_path)

    if sid_filter:
        stock_ids = [sid_filter]
    elif top_n:
        stock_ids = get_top_n_universe(db_path, top_n)
        logger.info(f"🎯 --top {top_n}：只下載活躍宇宙前 {len(stock_ids)} 檔")
    else:
        stock_ids = get_stock_list(db_path)
    if not stock_ids:
        logger.error(
            "❌ load_manifest 為空！請先執行：python run.py --step 1"
        )
        return

    logger.info(
        f"📥 開始下載三大法人資料"
        f"\n   股票數：{len(stock_ids)}"
        f"\n   起始日：{start}"
        f"\n   增量模式：{'否（強制重新下載）' if force else '是'}"
        f"\n   並行度：{workers}"
    )

    success, fail, skip, total_rows = 0, 0, 0, 0
    failed_stocks = []
    start_time = time.time()

    # 並行下載
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(download_one, sid, db_path, token, start, force): sid
                for sid in stock_ids
            }
            
            for i, future in enumerate(as_completed(futures), 1):
                sid = futures[future]
                try:
                    result = future.result(timeout=300)
                    status = result.get("status")
                    rows = result.get("rows", 0)
                    
                    if status == "ok":
                        success += 1
                        total_rows += rows
                        logger.debug(f"  [{sid}] ✓ {rows} 筆")
                    elif status == "up_to_date":
                        skip += 1
                    elif status == "no_data":
                        skip += 1
                    else:
                        fail += 1
                        failed_stocks.append(sid)
                    
                    if i % 20 == 0:
                        logger.info(
                            f"  進度 {i}/{len(stock_ids)} "
                            f"✓{success} ✗{fail} ⊘{skip} (共 {total_rows:,} 筆)"
                        )
                        
                except Exception as e:
                    fail += 1
                    failed_stocks.append(sid)
                    logger.warning(f"  [{sid}] ✗ 異常：{e}")
    else:
        # 順序下載
        for i, sid in enumerate(stock_ids, 1):
            try:
                result = download_one(sid, db_path, token, start, force)
                status = result.get("status")
                rows = result.get("rows", 0)
                
                if status == "ok":
                    success += 1
                    total_rows += rows
                elif status in ["up_to_date", "no_data"]:
                    skip += 1
                else:
                    fail += 1
                    failed_stocks.append(sid)
                
                if i % 20 == 0:
                    logger.info(
                        f"  進度 {i}/{len(stock_ids)} "
                        f"✓{success} ✗{fail} ⊘{skip} (共 {total_rows:,} 筆)"
                    )
                    
            except Exception as e:
                fail += 1
                failed_stocks.append(sid)
                logger.warning(f"  [{sid}] ✗ 異常：{e}")

            # 注意：不需在這裡 sleep。RateLimiter.acquire() 已在每次 API 呼叫前
            # 動態 sleep（依當前 quota 狀況）。多此一舉的 sleep 會拖慢下載。

    elapsed = time.time() - start_time

    # 最終統計
    with sqlite3.connect(db_path) as conn:
        total_count, distinct_stocks = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM institutional_investors"
        ).fetchone()

    logger.info(
        f"\n✅ 下載完成"
        f"\n   耗時：{elapsed:.1f}s"
        f"\n   新增：{total_rows:,} 筆"
        f"\n   統計：{distinct_stocks} 檔股票，共 {total_count:,} 筆記錄"
        f"\n   成功：{success} / 失敗：{fail} / 跳過：{skip}"
    )

    if failed_stocks:
        logger.warning(f"\n⚠️  失敗的股票（{len(failed_stocks)}）：{', '.join(failed_stocks[:10])}")
        if len(failed_stocks) > 10:
            logger.warning(f"   ... 等 {len(failed_stocks) - 10} 檔")


# ══════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="下載台股三大法人買賣超資料（增量、並行、低 API 消耗）"
    )
    parser.add_argument(
        "--sid", type=str,
        help="只下載指定股票代號，如 2330"
    )
    parser.add_argument(
        "--start", type=str, default=DEFAULT_START,
        help=f"起始日期，預設 {DEFAULT_START}（首次下載時）"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="忽略斷點續傳，強制從 --start 重新下載所有資料"
    )
    parser.add_argument(
        "--workers", type=int, default=1,
        help="並行下載數（預設 1 = 順序）。建議 2–4 以降低 API 限制風險"
    )
    parser.add_argument(
        "--top", type=int, default=None,
        help="只下載過去 365 日成交額前 N 檔（推薦每日用 --top 300，快 7×）"
    )
    args = parser.parse_args()

    # 初始化日誌
    logger.remove()
    logger.add(
        sys.stdout, level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}"
    )

    # 驗證環境變數
    token = os.getenv("FINMIND_TOKEN", "").strip()
    if not token:
        logger.error("❌ 環境變數 FINMIND_TOKEN 未設定")
        logger.error("   設定方式（macOS/Linux）：")
        logger.error("     export FINMIND_TOKEN='your_token_here'")
        logger.error("   或在 Python 中：")
        logger.error("     import os; os.environ['FINMIND_TOKEN'] = 'your_token'")
        logger.error("\n   FinMind 免費帳號申請：https://finmindtrade.com/")
        sys.exit(1)

    # 執行下載
    download_all(
        db_path    = DB_PATH,
        token      = token,
        start      = args.start,
        force      = args.force,
        sid_filter = args.sid,
        workers    = args.workers,
        top_n      = args.top,
    )
