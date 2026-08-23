"""
regime/decay_monitor.py
─────────────────────────────────────────────────────────────
Task 7b：因子衰退監控（Factor Decay Monitor）

因子的預測力不是永遠不變的——市場會學習、套利會讓已知的異常消失、
總經環境轉變也會讓某些因子階段性失效。這個模組追蹤每個因子的
252 日滾動 IC，跟它自己的長期均值比較，太弱就發出警示，讓
Task 8 的每日自動化可以在因子明顯失效時提醒使用者，而不是悶著頭
繼續用一個已經不管用的因子。

跟 quant_layer2.py 的依賴方向：維持 Task 3 一開始定案的規則——
regime/ 只被 quant_layer2.py 呼叫，不 import quant_layer2.py 本身。
這裡重用 strategy/factors/ 的因子數學定義（那是無狀態的純函式模組，
不是策略引擎本身，跟 regime/data_loader.py 一樣獨立從 SQLite 讀資料，
不透過 quant_layer2.load_matrices()）。rev_yoy 的 40 天延遲公告邏輯
跟 IC 計算是簡單、穩定的公式，這裡各自輕量重新實作一份並註明跟
quant_layer2.py 對應函式的關係，不是憑空發明新定義。

追蹤的因子：momentum／value／rev_yoy／low_vol／margin_usage／
inst_flow——涵蓋主策略複合裡的四個核心因子，加上 Task 2/5 排查後
移出複合、但仍持續計算的 inst_flow／margin_usage（衰退監控對它們
一樣有意義：如果之後哪天 IC 突然轉強，這裡也追蹤得到，不會因為
不在複合裡就沒人看）。

直接執行：
  python regime/decay_monitor.py
"""

import sqlite3
import sys
import warnings
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd
from loguru import logger
from pandas.tseries.offsets import DateOffset

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from factors import base as factor_base
from factors import style as factor_style
from factors import taiwan as factor_taiwan

DB_PATH = "data/taiwan_stock.db"
FWD_DAYS = 20
IC_ROLLING_WINDOW = 252
IC_ROLLING_MIN_PERIODS = 60
DECAY_THRESHOLD_RATIO = 0.5  # 近期 IC < 長期均值 50% → 判定衰退

FACTOR_NAMES = ["momentum", "value", "rev_yoy", "low_vol", "margin_usage", "inst_flow"]


# ══════════════════════════════════════════════════════════════
# 1. 資料載入（獨立於 quant_layer2.py，維持 regime/ 單向依賴）
# ══════════════════════════════════════════════════════════════

def load_factor_inputs(db_path: str = DB_PATH, start: str = "2015-01-01",
                       end: str = "2026-04-17") -> Dict[str, pd.DataFrame]:
    """跟 quant_layer2.load_matrices() 讀的是同幾張表，但這裡獨立重新查詢
    （不 import quant_layer2.py），維持 regime/ 的單向依賴規則。"""
    conn = sqlite3.connect(db_path)

    price_df = pd.read_sql(
        "SELECT date, stock_id, close, volume FROM daily_price WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    val_df = pd.read_sql(
        "SELECT date, stock_id, PER, PBR FROM daily_valuation WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    rev_df = pd.read_sql(
        "SELECT date, stock_id, revenue FROM monthly_revenue ORDER BY date",
        conn, parse_dates=["date"],
    )
    try:
        inst_df = pd.read_sql(
            """SELECT date, stock_id,
                      SUM(CASE WHEN investor_type IN
                          ('Foreign_Investor','Foreign_Dealer_Self','Investment_Trust')
                          THEN net ELSE 0 END) AS inst_net
               FROM institutional_investors WHERE date BETWEEN ? AND ?
               GROUP BY date, stock_id ORDER BY date""",
            conn, params=(start, end), parse_dates=["date"],
        )
        inst_matrix = inst_df.pivot(index="date", columns="stock_id", values="inst_net") if not inst_df.empty else pd.DataFrame()
    except Exception:
        inst_matrix = pd.DataFrame()
    try:
        margin_df = pd.read_sql(
            "SELECT date, stock_id, margin_balance FROM margin_trading WHERE date BETWEEN ? AND ? ORDER BY date",
            conn, params=(start, end), parse_dates=["date"],
        )
        margin_matrix = margin_df.pivot(index="date", columns="stock_id", values="margin_balance") if not margin_df.empty else pd.DataFrame()
    except Exception:
        margin_matrix = pd.DataFrame()
    conn.close()

    def wide(df, col):
        return df.pivot(index="date", columns="stock_id", values=col)

    return {
        "close": wide(price_df, "close").replace(0.0, np.nan),
        "volume": wide(price_df, "volume").replace(0.0, np.nan),
        "PER": wide(val_df, "PER"),
        "PBR": wide(val_df, "PBR"),
        "revenue": wide(rev_df, "revenue"),
        "institutional": inst_matrix,
        "margin_balance": margin_matrix,
    }


