"""
daily_update.py
─────────────────────────────────────────────────────────────
每日自動化更新腳本：

1. 從 FinMind 抓取今日最新資料（增量，只抓尚未存入的日期）
2. 執行因子計算，產生今日持倉訊號
3. 把訊號存成 signals/YYYY-MM-DD.json
4. 發送 LINE + Notion 通知

由 GitHub Actions 在每個交易日 14:30 自動觸發，
也可以在本機手動執行：
    python daily_update.py
"""

import os
import json
import time
import sqlite3
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset
from loguru import logger

# ── 設定 Python 路徑 ──────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent / ".."))  # 加入 Quant_Trading_System 根目錄
sys.path.insert(0, str(Path(__file__).parent.parent / "data_pipeline"))

# ── 引入同資料夾和 data_pipeline 的模組 ──────────────────────
from data_pipeline.loader import CSVLoader
from data_pipeline.download_institutional import download_all as download_institutional_all

# ── 設定 ──────────────────────────────────────────────────────
# 使用絕對路徑以避免相對路徑問題（無論從哪個目錄執行都能找到）
BASE_DIR       = Path(__file__).parent.parent  # 項目根目錄

# 自動讀 .env（讓 token 不用手動 export）
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=BASE_DIR / ".env")
except ImportError:
    pass

DB_PATH        = str(BASE_DIR / "data" / "taiwan_stock.db")
SIGNALS_DIR    = BASE_DIR / "signals"
FINMIND_URL    = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN  = os.getenv("FINMIND_TOKEN", "")

# 策略參數（與 quant_layer2.py 保持一致）
LOOKBACK   = 120
TOP_N      = 10
REBAL_FREQ = 20
BIAS_CAP   = 0.10
RF_RATE    = 0.015

SIGNALS_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════════
# PART 1  增量資料更新
# ══════════════════════════════════════════════════════════════

