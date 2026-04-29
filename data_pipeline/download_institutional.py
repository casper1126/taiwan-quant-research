"""
download_institutional.py
─────────────────────────────────────────────────────────────
從 FinMind 下載三大法人買賣超資料，寫入 SQLite。

資料集：TaiwanStockInstitutionalInvestorsBuySell
欄位：
  date          TEXT  交易日
  stock_id      TEXT  股票代號
  investor_type TEXT  法人類型（Foreign_Investor / Investment_Trust / Dealer_Self 等）
  buy           REAL  買進金額（元）
  sell          REAL  賣出金額（元）
  net           REAL  淨買超（buy - sell）

使用方式：
  python download_institutional.py               ← 下載全部
  python download_institutional.py --sid 2330    ← 只下載特定股票
  python download_institutional.py --start 2024-01-01  ← 指定起始日

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
from typing import List, Optional

import pandas as pd
from loguru import logger

# ── 路徑設定 ──────────────────────────────────────────────────
DB_PATH       = "data/taiwan_stock.db"
FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
DATASET       = "TaiwanStockInstitutionalInvestorsBuySell"
RATE_LIMIT_S  = 0.5     # FinMind 免費版速率控制
DEFAULT_START = "2015-01-01"


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
    """FinMind 單次 API 呼叫，含重試。"""
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
            data = resp.json()
            if data.get("status") == 200 and data.get("data"):
                df = pd.DataFrame(data["data"])
                return df
            if data.get("status") == 402:
                logger.warning("  FinMind API 超過速率限制，等待 60 秒...")
                time.sleep(60)
                continue
            return pd.DataFrame()
        except requests.exceptions.Timeout:
            logger.warning(f"  Timeout（{attempt}/{retries}），重試...")
            time.sleep(5 * attempt)
        except Exception as e:
            logger.error(f"  API 錯誤：{e}")
            return pd.DataFrame()
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
    """批次寫入 institutional_investors，回傳寫入筆數。"""
    if df.empty:
        return 0

    # 欄位對齊
    df = df.rename(columns={
        "stock_id":    "stock_id",
        "date":        "date",
        "name":        "investor_type",   # FinMind 欄位名稱
        "buy":         "buy",
        "sell":        "sell",
        "net":         "net",
    })

    # investor_type 欄位可能是 "name" 或 "investor_type"
    if "investor_type" not in df.columns and "name" in df.columns:
        df = df.rename(columns={"name": "investor_type"})

    needed = ["date", "stock_id", "investor_type", "buy", "sell", "net"]
    available = [c for c in needed if c in df.columns]

    # net 可能需要自行計算
    if "net" not in df.columns and "buy" in df.columns and "sell" in df.columns:
        df["net"] = pd.to_numeric(df["buy"], errors="coerce") - \
                    pd.to_numeric(df["sell"], errors="coerce")
        available.append("net")

    rows = df[available].values.tolist()
    ph   = ",".join("?" * len(available))
    conn.executemany(
        f"INSERT OR REPLACE INTO institutional_investors "
        f"({','.join(available)}) VALUES ({ph})",
        rows,
    )
    return len(rows)


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
                 start: str = DEFAULT_START, force: bool = False) -> bool:
    """
    下載單一股票的法人資料。

    斷點續傳：查詢 DB 最新日期，從隔天開始抓，不重複下載。
    force=True 時強制從 start 重新下載。
    """
    today = date.today().strftime("%Y-%m-%d")

    with sqlite3.connect(db_path) as conn:
        start_date = start if force else (_get_last_date(conn, stock_id) or start)

    if start_date > today:
        return True   # 資料已是最新

    df = _fetch(stock_id, start_date, today, token)
    if df.empty:
        return True   # 無新資料（非錯誤）

    with sqlite3.connect(db_path) as conn:
        n = _upsert(conn, df)

    logger.debug(f"  [{stock_id}] 寫入 {n} 筆 ({start_date} ~ {today})")
    return True


def download_all(db_path: str, token: str, start: str = DEFAULT_START,
                 force: bool = False, sid_filter: Optional[str] = None) -> None:
    """
    批次下載所有股票的三大法人資料。

    Parameters
    ----------
    db_path    : SQLite 路徑
    token      : FinMind API token
    start      : 起始日期（預設 2015-01-01）
    force      : True 時忽略斷點，從 start 重新下載
    sid_filter : 只下載指定股票（None 表示全部）
    """
    init_table(db_path)

    stock_ids = [sid_filter] if sid_filter else get_stock_list(db_path)
    if not stock_ids:
        logger.error("load_manifest 為空，請先執行 run.py --step 1 載入價格資料")
        return

    logger.info(f"開始下載三大法人資料：{len(stock_ids)} 檔股票，起始 {start}")

    success, fail = 0, 0
    for i, sid in enumerate(stock_ids, 1):
        try:
            download_one(sid, db_path, token, start=start, force=force)
            success += 1
        except Exception as e:
            logger.warning(f"  [{sid}] 失敗：{e}")
            fail += 1

        if i % 50 == 0:
            logger.info(f"  進度 {i}/{len(stock_ids)}（成功 {success} / 失敗 {fail}）")

        time.sleep(RATE_LIMIT_S)

    # 最終統計
    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM institutional_investors"
        ).fetchone()
    logger.info(
        f"下載完成：{total[1]} 檔股票，共 {total[0]:,} 筆"
        f"（成功 {success} / 失敗 {fail}）"
    )


# ══════════════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="下載三大法人買賣超資料")
    parser.add_argument("--sid",   type=str, help="只下載指定股票代號，如 2330")
    parser.add_argument("--start", type=str, default=DEFAULT_START,
                        help=f"起始日期（預設 {DEFAULT_START}）")
    parser.add_argument("--force", action="store_true",
                        help="忽略斷點，從 start 重新下載")
    args = parser.parse_args()

    token = os.getenv("FINMIND_TOKEN", "")
    if not token:
        logger.error("請設定環境變數 FINMIND_TOKEN")
        logger.error("  export FINMIND_TOKEN=your_token")
        sys.exit(1)

    logger.remove()
    logger.add(sys.stdout, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    download_all(
        db_path    = DB_PATH,
        token      = token,
        start      = args.start,
        force      = args.force,
        sid_filter = args.sid,
    )
