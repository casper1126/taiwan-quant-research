"""
strategy/pairing_analyzer.py
─────────────────────────────────────────────────────────────
Strategy Pairing Analyzer

評估 CTA filter 與不同 L3 底層策略的搭配效果，並自動推薦最佳組合。

用途：
  - 接收 CTA 訊號 (0/0.5/1) 與多個策略的日報酬
  - 計算每個策略「疊加 CTA 前 vs 後」的績效差異
  - 自動分類：最佳防禦互補 / 良好搭配 / 趨勢衝突 / 中性
  - 輸出對比圖 + Sharpe 提升 bar chart

API:
  overlay_signal(strategy_ret, cta_signal, shift_days=1)  → filtered return
  compute_metrics(returns)                                 → metrics dict
  compute_delta(before, after)                             → delta dict
  recommend(delta)                                         → label
  analyze_pairings(returns_df, cta_signal)                 → results DataFrame
  plot_overlay_comparison(name, ret_before, ret_after)     → matplotlib fig
  plot_sharpe_bars(results_df)                             → matplotlib fig
"""
from __future__ import annotations

import warnings
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

# ── 評估參數 ─────────────────────────────────────────────────
RF_RATE = 0.015
TRADING_DAYS = 252

# ── 推薦判定門檻（可調整）─────────────────────────────────────
SHARPE_DELTA_GOOD     = 0.05     # Sharpe 提升 > 0.05 → 顯著
SHARPE_DELTA_BAD      = -0.05    # Sharpe 下降 > 0.05 → 趨勢衝突
MDD_IMPROVE_GOOD      = -0.02    # MDD 改善 > 2%（負值變更負）
CAGR_TOLERANCE        = -0.01    # CAGR 損失容忍度 1%


# ══════════════════════════════════════════════════════════════
# 1. Overlay：將 CTA 訊號疊加到策略
# ══════════════════════════════════════════════════════════════

def overlay_signal(strategy_ret: pd.Series,
                    cta_signal: pd.Series,
                    shift_days: int = 1) -> pd.Series:
    """
    將 CTA 濾網疊加到策略日報酬。

    公式：
      Filtered_Return[t] = Strategy_Return[t] × CTA_Signal[t-shift_days]

    shift_days=1 是必要的（防 look-ahead bias）：
      今日 close 後才能算出 CTA 訊號 → 明日才能依此調整曝險

    Args:
      strategy_ret: pd.Series，策略日報酬（index = date）
      cta_signal:   pd.Series，CTA 訊號（值域 [0, 1]，1=多頭，0=空手）
      shift_days:   訊號延遲執行天數（預設 1）

    Returns:
      pd.Series，疊加後的日報酬
    """
    aligned = cta_signal.shift(shift_days).reindex(strategy_ret.index).ffill().fillna(0.0)
    aligned = aligned.clip(lower=0.0, upper=1.0)
    return strategy_ret * aligned


# ══════════════════════════════════════════════════════════════
# 2. 績效指標計算（向量化）
# ══════════════════════════════════════════════════════════════

def compute_metrics(returns: pd.Series, rf_rate: float = RF_RATE) -> Dict[str, float]:
    """
    從日報酬序列計算標準績效指標。
    全向量化，無 for 迴圈。
    """
    rets = returns.dropna()
    if len(rets) < 2:
        return {"cagr": 0, "vol": 0, "sharpe": 0, "mdd": 0, "calmar": 0,
                "total_ret": 0, "win_rate": 0, "n_days": 0}

    equity = (1.0 + rets).cumprod()
    total_ret = equity.iloc[-1] - 1.0
    years = (rets.index[-1] - rets.index[0]).days / 365.25
    cagr = (1.0 + total_ret) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    vol = rets.std() * np.sqrt(TRADING_DAYS)
    sharpe = (cagr - rf_rate) / vol if vol > 0 else 0.0
    mdd = float((equity / equity.cummax() - 1.0).min())
    calmar = cagr / abs(mdd) if mdd != 0 else 0.0
    win_rate = float((rets > 0).mean())

    return {
        "cagr":      float(cagr),
        "vol":       float(vol),
        "sharpe":    float(sharpe),
        "mdd":       float(mdd),
        "calmar":    float(calmar),
        "total_ret": float(total_ret),
        "win_rate":  float(win_rate),
        "n_days":    int(len(rets)),
    }