def _finmind_get(dataset: str, stock_id: str,
                 start_date: str, end_date: str,
                 retries: int = 3) -> pd.DataFrame:
    """FinMind API 單次呼叫（含重試）"""
    params = {
        "dataset":    dataset,
        "data_id":    stock_id,
        "start_date": start_date,
        "end_date":   end_date,
        "token":      FINMIND_TOKEN,
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(FINMIND_URL, params=params, timeout=30)
            data = resp.json()
            if data.get("status") == 200 and data.get("data"):
                return pd.DataFrame(data["data"])
            return pd.DataFrame()
        except requests.exceptions.Timeout:
            logger.warning(f"  Timeout（第 {attempt}/{retries} 次），重試...")
            time.sleep(5 * attempt)
        except Exception as e:
            logger.error(f"  API 錯誤：{e}")
            return pd.DataFrame()
    return pd.DataFrame()


def get_last_price_date(conn: sqlite3.Connection) -> str:
    """查詢 DB 裡最新的日期，決定增量起始點"""
    row = conn.execute(
        "SELECT MAX(date) FROM daily_price"
    ).fetchone()
    if row and row[0]:
        # 從最新日期的下一天開始抓
        last = datetime.strptime(row[0], "%Y-%m-%d")
        return (last + timedelta(days=1)).strftime("%Y-%m-%d")
    return "2010-01-01"


def incremental_update():
    """
    增量更新：只抓資料庫還沒有的新資料。

    流程：
    1. 查詢 DB 最新日期
    2. 從 FinMind 抓 [最新日期+1, 今天] 的資料
    3. 清洗後寫入 DB

    注意：只更新 daily_price 和 daily_valuation，
    月營收（monthly_revenue）會在月初有新資料時才更新。
    """
    today     = date.today().strftime("%Y-%m-%d")
    conn_path = DB_PATH

    if not Path(conn_path).exists():
        logger.error(f"資料庫不存在：{conn_path}")
        logger.error("請先在本機跑完 python run.py --step 1，再 push DB 或設定 cache")
        return False

    conn = sqlite3.connect(conn_path)
    start_date = get_last_price_date(conn)

    if start_date > today:
        logger.info(f"資料已是最新（{today}），無需更新")
        conn.close()
        return True

    logger.info(f"增量更新範圍：{start_date} ~ {today}")

    # 取得所有股票代號
    stock_ids = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT stock_id FROM daily_price"
        ).fetchall()
    ]
    conn.close()

    if not stock_ids:
        logger.error("DB 裡沒有股票，請先跑完 Step 1")
        return False

    logger.info(f"更新 {len(stock_ids)} 檔股票...")

    # 使用 CSVLoader 的 DB 連線方式寫入
    loader = CSVLoader(DB_PATH)

    success = 0
    fail    = 0

    for i, sid in enumerate(stock_ids):
        try:
            # 日頻價格
            df_price = _finmind_get("TaiwanStockPrice", sid, start_date, today)
            if not df_price.empty:
                df_price["date"] = pd.to_datetime(df_price["date"])
                df_price = df_price.rename(columns={
                    "max": "high", "min": "low",
                    "Trading_Volume": "volume",
                })
                # 過濾掉 close=0 的停牌日
                df_price = df_price[df_price.get("close", pd.Series([1])) > 0]
                if not df_price.empty:
                    with loader._conn() as c:
                        df_out = df_price[["date","open","high","low","close","volume"]].copy()
                        df_out["date"]     = df_out["date"].dt.strftime("%Y-%m-%d")
                        df_out["stock_id"] = sid
                        df_out.to_sql("daily_price", c, if_exists="append",
                                      index=False, method="multi")

            # 日頻估值
            df_val = _finmind_get("TaiwanStockPER", sid, start_date, today)
            if not df_val.empty:
                df_val["date"] = pd.to_datetime(df_val["date"])
                df_val["PER"]  = pd.to_numeric(df_val["PER"],  errors="coerce")
                df_val.loc[df_val["PER"] == 0, "PER"] = float("nan")
                with loader._conn() as c:
                    df_out = df_val[["date","dividend_yield","PER","PBR"]].copy()
                    df_out["date"]     = df_out["date"].dt.strftime("%Y-%m-%d")
                    df_out["stock_id"] = sid
                    df_out.to_sql("daily_valuation", c, if_exists="append",
                                  index=False, method="multi")

            success += 1

            # 每 50 檔顯示進度
            if (i + 1) % 50 == 0:
                logger.info(f"  進度 {i+1}/{len(stock_ids)}...")

            time.sleep(0.3)   # 速率控制

        except Exception as e:
            logger.warning(f"  {sid} 更新失敗：{e}")
            fail += 1

    logger.info(f"增量更新完成：成功 {success}，失敗 {fail}")
    return True


def incremental_institutional_update():
    """
    增量下載籌碼資料（三大法人）。

    利用已建立的檢查點機制，只抓 DB 還沒有的新日期資料。
    自動應用 RateLimiter 確保不超過 FinMind 的 600 次/小時配額。
    """
    if not FINMIND_TOKEN:
        logger.warning("未設定 FINMIND_TOKEN，跳過籌碼資料下載")
        return False

    try:
        logger.info("  開始增量下載籌碼資料...")
        download_institutional_all(
            db_path=DB_PATH,
            token=FINMIND_TOKEN,
            force=False,  # 增量模式：只抓新日期
            workers=1,     # 日間更新用 1 worker 即可（夜間可改 4）
        )
        logger.info("  籌碼資料更新完成")
        return True
    except Exception as e:
        logger.warning(f"  籌碼資料下載失敗：{e}（非致命，繼續執行）")
        return True  # 不中止流程


# ══════════════════════════════════════════════════════════════
# PART 2  產生今日訊號
# ══════════════════════════════════════════════════════════════

