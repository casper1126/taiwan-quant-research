"""
pairs_module.py
─────────────────────────────────────────────────────────────
配對交易（Statistical Arbitrage）模組 — K

理論根源：
  Gatev, Goetzmann, Rouwenhorst (2006)
  "Pairs Trading: Performance of a Relative-Value Arbitrage Rule"
  → 兩檔長期同步移動的股票，當價差暫時拉大時做反向交易

協整 vs 相關（你研究筆記第 4.1 節）：
  相關 = 短期同步漲跌
  協整 = 長期均值回歸關係（spread 是 stationary）
  本實作用「rolling 252 天相關 > 0.85」當 cheap proxy（簡化版）

訊號（z-score thresholds，Avellaneda-Lee 2010）：
  z > +2.0  → 做空 A 做多 B（spread 過寬會回歸）
  z < -2.0  → 做多 A 做空 B
  |z| < 0.5 → 平倉
  z > +3.5 或 z < -3.5 → 強制止損出場（防範 spread 結構性破裂）

台股限制：
  - 放空需在融券名單（約 600 檔，本策略假設選的對都符合）
  - 借券利率：本回測簡化為 0（實務 ~1-3%/年）
  - 漲跌停 ±10%：可能中斷套利

直接執行：
  python strategy/pairs_module.py            — 標準回測
  python strategy/pairs_module.py --combine  — 與 L3 組合
"""
import sys
import warnings
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE, REPORT_START,
    COMMISSION, TAX, SLIPPAGE, RF_RATE,
    load_matrices, build_factors, _industry_of,
)

# ── 配對交易參數 ──────────────────────────────────────────────
TOP_K_UNIVERSE   = 80      # 從流動宇宙取前 80 檔配對（避免 n² 爆炸）
MIN_CORR         = 0.85    # 最低相關門檻
CORR_LOOKBACK    = 252     # 1 年滾動相關
SPREAD_LOOKBACK  = 60      # spread z-score 的滾動視窗
ENTRY_Z          = 2.0
EXIT_Z           = 0.5
STOP_Z           = 3.5
N_PAIRS          = 20      # 同時持有的對數
PAIR_NOTIONAL    = 1.0     # 每對的總名目（多+空 各 0.5）

# 組合權重
W_L3_PAIRS  = 0.85
W_PAIRS     = 0.15


# ══════════════════════════════════════════════════════════════
# 1. 找配對（同產業 + 高相關）
# ══════════════════════════════════════════════════════════════

def find_pairs(close: pd.DataFrame,
                liquid_mask: pd.DataFrame,
                top_k: int = TOP_K_UNIVERSE,
                min_corr: float = MIN_CORR,
                corr_lookback: int = CORR_LOOKBACK) -> List[Tuple[str, str, float, str]]:
    """
    在「同產業 + 高相關」中找配對。

    流程：
      1. 取流動宇宙最後一天前 top_k 檔（最近最活躍）
      2. 按產業分組
      3. 每組內計算 252 日 log-price 相關性
      4. 取相關 > min_corr 的對

    返回：[(stock_a, stock_b, corr, industry), ...]
    """
    last_day = close.index[-1]
    dv_rank = liquid_mask.loc[last_day]
    # 取目前 liquid 的股票（為簡化，用 last day 流動性）
    candidates = dv_rank[dv_rank].index.tolist()[:top_k]

    if len(candidates) < 4:
        return []

    # 計算 log-price（用價格比的對數差消除幅度差異）
    log_p = np.log(close[candidates].iloc[-corr_lookback:])

    # 產業分組
    ind_map = {s: _industry_of(s) for s in candidates}

    pairs: List[Tuple[str, str, float, str]] = []
    for ind in set(ind_map.values()):
        if ind == "OTHER":
            continue
        members = [s for s, i in ind_map.items() if i == ind]
        if len(members) < 2:
            continue
        sub = log_p[members].dropna(how="any")
        if sub.shape[0] < 60:
            continue
        corr = sub.corr()
        for a, b in combinations(members, 2):
            if a in corr.index and b in corr.columns:
                c = corr.loc[a, b]
                if c >= min_corr:
                    pairs.append((a, b, c, ind))

    pairs.sort(key=lambda x: x[2], reverse=True)
    return pairs


# ══════════════════════════════════════════════════════════════
# 2. 計算 spread z-score（每個對）
# ══════════════════════════════════════════════════════════════