def compute_delta(before: Dict[str, float],
                   after: Dict[str, float]) -> Dict[str, float]:
    """
    計算疊加前後的差異。

    mdd_improvement: > 0 = MDD 縮小（好），< 0 = MDD 擴大（壞）
    """
    return {
        "cagr_delta":         after["cagr"]   - before["cagr"],
        "sharpe_delta":       after["sharpe"] - before["sharpe"],
        "vol_delta":          after["vol"]    - before["vol"],
        "mdd_improvement":    abs(before["mdd"]) - abs(after["mdd"]),
        "calmar_delta":       after["calmar"] - before["calmar"],
        "cagr_pct_change":    ((after["cagr"] - before["cagr"]) / abs(before["cagr"])
                                if before["cagr"] != 0 else 0.0),
        "sharpe_pct_change":  ((after["sharpe"] - before["sharpe"]) / abs(before["sharpe"])
                                if before["sharpe"] != 0 else 0.0),
    }


# ══════════════════════════════════════════════════════════════
# 3. 自動推薦引擎
# ══════════════════════════════════════════════════════════════

def recommend(delta: Dict[str, float],
              before: Dict[str, float],
              after: Dict[str, float]) -> Tuple[str, str]:
    """
    根據 delta 給出文字建議與分類標籤。

    分類邏輯（依優先順序）：
      ① 最佳防禦互補:   Sharpe 顯著提升 + MDD 顯著縮小
      ② 良好搭配:       Sharpe 顯著提升，CAGR 損失可接受
      ③ 純風控:         Sharpe 變化小，但 MDD 改善
      ④ 趨勢衝突:       Sharpe 顯著下降（CTA 與策略時序衝突）
      ⑤ 中性:           無顯著變化
      ⑥ 邊際改善:       小幅改善

    Returns:
      (category, message)
    """
    sharpe_delta = delta["sharpe_delta"]
    mdd_improvement = delta["mdd_improvement"]
    cagr_delta = delta["cagr_delta"]

    sharpe_up = sharpe_delta > SHARPE_DELTA_GOOD
    sharpe_dn = sharpe_delta < SHARPE_DELTA_BAD
    mdd_up = mdd_improvement > abs(MDD_IMPROVE_GOOD)
    cagr_kept = cagr_delta > CAGR_TOLERANCE

    if sharpe_up and mdd_up:
        return ("BEST_DEFENSIVE",
                "🏆 最佳防禦互補 — Sharpe ↑ 且 MDD ↓，風險調整後報酬大幅改善")
    if sharpe_up and cagr_kept:
        return ("GOOD_PAIR",
                "✅ 良好搭配 — Sharpe 顯著提升，CAGR 損失可接受")
    if not sharpe_up and mdd_up and cagr_kept:
        return ("RISK_CONTROL",
                "🛡️ 純風控搭配 — MDD 改善但 Sharpe 變化不大（適合保守投資）")
    if sharpe_dn:
        return ("CONFLICT",
                "⚠️ 趨勢衝突 — Sharpe 顯著下降，CTA 與該策略時序不合，不建議搭配")
    if abs(sharpe_delta) < SHARPE_DELTA_GOOD * 0.5:
        return ("NEUTRAL",
                "➖ 中性 — 疊加 CTA 對該策略無顯著影響")
    return ("MARGINAL",
            "📊 邊際改善 — 小幅優化，值得進一步測試")


# ══════════════════════════════════════════════════════════════
# 4. 主分析函數
# ══════════════════════════════════════════════════════════════