def _build_rev_yoy(rev_matrix: pd.DataFrame, price_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """跟 quant_layer2.build_rev_yoy() 同一個公式（40 天公告延遲防 look-ahead），
    這裡獨立重新實作一份，維持 regime/ 不 import quant_layer2.py。"""
    monthly_yoy = rev_matrix.sort_index().pct_change(periods=12)
    monthly_yoy.index = monthly_yoy.index + DateOffset(days=40)
    monthly_yoy = monthly_yoy.sort_index()
    return monthly_yoy.reindex(price_dates, method="ffill")


def build_monitored_factors(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """建構 FACTOR_NAMES 六個因子的寬格式矩陣，不套用流動性遮罩——
    衰退監控關心的是因子本身在全樣本的預測力變化，不是選股宇宙。"""
    return {
        "momentum": factor_style.momentum_52w(data),
        "value": factor_style.value_composite(data),
        "low_vol": factor_style.low_vol_ivol(data),
        "rev_yoy": _build_rev_yoy(data["revenue"], data["close"].index),
        "margin_usage": factor_taiwan.margin_usage(data),
        "inst_flow": factor_taiwan.inst_flow(data),
    }


# ══════════════════════════════════════════════════════════════
# 2. IC 與滾動 IC
# ══════════════════════════════════════════════════════════════

def compute_ic(factor: pd.DataFrame, fwd_return: pd.DataFrame, min_stocks: int = 10) -> pd.Series:
    """逐日橫截面 Spearman IC（因子排名 vs 未來報酬排名）。"""
    records = []
    for dt in factor.index:
        if dt not in fwd_return.index:
            continue
        f = factor.loc[dt].dropna()
        r = fwd_return.loc[dt].dropna()
        common = f.index.intersection(r.index)
        if len(common) < min_stocks:
            continue
        ic = f[common].rank().corr(r[common].rank(), method="spearman")
        records.append({"date": dt, "IC": ic})
    if not records:
        return pd.Series(dtype=float)
    return pd.DataFrame(records).set_index("date")["IC"]


def compute_rolling_ic(factor: pd.DataFrame, fwd_return: pd.DataFrame,
                       window: int = IC_ROLLING_WINDOW,
                       min_periods: int = IC_ROLLING_MIN_PERIODS) -> pd.Series:
    """逐日 IC 的 |IC| 252 日滾動均值——跟 quant_layer2.py 的 ic_rolling 是同一種
    量測方式（用絕對值，因子失效的定義是「訊號變弱」，不只是「訊號翻負」）。"""
    ic_series = compute_ic(factor, fwd_return)
    if ic_series.empty:
        return ic_series
    full_idx = factor.index
    ic_full = ic_series.reindex(full_idx)
    return ic_full.abs().rolling(window, min_periods=min_periods).mean()


def compute_decay_table(rolling_ic: pd.Series, threshold_ratio: float = DECAY_THRESHOLD_RATIO) -> pd.DataFrame:
    """
    long_term_mean(t) = rolling_ic 自己「到 t 為止」的展開式（expanding）均值——
    刻意用 expanding 而不是全樣本均值，因為全樣本均值會用到 t 之後的資料，
    對「t 那天算出來的警示合不合理」是一種 look-ahead。

    decayed(t) = recent_ic(t) < threshold_ratio * long_term_mean(t)
    （long_term_mean 非正時不判斷，衰退的定義建立在「本來有正訊號後來變弱」，
    long_term_mean ≤ 0 代表這個因子從頭到尾就沒有穩定訊號，不是「衰退」）
    """
    long_term_mean = rolling_ic.expanding(min_periods=IC_ROLLING_MIN_PERIODS).mean()
    decayed = pd.Series(False, index=rolling_ic.index)
    valid = long_term_mean.notna() & (long_term_mean > 0) & rolling_ic.notna()
    decayed.loc[valid] = rolling_ic.loc[valid] < threshold_ratio * long_term_mean.loc[valid]

    return pd.DataFrame({
        "recent_ic": rolling_ic,
        "long_term_mean": long_term_mean,
        "decayed": decayed,
    })


# ══════════════════════════════════════════════════════════════
# 3. 每日 JSON 用的 alert 快照
# ══════════════════════════════════════════════════════════════

def latest_alerts(decay_tables: Dict[str, pd.DataFrame]) -> Dict[str, Dict]:
    """
    給 Task 8 每日自動化用：取每個因子最新一天的衰退狀態，回傳可以直接
    塞進 signals/YYYY-MM-DD.json 的 "factor_decay_alerts" 欄位的字典。
    """
    alerts = {}
    for name, table in decay_tables.items():
        valid = table.dropna(subset=["recent_ic", "long_term_mean"])
        if valid.empty:
            alerts[name] = {"recent_ic": None, "long_term_mean": None, "decayed": None}
            continue
        last = valid.iloc[-1]
        alerts[name] = {
            "recent_ic": round(float(last["recent_ic"]), 4),
            "long_term_mean": round(float(last["long_term_mean"]), 4),
            "decayed": bool(last["decayed"]),
        }
    return alerts


# ══════════════════════════════════════════════════════════════
# 4. 主流程 + 圖表輸出
# ══════════════════════════════════════════════════════════════

def run_decay_monitor(db_path: str = DB_PATH, start: str = "2015-01-01",
                      end: str = "2026-04-17") -> Dict:
    logger.info("decay_monitor：載入資料、建構因子...")
    data = load_factor_inputs(db_path, start, end)
    factors = build_monitored_factors(data)
    fwd_ret = data["close"].pct_change(FWD_DAYS).shift(-FWD_DAYS)

    decay_tables = {}
    for name in FACTOR_NAMES:
        logger.info(f"decay_monitor：計算 {name} 的滾動 IC...")
        rolling_ic = compute_rolling_ic(factors[name], fwd_ret)
        decay_tables[name] = compute_decay_table(rolling_ic)

    alerts = latest_alerts(decay_tables)
    n_decayed = sum(1 for a in alerts.values() if a["decayed"])
    logger.info(f"decay_monitor：完成。最新一天有 {n_decayed}/{len(FACTOR_NAMES)} 個因子被判定衰退：{alerts}")

    return {"decay_tables": decay_tables, "alerts": alerts}


def write_decay_history_plot(decay_tables: Dict[str, pd.DataFrame],
                             path: str = "reports/factor_decay_history.png") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(decay_tables)
    fig, axes = plt.subplots(n, 1, figsize=(12, 2.2 * n), sharex=True)
    if n == 1:
        axes = [axes]

    for ax, (name, table) in zip(axes, decay_tables.items()):
        ax.plot(table.index, table["recent_ic"], label="252d rolling |IC|", color="tab:blue", linewidth=1)
        ax.plot(table.index, table["long_term_mean"], label="expanding long-term mean",
               color="tab:orange", linestyle="--", linewidth=1)
        decayed_mask = table["decayed"].fillna(False)
        if decayed_mask.any():
            ax.fill_between(table.index, 0, table["recent_ic"].max(skipna=True) or 0.01,
                           where=decayed_mask.values, color="red", alpha=0.15, label="decayed")
        ax.set_ylabel(name, fontsize=9)
        ax.legend(fontsize=7, loc="upper right")

    axes[-1].set_xlabel("date")
    fig.suptitle("Factor Decay Monitor — 252-day rolling |IC| vs expanding long-term mean")
    plt.tight_layout()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"decay_monitor：圖表已存到 {path}")


def write_alerts_snapshot(alerts: Dict, path: str = "reports/factor_decay_alerts_latest.json") -> None:
    """
    Task 7b 的 factor_decay_alerts 目前還沒有實際每日自動化管線可以掛進去
    （那是 Task 8 的範圍），這裡先輸出一份「如果今天跑 Task 8，daily JSON
    裡 factor_decay_alerts 欄位會長什麼樣子」的真實快照，證明這個功能已經
    可以直接被 Task 8 呼叫、不是空殼。
    """
    import json
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(alerts, fh, ensure_ascii=False, indent=2)
    logger.info(f"decay_monitor：alert 快照已存到 {path}")


if __name__ == "__main__":
    result = run_decay_monitor()
    write_decay_history_plot(result["decay_tables"])
    write_alerts_snapshot(result["alerts"])
    print("\n✅ Task 7b 因子衰退監控完成。")
    for name, a in result["alerts"].items():
        flag = "⚠️ 衰退" if a["decayed"] else ("✅ 正常" if a["decayed"] is not None else "n/a")
        print(f"   {name:14s} recent_ic={a['recent_ic']}  long_term_mean={a['long_term_mean']}  {flag}")