def load_matrices_for_signal(db_path: str,
                              start: str = "2018-01-01") -> dict:
    """從 DB 讀取計算訊號所需的矩陣"""
    conn = sqlite3.connect(db_path)
    end  = date.today().strftime("%Y-%m-%d")

    price_df = pd.read_sql(
        "SELECT date,stock_id,close FROM daily_price "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    val_df = pd.read_sql(
        "SELECT date,stock_id,PER,PBR FROM daily_valuation "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    rev_df = pd.read_sql(
        "SELECT date,stock_id,revenue FROM monthly_revenue ORDER BY date",
        conn, parse_dates=["date"],
    )
    conn.close()

    def wide(df, col):
        return df.pivot(index="date", columns="stock_id", values=col)

    return {
        "close":   wide(price_df, "close"),
        "PER":     wide(val_df,   "PER"),
        "PBR":     wide(val_df,   "PBR"),
        "revenue": wide(rev_df,   "revenue"),
    }


def generate_signals(data: dict) -> tuple:
    """
    計算今日因子並產生持倉訊號。

    Returns
    -------
    signals : list of dict，每檔股票的動作和理由
    stats   : dict，策略的最新績效指標
    """
    close   = data["close"].replace(0.0, np.nan)
    PER     = data["PER"]
    rev_raw = data["revenue"]

    # ── 因子計算 ──────────────────────────────────────────────
    momentum = close.pct_change(LOOKBACK)

    # Revenue YoY（延遲 40 天）
    monthly_yoy = rev_raw.sort_index().pct_change(12)
    monthly_yoy.index = monthly_yoy.index + DateOffset(days=40)
    rev_yoy = monthly_yoy.reindex(close.index, method="ffill")

    # 乖離率
    bias = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    # 大盤擇時
    proxy = close.median(axis=1).ffill()
    score = pd.Series(0.0, index=proxy.index)
    for w in [10, 30, 60, 120]:
        score += (proxy > proxy.rolling(w).mean()).astype(float)
    market_score = (score / 4.0).iloc[-1]

    # ── 今日橫截面篩選 + 排名 ─────────────────────────────────
    today_mom  = momentum.iloc[-1]
    today_per  = PER.iloc[-1]
    today_yoy  = rev_yoy.iloc[-1]
    today_bias = bias.iloc[-1]

    valid = (
        (today_per > 0)
        & (today_yoy >= 0)
        & (today_bias < BIAS_CAP)
        & (today_mom > 0)
    )

    masked = today_mom.where(valid, np.nan)
    rank   = masked.rank(ascending=False)
    top    = set(rank[rank <= TOP_N].index.tolist())

    # ── 與前一期持倉比較（判斷 BUY / SELL / HOLD）────────────
    prev_signals_file = _get_prev_signals_file()
    prev_holdings = set()
    if prev_signals_file:
        try:
            with open(prev_signals_file) as f:
                prev_data = json.load(f)
            prev_holdings = {
                s["stock_id"] for s in prev_data.get("signals", [])
                if s.get("action") in ("BUY", "HOLD")
            }
        except Exception:
            pass

    weight = 1.0 / TOP_N * market_score   # 大盤擇時調整後的部位

    signals = []
    for sid in sorted(top):
        action = "BUY" if sid not in prev_holdings else "HOLD"
        signals.append({
            "stock_id": sid,
            "action":   action,
            "weight":   round(float(weight), 4),
            "rank":     int(rank[sid]),
            "momentum": round(float(today_mom[sid]), 4),
            "PER":      round(float(today_per[sid]), 2) if not pd.isna(today_per[sid]) else None,
            "rev_yoy":  round(float(today_yoy[sid]), 4) if not pd.isna(today_yoy[sid]) else None,
        })

    # 賣出訊號：上期持有但今期不在 top
    for sid in prev_holdings - top:
        signals.append({"stock_id": sid, "action": "SELL", "weight": 0.0})

    signals.sort(key=lambda x: (x["action"] != "BUY",
                                  x["action"] != "HOLD",
                                  x.get("rank", 99)))

    # ── 簡易績效計算（近 252 天）─────────────────────────────
    # --- attach stock names (if available) ---
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT stock_id, stock_name FROM load_manifest").fetchall()
        conn.close()
        name_map = {r[0]: r[1] for r in rows}
    except Exception:
        name_map = {}

    for s in signals:
        s["stock_name"] = name_map.get(s["stock_id"], "")

    # --- compute stats: prefer portfolio-based stats when we have holdings ---
    holds = [s for s in signals if s.get("action") in ("BUY", "HOLD")]
    if holds:
        hold_ids = [s["stock_id"] for s in holds]
        stats = _compute_portfolio_stats(close, hold_ids)
    else:
        stats = _compute_recent_stats(close)

    stats["market_score"] = f"{market_score:.2f}"
    stats["holdings"] = len(holds)

    logger.info(f"  今日持倉：{len(holds)} 檔，大盤分數：{market_score:.2f}")
    return signals, stats


def _get_prev_signals_file() -> Optional[str]:
    """找到最近一次的訊號檔案"""
    files = sorted(SIGNALS_DIR.glob("*.json"), reverse=True)
    today_file = SIGNALS_DIR / f"{date.today()}.json"
    for f in files:
        if f != today_file:
            return str(f)
    return None


def _compute_recent_stats(close: pd.DataFrame,
                           lookback_days: int = 252) -> dict:
    """計算最近一年的策略績效（用等權持倉簡化計算）"""
    try:
        recent = close.iloc[-lookback_days:]
        eq = recent.mean(axis=1).pct_change().dropna()

        total   = (1 + eq).prod() - 1
        ann_ret = (1 + total) ** (252 / max(len(eq), 1)) - 1
        ann_vol = eq.std() * np.sqrt(252)
        sharpe  = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0
        mdd     = ((1 + eq).cumprod() / (1 + eq).cumprod().cummax() - 1).min()

        return {
            "annual_return": f"{ann_ret*100:.1f}%",
            "max_drawdown":  f"{mdd*100:.1f}%",
            "sharpe":        f"{sharpe:.2f}",
        }
    except Exception:
        return {"annual_return": "N/A", "max_drawdown": "N/A", "sharpe": "N/A"}


def _compute_portfolio_stats(close: pd.DataFrame,
                             holdings: list,
                             lookback_days: int = 252) -> dict:
    """
    Compute simple equal-weighted portfolio stats for the given holdings
    over the last `lookback_days` trading days.
    """
    try:
        cols = [h for h in holdings if h in close.columns]
        if not cols:
            return {"annual_return": "N/A", "max_drawdown": "N/A", "sharpe": "N/A"}

        recent = close[cols].iloc[-lookback_days:]
        recent = recent.dropna(axis=1, how="all")
        eq = recent.mean(axis=1).pct_change().dropna()
        if eq.empty:
            return {"annual_return": "N/A", "max_drawdown": "N/A", "sharpe": "N/A"}

        total = (1 + eq).prod() - 1
        ann_ret = (1 + total) ** (252 / max(len(eq), 1)) - 1
        ann_vol = eq.std() * np.sqrt(252)
        sharpe = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0
        mdd = ((1 + eq).cumprod() / (1 + eq).cumprod().cummax() - 1).min()

        return {
            "annual_return": f"{ann_ret*100:.1f}%",
            "max_drawdown":  f"{mdd*100:.1f}%",
            "sharpe":        f"{sharpe:.2f}",
        }
    except Exception:
        return {"annual_return": "N/A", "max_drawdown": "N/A", "sharpe": "N/A"}


# ══════════════════════════════════════════════════════════════
# PART 3  儲存訊號
# ══════════════════════════════════════════════════════════════

def save_signals(signals: list, stats: dict, today: str):
    """把今日訊號存成 signals/YYYY-MM-DD.json"""
    output = {
        "date":        today,
        "generated":   datetime.now().isoformat(),
        "signals":     signals,
        "stats":       stats,
    }
    path = SIGNALS_DIR / f"{today}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    logger.info(f"  💾 訊號已儲存：{path}")


# ══════════════════════════════════════════════════════════════
# PART 4  主程式
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    today = str(date.today())
    logger.info(f"{'='*50}")
    logger.info(f"  台股量化系統 每日更新：{today}")
    logger.info(f"{'='*50}")

    # Step 1：增量更新資料
    logger.info("\n📥 Step 1a：增量更新價格 & 估值資料...")
    ok = incremental_update()
    if not ok:
        logger.error("資料更新失敗，終止流程")
        exit(1)

    # Step 1b：增量下載籌碼資料
    logger.info("\n📊 Step 1b：增量下載籌碼資料（三大法人）...")
    incremental_institutional_update()  # 失敗不中止

    # ⚠️ Step 2-4 已停用：daily_update.py 用的是舊 Layer 2 訊號邏輯（含 bug，
    # 會產生空訊號 / -73% MDD 這類錯誤結果，會覆蓋 predict_model.py 的正確訊號）。
    # 訊號生成請改用 predict_model.py（N1 v2 ML 最終策略）。
    logger.info("\n✅ 資料更新完成。產生最新訊號請執行：python predict_model.py")
    logger.info(f"\n✅ 每日更新完成：{today}")