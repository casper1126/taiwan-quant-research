"""
daily_update.py
─────────────────────────────────────────────────────────────
每日自動化更新腳本（Task 8）：

1. 增量更新四個資料集：價格/估值、三大法人、融資融券、TAIEX
2. 跑一次機制偵測（regime/regime_engine.py），算出今天的機制狀態、
   健康分數，並偵測是否比昨天降級
3. 跑一次因子衰退監控（regime/decay_monitor.py），附上最新的衰退警示
4. 用 strategy/quant_layer2.py 的正式回測引擎算今天的建議持倉
   （不是自己手刻一份公式——2026-08-23 以前這裡曾經有一份獨立的因子
   計算邏輯，含 bug，已經停用；現在改成呼叫跟 Task 2-7 共用的同一套
   `run_pipeline()`，維持整個專案「因子定義只有一個事實來源」的原則）
5. 把訊號存成 signals/YYYY-MM-DD.json
6. 發送 LINE + Notion 通知（機制降級時額外發警示）；沒有設定 token
   時自動切換成 dry-run（組好內容印到 log，並存檔到 signals/）

由 GitHub Actions 在每個交易日 14:30 自動觸發，也可以在本機手動執行：
    python automation/daily_update.py
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import sqlite3
from loguru import logger

# ── 設定 Python 路徑 ──────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent  # 專案根目錄
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "data_pipeline"))
sys.path.insert(0, str(BASE_DIR / "strategy"))

# 自動讀 .env（讓 token 不用手動 export）
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=BASE_DIR / ".env")
except ImportError:
    pass

from data_pipeline.loader import CSVLoader
from data_pipeline.download_lock import download_lock
from data_pipeline.finmind_common import QuotaExhaustedError, MAX_QUOTA_RETRY_CYCLES, sleep_with_heartbeat
from data_pipeline.download_institutional import (
    download_all as download_institutional_all,
    get_top_n_universe,
)
from data_pipeline.download_supplementary import download_all_supplementary

import quant_layer2 as q
from regime.regime_engine import run_regime_engine, STATE_RANK
from regime.decay_monitor import run_decay_monitor

import notifier

# ── 設定 ──────────────────────────────────────────────────────
DB_PATH       = str(BASE_DIR / "data" / "taiwan_stock.db")
SIGNALS_DIR   = BASE_DIR / "signals"
FINMIND_URL   = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "").strip()

PER_CALL_SLEEP = 0.5      # 秒/次（跟 download_supplementary.py 同一套固定節奏）
HOURLY_LIMIT   = 550
WINDOW_SECONDS = 3600

# 2026-08-23 決定（見 docs/DECISIONS.md「Task 8」那筆）：每日增量更新只
# 更新「過去 365 天平均成交金額」前 N 檔活躍股，不是全部 2056 檔。
#
# 原因：全部 2056 檔股票 × 價格/估值兩個資料集，即使每天只增量抓 1 天，
# 也是 2056×2≈4112 次 API 呼叫，在 550 次/小時的額度下光是這一步就要
# 7+ 小時，完全不適合每天 14:30 觸發、還要接著跑機制偵測+回測+通知的
# 自動化排程。策略本身選股時也只會用流動性篩選（liquid_mask）留下的
# 活躍股，非活躍股從來就不會被選中——限制在前 300 檔不會漏掉任何策略
# 真正可能買的股票，只是讓「每天要不要更新」這件事跟「策略用不用得到」
# 這件事對齊。完整 2056 檔的歷史資料庫（回測研究用）不受影響，只有
# daily_update.py 這支「每日」腳本的更新範圍縮小，Task 1-7 累積的
# 全量歷史資料不會因為每天執行這支腳本而變得只剩前 300 檔。
DAILY_UNIVERSE_TOP_N = 300

SIGNALS_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════
# PART 1a  價格／估值增量更新（含 402 判定 + 休眠自動恢復）
# ══════════════════════════════════════════════════════════════
#
# 2026-08-23 重寫：原本這裡的 `_finmind_get()` 沒有處理 HTTP 402，跟
# Task 1 事故發生前的 download_supplementary.py／download_institutional.py
# 是同一種 bug（配額用盡被靜默吞成「沒資料」）。這裡直接套用已經修好、
# 驗證過的模式（finmind_common.QuotaExhaustedError + 休眠到下個整點
# 自動恢復 + download_lock 互斥鎖），不重新發明一份不同步的邏輯。

class RateLimiter:
    """跟 download_supplementary.py 同一種固定節奏限速器。"""

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
        self.window_start = self._current_window()
        self.call_count = 0

    def acquire(self) -> None:
        now = time.time()
        if now - self.window_start >= self.window_seconds:
            self.reset_window()
        if self.call_count >= self.limit:
            wait = self.next_window_start() - time.time()
            if wait > 0:
                logger.warning(f"⚠️  本時間窗口已呼叫 {self.call_count} 次，主動休眠 {wait:.0f} 秒...")
                time.sleep(wait + 0.5)
            self.reset_window()
        time.sleep(self.per_call_sleep)
        self.call_count += 1


_rate_limiter = RateLimiter()


def _hibernate(processed: int, total: int, cycle: int) -> None:
    wake_at = datetime.fromtimestamp(_rate_limiter.next_window_start()) + timedelta(seconds=5)
    logger.warning(
        f"⏸️  額度耗盡於 {datetime.now():%H:%M:%S}，已處理 {processed}/{total} 檔，"
        f"將於 {wake_at:%H:%M:%S} 自動恢復（第 {cycle}/{MAX_QUOTA_RETRY_CYCLES} 次）"
    )
    sleep_with_heartbeat(wake_at, processed, total)
    _rate_limiter.reset_window()
    logger.info(f"▶️  已恢復，從第 {processed + 1} 檔繼續")


def _finmind_get(dataset: str, stock_id: str, start_date: str, end_date: str,
                 retries: int = 3) -> pd.DataFrame:
    """
    FinMind 單次呼叫。402 判定必須在 `resp.raise_for_status()` 之前，
    否則會被 generic HTTPError 分支接住、重試幾次後放棄，被上層誤判成
    「沒有資料」（Task 1 402 事故的根因，見 docs/PROJECT_STATUS.md §5）。
    """
    params = {
        "dataset": dataset, "data_id": stock_id,
        "start_date": start_date, "end_date": end_date, "token": FINMIND_TOKEN,
    }
    for attempt in range(1, retries + 1):
        _rate_limiter.acquire()
        try:
            resp = requests.get(FINMIND_URL, params=params, timeout=30)
            if resp.status_code == 402:
                raise QuotaExhaustedError(stock_id)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") == 402:
                raise QuotaExhaustedError(stock_id)
            if payload.get("status") == 200:
                return pd.DataFrame(payload.get("data") or [])
            return pd.DataFrame()
        except QuotaExhaustedError:
            raise
        except requests.exceptions.Timeout:
            logger.warning(f"  [{stock_id}] Timeout（attempt {attempt}/{retries}），重試...")
            time.sleep(2 ** attempt)
        except requests.exceptions.HTTPError as e:
            logger.warning(f"  [{stock_id}] HTTP 錯誤（{e.response.status_code}），重試...")
            time.sleep(2 ** attempt)
        except Exception as e:
            logger.error(f"  [{stock_id}] 異常：{type(e).__name__}: {e}")
            return pd.DataFrame()
    logger.warning(f"  [{stock_id}] 最終失敗（{retries} 次重試後）")
    return pd.DataFrame()


def _get_last_date(conn: sqlite3.Connection, table: str, sid: str,
                   default: str = "2010-01-01") -> str:
    """
    每支股票、每張表各自獨立的 checkpoint（不是全域最大日期）。

    2026-08-23 修正：原本用一個「全部股票共用」的全域 start_date（查
    `daily_price` 整張表的 MAX(date)），只要有任何一檔股票已經追上最新
    日期，這個全域值就會前進，導致還沒處理到的股票、或者 daily_price
    寫成功但 daily_valuation 因為配額用盡沒寫成功的那一小段區間，
    在「下一次」執行時被這個已經前進的全域 start_date 蓋過去、永遠
    抓不回來（誤判成「已經是最新」）。改成跟 download_institutional.py／
    download_supplementary.py 一樣的每股票、每表獨立 checkpoint，才不會
    有這個缺口。
    """
    row = conn.execute(f"SELECT MAX(date) FROM {table} WHERE stock_id = ?", (sid,)).fetchone()
    if row and row[0]:
        last = datetime.strptime(row[0], "%Y-%m-%d")
        return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    return default


def _upsert_one_price_valuation(loader: CSVLoader, conn: sqlite3.Connection,
                                sid: str, today: str) -> str:
    """
    抓單一股票的價格＋估值並寫入，回傳 status：
    "ok" / "no_data" / "quota_exhausted"

    價格／估值各自用自己的 checkpoint（見 `_get_last_date`），不是共用
    同一個 start_date——兩張表可能因為之前某次配額用盡而進度不同步，
    各自抓各自缺的區間才不會有一邊永遠補不回來的缺口。

    用 INSERT OR REPLACE（不是 pandas to_sql(if_exists="append")）：
    daily_price／daily_valuation 都用 (date, stock_id) 當 PRIMARY KEY，
    append 模式在同一個區間重複執行（例如 402 休眠恢復後重跑同一個
    stock）會直接撞 UNIQUE constraint 拋例外，把「應該要能安全重試」的
    正常流程誤判成失敗、跳過該股票、留下真正的資料缺口。改用
    INSERT OR REPLACE，跟專案裡其他下載器（download_institutional.py／
    download_supplementary.py）一致，任何時候重跑同一段區間都是安全的。
    """
    price_start = _get_last_date(conn, "daily_price", sid)
    val_start = _get_last_date(conn, "daily_valuation", sid)

    df_price = pd.DataFrame() if price_start > today else \
        _finmind_get("TaiwanStockPrice", sid, price_start, today)
    df_val = pd.DataFrame() if val_start > today else \
        _finmind_get("TaiwanStockPER", sid, val_start, today)

    wrote_any = False

    if not df_price.empty:
        df_price = df_price.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
        df_price["date"] = pd.to_datetime(df_price["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df_price["close"] = pd.to_numeric(df_price.get("close"), errors="coerce")
        df_price = df_price[df_price["close"] > 0]
        df_price = df_price.dropna(subset=["date", "close"])
        if not df_price.empty:
            df_price["open"] = pd.to_numeric(df_price.get("open"), errors="coerce")
            df_price["high"] = pd.to_numeric(df_price.get("high"), errors="coerce")
            df_price["low"] = pd.to_numeric(df_price.get("low"), errors="coerce")
            df_price["volume"] = pd.to_numeric(df_price.get("volume"), errors="coerce").fillna(0)
            df_price["stock_id"] = sid
            rows = df_price[["date", "stock_id", "open", "high", "low", "close", "volume"]].values.tolist()
            with loader._conn() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO daily_price "
                    "(date, stock_id, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            wrote_any = True

    if not df_val.empty:
        df_val["date"] = pd.to_datetime(df_val["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        df_val["PER"] = pd.to_numeric(df_val.get("PER"), errors="coerce")
        df_val.loc[df_val["PER"] == 0, "PER"] = float("nan")
        df_val["PBR"] = pd.to_numeric(df_val.get("PBR"), errors="coerce")
        df_val["dividend_yield"] = pd.to_numeric(df_val.get("dividend_yield"), errors="coerce")
        df_val = df_val.dropna(subset=["date"])
        if not df_val.empty:
            df_val["stock_id"] = sid
            rows = df_val[["date", "stock_id", "dividend_yield", "PER", "PBR"]].values.tolist()
            with loader._conn() as c:
                c.executemany(
                    "INSERT OR REPLACE INTO daily_valuation "
                    "(date, stock_id, dividend_yield, PER, PBR) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
            wrote_any = True

    return "ok" if wrote_any else "no_data"


def incremental_price_valuation(sid_filter: Optional[str] = None,
                                stock_ids: Optional[List[str]] = None) -> bool:
    """
    增量更新 daily_price／daily_valuation。全程持有 download_lock（跟
    institutional／supplementary 共用同一把鎖檔，不同時段各自取得，
    不會巢狀重入——同一時間只有一個階段在打 FinMind）。

    stock_ids 不給時預設更新「全部」DB 裡已知的股票——只有
    run_daily_update() 的正常每日流程會主動傳入 DAILY_UNIVERSE_TOP_N
    活躍股清單，其他呼叫端（測試、未來要跑全量的場景）維持原本「不給
    就全部更新」的行為，不會因為這次改動而意外縮小範圍。
    """
    today = date.today().strftime("%Y-%m-%d")

    with download_lock("daily_update.py:price_valuation"):
        if not Path(DB_PATH).exists():
            logger.error(f"資料庫不存在：{DB_PATH}")
            return False

        conn = sqlite3.connect(DB_PATH)
        if sid_filter:
            resolved_ids = [sid_filter]
        elif stock_ids is not None:
            resolved_ids = stock_ids
        else:
            resolved_ids = [
                r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM daily_price").fetchall()
            ]
        stock_ids = resolved_ids

        if not stock_ids:
            conn.close()
            logger.error("DB 裡沒有股票，請先跑完 python run.py --step 1")
            return False

        logger.info(f"📥 增量更新價格／估值：{len(stock_ids)} 檔，每股票各自 checkpoint 續抓到 {today}")
        loader = CSVLoader(DB_PATH)

        success, fail, skip = 0, 0, 0
        quota_cycle = 0
        i = 0
        last_heartbeat = time.time()

        try:
            while i < len(stock_ids):
                sid = stock_ids[i]
                try:
                    status = _upsert_one_price_valuation(loader, conn, sid, today)
                    if status == "ok":
                        success += 1
                    elif status == "no_data":
                        skip += 1
                except QuotaExhaustedError:
                    quota_cycle += 1
                    if quota_cycle > MAX_QUOTA_RETRY_CYCLES:
                        logger.error(
                            f"❌ 已嘗試 {MAX_QUOTA_RETRY_CYCLES} 個時間窗口仍配額不足，"
                            f"已處理 {i}/{len(stock_ids)} 檔（成功 {success}）。"
                            f"重新執行同一指令可從 checkpoint 續跑。"
                        )
                        return False
                    _hibernate(i, len(stock_ids), quota_cycle)
                    continue
                except Exception as e:
                    fail += 1
                    logger.warning(f"  [{sid}] ✗ 異常：{e}")

                quota_cycle = 0
                i += 1
                if (i) % 100 == 0:
                    logger.info(f"  進度 {i}/{len(stock_ids)} ✓{success} ⊘{skip} ✗{fail}")
                if time.time() - last_heartbeat > 600:
                    logger.info(f"💓 心跳：{i}/{len(stock_ids)} 檔，✓{success} ⊘{skip} ✗{fail}")
                    last_heartbeat = time.time()
        finally:
            conn.close()

        logger.info(f"✅ 價格／估值增量更新完成：成功 {success}／跳過(無資料) {skip}／失敗 {fail}")
        return True


# ══════════════════════════════════════════════════════════════
# PART 1b  三大法人增量更新（既有邏輯，已經有完整的休眠/恢復機制）
# ══════════════════════════════════════════════════════════════

def incremental_institutional_update(top_n: Optional[int] = None,
                                     sid_filter: Optional[str] = None) -> bool:
    if not FINMIND_TOKEN:
        logger.warning("未設定 FINMIND_TOKEN，跳過三大法人資料下載")
        return False
    try:
        logger.info(f"📊 增量下載三大法人資料（sid={sid_filter}, top_n={top_n}）...")
        download_institutional_all(db_path=DB_PATH, token=FINMIND_TOKEN, force=False,
                                   workers=1, top_n=top_n, sid_filter=sid_filter)
        logger.info("✅ 三大法人資料更新完成")
        return True
    except SystemExit:
        # download_lock 偵測到有其他 process 在跑會 sys.exit(1)，daily_update.py
        # 不應該整支腳本跟著被殺掉——記錄下來，當作這個步驟今天沒跑成，繼續後面流程。
        logger.warning("⚠️  三大法人下載未取得鎖（可能有其他下載程序在跑），今天跳過此步驟")
        return False
    except Exception as e:
        logger.warning(f"三大法人資料下載失敗：{e}（非致命，繼續執行）")
        return False


# ══════════════════════════════════════════════════════════════
# PART 1c  融資融券 + TAIEX 增量更新（Task 8 新增，重用 Task 1 的下載器）
# ══════════════════════════════════════════════════════════════

def incremental_margin_and_index(stock_ids: Optional[List[str]] = None) -> bool:
    """
    直接呼叫 download_supplementary.py 既有的增量入口（不帶 --backfill-to
    就是預設的增量模式：從各自 checkpoint 續抓到今天），不重新實作一份
    邏輯——這支下載器已經有完整的鎖檔+402 休眠恢復機制（Task 1 事故修復）。
    """
    if not FINMIND_TOKEN:
        logger.warning("未設定 FINMIND_TOKEN，跳過融資融券／TAIEX 下載")
        return False
    try:
        logger.info(f"📈 增量下載融資融券／TAIEX 資料（{len(stock_ids) if stock_ids else '全部'} 檔）...")
        download_all_supplementary(db_path=DB_PATH, token=FINMIND_TOKEN, force=False,
                                   stock_ids=stock_ids)
        logger.info("✅ 融資融券／TAIEX 更新完成")
        return True
    except SystemExit:
        logger.warning("⚠️  融資融券／TAIEX 下載未取得鎖（可能有其他下載程序在跑），今天跳過此步驟")
        return False
    except Exception as e:
        logger.warning(f"融資融券／TAIEX 下載失敗：{e}（非致命，繼續執行）")
        return False


# ══════════════════════════════════════════════════════════════
# PART 2  機制訊號（Task 3 的 regime_engine，跟 quant_layer2.py 是被動接受
#          關係——regime/ 不 import quant_layer2.py，見 docs/PROJECT_STATUS.md）
# ══════════════════════════════════════════════════════════════

def compute_regime_signal() -> Tuple[Dict, pd.DataFrame]:
    """
    跑一次完整的機制偵測，取最新一天當作「今天的機制」。

    回傳 (regime_info, regime_df)：
      regime_info：{"date","regime","health_score","p_bear","crash_prob",
                    "exposure","downgraded","previous_regime"}
      regime_df  ：完整結果（目前 Task 8 不需要拿它去接 quant_layer2 的
                    機制模式，只是保留完整輸出以防之後要用）
    """
    logger.info("🧭 計算今日機制訊號...")
    regime_df, _ = run_regime_engine(db_path=DB_PATH)
    valid = regime_df.dropna(subset=["regime"])
    if valid.empty:
        logger.warning("機制偵測沒有任何有效輸出（資料不足），regime 訊號留空")
        return {
            "date": None, "regime": None, "health_score": None,
            "p_bear": None, "crash_prob": None, "exposure": None,
            "downgraded": False, "previous_regime": None,
        }, regime_df

    latest = valid.iloc[-1]
    latest_date = valid.index[-1]

    downgraded = False
    previous_regime = None
    if len(valid) >= 2:
        prev = valid.iloc[-2]
        previous_regime = prev["regime"]
        if STATE_RANK.get(latest["regime"], -1) < STATE_RANK.get(prev["regime"], -1):
            downgraded = True

    def _safe_round(x, n=4):
        return None if pd.isna(x) else round(float(x), n)

    info = {
        "date": latest_date.strftime("%Y-%m-%d"),
        "regime": latest["regime"],
        "health_score": _safe_round(latest["health"], 1),
        "p_bear": _safe_round(latest["p_bear"]),
        "crash_prob": _safe_round(latest["crash_prob"]),
        "exposure": _safe_round(latest["exposure"], 2),
        "downgraded": bool(downgraded),
        "previous_regime": previous_regime,
    }
    logger.info(
        f"  機制：{info['regime']}（健康分數 {info['health_score']}，"
        f"曝險建議 {info['exposure']}）{'⚠️ 較昨天降級' if downgraded else ''}"
    )
    return info, regime_df


def compute_factor_decay_alerts(today: str) -> Dict:
    """跑 Task 7b 的因子衰退監控，回傳可以直接塞進 signals JSON 的字典。"""
    logger.info("📉 計算因子衰退警示...")
    try:
        result = run_decay_monitor(db_path=DB_PATH, end=today)
        return result["alerts"]
    except Exception as e:
        logger.warning(f"因子衰退監控失敗：{e}（非致命，signals 裡這欄留空）")
        return {}


# ══════════════════════════════════════════════════════════════
# PART 3  個股訊號（改呼叫 quant_layer2.py 的正式引擎，不再手刻公式）
# ══════════════════════════════════════════════════════════════

def _get_prev_signals_file(today: str) -> Optional[Path]:
    files = sorted(SIGNALS_DIR.glob("????-??-??.json"), reverse=True)
    today_file = SIGNALS_DIR / f"{today}.json"
    for f in files:
        if f != today_file:
            return f
    return None


def _load_prev_holdings(today: str) -> set:
    prev_file = _get_prev_signals_file(today)
    if not prev_file:
        return set()
    try:
        with open(prev_file, encoding="utf-8") as f:
            prev = json.load(f)
        return {s["stock_id"] for s in prev.get("signals", [])
                if s.get("action") in ("BUY", "HOLD")}
    except Exception:
        return set()


def generate_stock_signals(today: str) -> Tuple[List[Dict], Dict]:
    """
    用 strategy/quant_layer2.py 的正式回測引擎產生今日訊號：
      1. 正常跑一次（data_end=today）拿到真實的、可信的 stats/equity——
         用真正的完整回測結果，不是舊版那種「最近 252 天等權近似」的
         粗略估計，符合本專案「數字只能來自真實計算」的原則。
      2. 用 for_live_signal=True 再跑一次拿到「用到今天為止的全部資料
         建議、明天該持有什麼」的即時訊號（第一次那個 stats 對應的
         positions 是回測安全版本，today 那列其實是「已經執行完」的
         部位，不能拿來當作明天的操作建議，見 build_positions() 的
         for_live_signal 說明）。
    """
    logger.info("🧠 用 quant_layer2.py 產生今日訊號...")

    stats, _, _ = q.run_pipeline(data_end=today, save_equity_path=None)
    _, _, live_positions = q.run_pipeline(data_end=today, for_live_signal=True, save_equity_path=None)

    today_row = live_positions.iloc[-1]
    signal_date = live_positions.index[-1]
    holdings = today_row[today_row > 0].sort_values(ascending=False)

    prev_holdings = _load_prev_holdings(today)
    curr_ids = set(holdings.index)

    # 股票名稱存在 data/stock_names.json（data_pipeline/fetch_stock_names.py
    # 產生的），不是 SQLite 表——舊版這裡查詢一個 load_manifest.stock_name
    # 欄位，這個欄位其實從來就不存在，靜默失敗成空字典，讓通知裡的股票
    # 名稱一直是空的。順手修正，讀真正存名稱的地方。
    try:
        names_path = BASE_DIR / "data" / "stock_names.json"
        with open(names_path, encoding="utf-8") as f:
            name_map = json.load(f)
    except Exception:
        name_map = {}

    signals = []
    for sid, w in holdings.items():
        action = "BUY" if sid not in prev_holdings else "HOLD"
        signals.append({
            "stock_id": sid,
            "stock_name": name_map.get(sid, ""),
            "action": action,
            "weight": round(float(w), 4),
        })
    for sid in prev_holdings - curr_ids:
        signals.append({
            "stock_id": sid, "stock_name": name_map.get(sid, ""),
            "action": "SELL", "weight": 0.0,
        })

    signals.sort(key=lambda x: (x["action"] != "BUY", x["action"] != "HOLD"))

    logger.info(
        f"  訊號基準日：{signal_date.date()}（用 {today} 為止的資料算出，"
        f"建議下個交易日執行）持倉 {len(holdings)} 檔"
    )

    if stats is None:
        stats = {"annual_return": "N/A", "max_drawdown": "N/A", "sharpe": "N/A"}
    stats = dict(stats)
    stats["signal_basis_date"] = signal_date.strftime("%Y-%m-%d")
    stats["holdings"] = len(holdings)

    return signals, stats


# ══════════════════════════════════════════════════════════════
# PART 4  儲存訊號
# ══════════════════════════════════════════════════════════════

def save_signals(signals: list, stats: dict, regime_info: dict,
                 factor_decay_alerts: dict, today: str) -> Path:
    output = {
        "date": today,
        "generated": datetime.now().isoformat(),
        "signals": signals,
        "stats": stats,
        "regime": regime_info,
        "factor_decay_alerts": factor_decay_alerts,
    }
    path = SIGNALS_DIR / f"{today}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"💾 訊號已儲存：{path}")
    return path


# ══════════════════════════════════════════════════════════════
# PART 5  主程式
# ══════════════════════════════════════════════════════════════

def run_daily_update(sid_filter: Optional[str] = None,
                     universe_top_n: Optional[int] = DAILY_UNIVERSE_TOP_N) -> bool:
    """
    universe_top_n：每日只更新這麼多檔活躍股（見 DAILY_UNIVERSE_TOP_N 的
    說明）。傳 None 表示不設限、更新 DB 裡全部已知股票——正常每日排程
    不會這樣用（太慢），只有真的需要一次全量刷新時才手動傳 None。
    """
    today = str(date.today())
    logger.info(f"{'='*50}")
    logger.info(f"  台股量化系統 每日更新：{today}")
    logger.info(f"{'='*50}")

    universe: Optional[List[str]] = None
    if sid_filter:
        universe = [sid_filter]
    elif universe_top_n is not None:
        universe = get_top_n_universe(DB_PATH, universe_top_n)
        logger.info(f"🎯 每日活躍宇宙：前 {universe_top_n} 檔（實際取得 {len(universe)} 檔）")

    logger.info("\n📥 Step 1a：增量更新價格／估值...")
    ok = incremental_price_valuation(sid_filter=sid_filter, stock_ids=universe)
    if not ok:
        logger.error("價格／估值更新失敗，終止流程")
        return False

    logger.info("\n📊 Step 1b：增量更新三大法人資料...")
    incremental_institutional_update(
        top_n=None if sid_filter else universe_top_n, sid_filter=sid_filter,
    )  # 非致命，失敗不中止

    logger.info("\n📈 Step 1c：增量更新融資融券／TAIEX 資料...")
    incremental_margin_and_index(stock_ids=universe)  # 非致命，失敗不中止

    logger.info("\n🧭 Step 2：計算機制訊號...")
    regime_info, _ = compute_regime_signal()

    logger.info("\n📉 Step 3：計算因子衰退警示...")
    factor_decay_alerts = compute_factor_decay_alerts(today)

    logger.info("\n🧠 Step 4：產生今日個股訊號...")
    signals, stats = generate_stock_signals(today)

    logger.info("\n💾 Step 5：儲存訊號...")
    save_signals(signals, stats, regime_info, factor_decay_alerts, today)

    logger.info("\n📡 Step 6：發送通知...")
    notifier.notify_all(signals, stats, regime_info, factor_decay_alerts, today=today)

    logger.info(f"\n✅ 每日更新完成：{today}")
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="每日自動化更新（Task 8）")
    parser.add_argument("--sid", type=str, default=None,
                        help="測試用：只更新指定一檔股票的價格/估值/法人/融資（TAIEX 不分股票，仍會更新）")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stdout, level="INFO",
              format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

    ok = run_daily_update(sid_filter=args.sid)
    sys.exit(0 if ok else 1)