def analyze_pairings(strategies_returns: pd.DataFrame,
                      cta_signal: pd.Series,
                      shift_days: int = 1) -> pd.DataFrame:
    """
    為每個策略計算疊加 CTA 後的指標 + 自動建議。

    Args:
      strategies_returns: DataFrame，columns = 策略名，每欄為日報酬
      cta_signal:         CTA 訊號 Series（已經 shift 處理過）
      shift_days:         額外的延遲天數（預設 1，防 look-ahead）

    Returns:
      DataFrame，每行一個策略，含完整指標 + 推薦分類
    """
    rows = []
    for name in strategies_returns.columns:
        ret_before = strategies_returns[name].dropna()
        if len(ret_before) < 30:
            continue

        ret_after = overlay_signal(ret_before, cta_signal, shift_days=shift_days)

        m_before = compute_metrics(ret_before)
        m_after  = compute_metrics(ret_after)
        delta    = compute_delta(m_before, m_after)
        category, msg = recommend(delta, m_before, m_after)

        rows.append({
            "strategy":          name,
            "cagr_before":       round(m_before["cagr"]   * 100, 2),
            "cagr_after":        round(m_after["cagr"]    * 100, 2),
            "cagr_delta":        round(delta["cagr_delta"]   * 100, 2),
            "sharpe_before":     round(m_before["sharpe"], 3),
            "sharpe_after":      round(m_after["sharpe"],  3),
            "sharpe_delta":      round(delta["sharpe_delta"], 3),
            "mdd_before":        round(m_before["mdd"]    * 100, 2),
            "mdd_after":         round(m_after["mdd"]     * 100, 2),
            "mdd_improvement":   round(delta["mdd_improvement"] * 100, 2),
            "vol_before":        round(m_before["vol"]    * 100, 2),
            "vol_after":         round(m_after["vol"]     * 100, 2),
            "category":          category,
            "recommendation":    msg,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # 排序：BEST_DEFENSIVE → GOOD_PAIR → RISK_CONTROL → MARGINAL → NEUTRAL → CONFLICT
    cat_order = {"BEST_DEFENSIVE": 0, "GOOD_PAIR": 1, "RISK_CONTROL": 2,
                 "MARGINAL": 3, "NEUTRAL": 4, "CONFLICT": 5}
    df["_cat_order"] = df["category"].map(cat_order)
    df = df.sort_values(["_cat_order", "sharpe_delta"],
                         ascending=[True, False]).drop("_cat_order", axis=1)
    return df.reset_index(drop=True)


# ══════════════════════════════════════════════════════════════
# 5. 視覺化
# ══════════════════════════════════════════════════════════════

def plot_overlay_comparison(name: str,
                             ret_before: pd.Series,
                             ret_after: pd.Series,
                             save_path: str = None,
                             show: bool = False):
    """畫單一策略的「疊加前 vs 疊加後」雙淨值曲線對比。"""
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Heiti TC", "PingFang TC", "Arial Unicode MS",
                                        "Microsoft JhengHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    eq_before = (1 + ret_before).cumprod()
    eq_after  = (1 + ret_after.reindex(eq_before.index).fillna(0)).cumprod()

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(eq_before.index, eq_before.values,
            label=f"{name} 原始", linewidth=2, color="#3498db")
    ax.plot(eq_after.index, eq_after.values,
            label=f"{name} + CTA Filter", linewidth=2, color="#e74c3c", linestyle="--")
    ax.set_title(f"{name}：CTA 疊加前 vs 後", fontsize=14, fontweight="bold")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative Equity")
    ax.legend(loc="upper left", fontsize=11)
    ax.grid(alpha=0.3)
    ax.axhline(1.0, color="gray", linewidth=0.5, linestyle=":")
    plt.tight_layout()

    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(save_path, dpi=120, bbox_inches="tight")
    if show:
        plt.show()
    plt.close()


def plot_sharpe_bars(results_df: pd.DataFrame,
                      save_path: str = None,
                      show: bool = False):
    """畫各策略 Sharpe 提升的 bar chart。"""
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Heiti TC", "PingFang TC", "Arial Unicode MS",
                                        "Microsoft JhengHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    if results_df.empty:
        print("⚠️  results_df empty，跳過 plot")
        return

    df = results_df.sort_values("sharpe_delta")
    colors = ["#27ae60" if x > 0 else "#e74c3c" for x in df["sharpe_delta"]]

    fig, ax = plt.subplots(figsize=(11, 6))
    bars = ax.barh(df["strategy"], df["sharpe_delta"], color=colors, alpha=0.8)
    ax.axvline(0, color="black", linewidth=1)
    ax.set_xlabel("Sharpe Ratio Δ (after − before)", fontsize=12)
    ax.set_title("CTA Filter 對各 L3 底層策略的 Sharpe 影響", fontsize=14, fontweight="bold")
    ax.grid(alpha=0.3, axis="x")

    # 標註數值
    for bar, v in zip(bars, df["sharpe_delta"]):
        x = bar.get_width()
        ax.text(x + (0.005 if x >= 0 else -0.005),
                bar.get_y() + bar.get_height() / 2,
                f"{v:+.3f}",
                va="center",
                ha="left" if x >= 0 else "right",
                fontsize=9, fontweight="bold")

    plt.tight_layout()
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(save_path, dpi=120, bbox_inches="tight")
    if show:
        plt.show()
    plt.close()


def plot_metrics_heatmap(results_df: pd.DataFrame,
                          save_path: str = None,
                          show: bool = False):
    """各策略指標總覽 heatmap（CAGR/Sharpe/MDD 變化）。"""
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["Heiti TC", "PingFang TC", "Arial Unicode MS",
                                        "Microsoft JhengHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    if results_df.empty:
        return

    metrics = ["cagr_delta", "sharpe_delta", "mdd_improvement"]
    label_map = {"cagr_delta": "CAGR Δ (%)", "sharpe_delta": "Sharpe Δ",
                 "mdd_improvement": "MDD 改善 (%)"}
    data = results_df.set_index("strategy")[metrics].rename(columns=label_map)

    fig, ax = plt.subplots(figsize=(10, max(4, 0.6 * len(data))))
    im = ax.imshow(data.values, cmap="RdYlGn", aspect="auto",
                   vmin=-data.abs().values.max(), vmax=data.abs().values.max())
    ax.set_xticks(range(len(data.columns)))
    ax.set_xticklabels(data.columns, rotation=0, fontsize=11)
    ax.set_yticks(range(len(data.index)))
    ax.set_yticklabels(data.index, fontsize=11)

    for i in range(len(data.index)):
        for j in range(len(data.columns)):
            v = data.values[i, j]
            ax.text(j, i, f"{v:+.2f}", ha="center", va="center",
                    color="black", fontsize=10, fontweight="bold")

    ax.set_title("各 L3 策略 + CTA 改善幅度總覽", fontsize=13, fontweight="bold")
    plt.colorbar(im, ax=ax, label="改善幅度")
    plt.tight_layout()
    if save_path:
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(save_path, dpi=120, bbox_inches="tight")
    if show:
        plt.show()
    plt.close()


# ══════════════════════════════════════════════════════════════
# 6. 報告整合
# ══════════════════════════════════════════════════════════════

def print_report(results_df: pd.DataFrame) -> None:
    """印出完整文字報告。"""
    if results_df.empty:
        print("⚠️  沒有可分析的策略")
        return

    print("\n" + "═" * 90)
    print("  Strategy Pairing Analyzer — 結果報告")
    print("═" * 90)

    # 摘要表
    cols = ["strategy", "cagr_before", "cagr_after", "cagr_delta",
            "sharpe_before", "sharpe_after", "sharpe_delta",
            "mdd_before", "mdd_after", "mdd_improvement"]
    print("\n📊 績效指標（疊加前 → 疊加後 → 變化）")
    print(results_df[cols].to_string(index=False))

    print("\n💡 自動推薦分類")
    print("─" * 90)
    for _, r in results_df.iterrows():
        print(f"  [{r['category']:<16}] {r['strategy']:<20}  {r['recommendation']}")
    print("═" * 90 + "\n")