def spread_zscore(close: pd.DataFrame,
                   pair: Tuple[str, str],
                   lookback: int = SPREAD_LOOKBACK) -> pd.Series:
    """
    spread = log(price_A) − β × log(price_B)
    β 用 OLS 估計（rolling）
    z = (spread − rolling_mean) / rolling_std
    """
    a, b = pair
    log_a = np.log(close[a])
    log_b = np.log(close[b])

    # 簡化：用全期 β（避免 rolling OLS 複雜度）— 實務應 rolling
    valid = log_a.notna() & log_b.notna()
    if valid.sum() < lookback:
        return pd.Series(dtype=float, index=close.index)
    beta = np.cov(log_a[valid], log_b[valid])[0, 1] / np.var(log_b[valid])

    spread = log_a - beta * log_b
    mean   = spread.rolling(lookback, min_periods=lookback // 2).mean()
    std    = spread.rolling(lookback, min_periods=lookback // 2).std().replace(0, np.nan)
    return (spread - mean) / std


# ══════════════════════════════════════════════════════════════
# 3. 配對訊號
# ══════════════════════════════════════════════════════════════

def pair_signal(zscore: pd.Series,
                 entry: float = ENTRY_Z,
                 exit: float = EXIT_Z,
                 stop: float = STOP_Z) -> pd.Series:
    """
    回傳 +1 / -1 / 0 訊號（從 A 角度）：
      z > +entry → A 過貴 → 做空 A，多 B → A 部位 = -1
      z < -entry → A 過便宜 → 多 A，空 B → A 部位 = +1
      |z| < exit → 平倉 → 0
      |z| > stop → 強制平倉（spread 結構破裂）→ 0
    """
    pos = pd.Series(0.0, index=zscore.index)
    state = 0  # 0=flat, +1=long A, -1=short A
    for d, z in zscore.items():
        if pd.isna(z):
            pos.loc[d] = state
            continue
        if state == 0:
            if z > entry:
                state = -1
            elif z < -entry:
                state = +1
        else:
            if abs(z) < exit or abs(z) > stop:
                state = 0
        pos.loc[d] = state
    return pos.shift(1).fillna(0.0)   # 防 look-ahead


# ══════════════════════════════════════════════════════════════
# 4. 配對組合回測（每對等權）
# ══════════════════════════════════════════════════════════════

def backtest_pairs_portfolio(close: pd.DataFrame,
                              pairs: List[Tuple[str, str, float, str]]
                              ) -> Tuple[dict, pd.Series, int]:
    """
    取前 N_PAIRS 對，每對等權配置（多空各佔 50%）。
    每對的 PnL = +signal_A × ret_A − signal_A × ret_B
              （signal=+1 時多 A 空 B；-1 時反之）
    """
    if not pairs:
        return {}, pd.Series(dtype=float), 0

    selected = pairs[:N_PAIRS]
    if not selected:
        return {}, pd.Series(dtype=float), 0

    daily_ret = close.pct_change().fillna(0.0)
    pair_w    = 1.0 / len(selected)
    total_pnl = pd.Series(0.0, index=close.index)
    total_to  = pd.Series(0.0, index=close.index)
    n_active  = 0

    for a, b, corr, ind in selected:
        if a not in close.columns or b not in close.columns:
            continue
        z = spread_zscore(close, (a, b))
        sig = pair_signal(z)
        # PnL：sig × (ret_A − ret_B)，每對 0.5 多 + 0.5 空
        pnl = sig * (daily_ret[a] - daily_ret[b]) * 0.5
        # 換手：訊號變化代表進出場
        flip = sig.diff().abs().fillna(0.0)
        # 多空兩端都有成本（買 + 賣，含證交稅）
        cost = flip * (COMMISSION + SLIPPAGE + (COMMISSION + TAX + SLIPPAGE)) * 0.5
        total_pnl += pair_w * (pnl - cost)
        total_to  += pair_w * flip
        n_active += 1

    mask = total_pnl.index >= pd.Timestamp(REPORT_START)
    net  = total_pnl.loc[mask]
    equity = (1 + net).cumprod()

    if len(equity) < 2:
        return {}, equity, n_active

    total_ret = equity.iloc[-1] - 1
    years     = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr      = (1 + total_ret) ** (1 / years) - 1
    vol       = net.std() * np.sqrt(252)
    sharpe    = (cagr - RF_RATE) / vol if vol > 0 else 0.0
    mdd       = (equity / equity.cummax() - 1).min()

    stats = {
        "total_ret":   round(total_ret * 100, 2),
        "cagr":        round(cagr * 100, 2),
        "vol":         round(vol * 100, 2),
        "sharpe":      round(sharpe, 3),
        "max_dd":      round(mdd * 100, 2),
        "n_pairs":     n_active,
        "annual_to":   round(total_to.loc[mask].mean() * 252 * 100, 1),
    }
    return stats, equity, n_active


# ══════════════════════════════════════════════════════════════
# 5. 主程式
# ══════════════════════════════════════════════════════════════

def run_pairs(combine_with_l3: bool = False) -> None:
    print("\n" + "=" * 60)
    print("  配對交易模組（K）")
    print("=" * 60)

    print("📂 載入資料...")
    data    = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    factors = build_factors(data)
    close       = factors["close"].dropna(axis=1, how="all")
    liquid_mask = factors["liquid_mask"]

    print(f"\n🔍 同產業配對搜索（top {TOP_K_UNIVERSE}, corr ≥ {MIN_CORR}）...")
    pairs = find_pairs(close, liquid_mask)
    print(f"  找到 {len(pairs)} 對符合條件的股票對")
    if pairs:
        print("  Top 10 候選：")
        for a, b, c, ind in pairs[:10]:
            print(f"    {a} ↔ {b}   corr={c:.3f}   industry={ind}")

    print(f"\n📈 配對組合回測（{N_PAIRS} 對等權）...")
    stats, equity, n_active = backtest_pairs_portfolio(close, pairs)

    if not stats:
        print("  ⚠️  沒有有效配對可回測")
        return

    print("\n" + "=" * 60)
    print("  配對交易標準回測結果")
    print("=" * 60)
    for k, v in stats.items():
        print(f"    {k:<10} {v}")

    Path("reports").mkdir(exist_ok=True)
    equity.to_frame("equity").to_csv("reports/equity_curve_pairs.csv")
    print(f"\n  💾 reports/equity_curve_pairs.csv")

    if combine_with_l3:
        l3_path = "reports/equity_curve_L3_ml.csv"
        if not Path(l3_path).exists():
            l3_path = "reports/equity_curve_L3.csv"
        print(f"\n📂 讀取 L3 淨值：{l3_path}")
        l3_eq = pd.read_csv(l3_path, parse_dates=["date"]).set_index("date")["equity"]

        common = equity.index.intersection(l3_eq.index)
        l3_norm    = (l3_eq.reindex(common)    / l3_eq.reindex(common).iloc[0])
        pair_norm  = (equity.reindex(common)   / equity.reindex(common).iloc[0])

        ret_l3   = l3_norm.pct_change().fillna(0.0)
        ret_pair = pair_norm.pct_change().fillna(0.0)
        combined = (1 + W_L3_PAIRS * ret_l3 + W_PAIRS * ret_pair).cumprod()

        def _metrics(eq, label):
            rets = eq.pct_change().dropna()
            yrs = (eq.index[-1] - eq.index[0]).days / 365.25
            cagr = eq.iloc[-1] ** (1 / yrs) - 1
            vol  = rets.std() * np.sqrt(252)
            sharpe = (cagr - RF_RATE) / vol if vol > 0 else 0.0
            mdd  = (eq / eq.cummax() - 1).min()
            print(f"\n  ─── {label} ───")
            print(f"    CAGR    {cagr*100:+.2f}%")
            print(f"    Sharpe  {sharpe:.3f}")
            print(f"    MDD     {mdd*100:+.2f}%")

        _metrics(l3_norm,   "Layer 3（100%）")
        _metrics(pair_norm, "Pairs（100%）")
        _metrics(combined,  f"組合 ({W_L3_PAIRS*100:.0f}% L3 + {W_PAIRS*100:.0f}% Pairs)")

        corr = ret_l3.corr(ret_pair)
        print(f"\n  📐 L3 vs Pairs 日報酬相關性：{corr:.3f}")
        combined.to_frame("equity").to_csv("reports/equity_curve_combined_pairs.csv")
        print(f"  💾 reports/equity_curve_combined_pairs.csv")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--combine", action="store_true")
    args = p.parse_args()
    run_pairs(combine_with_l3=args.combine)
