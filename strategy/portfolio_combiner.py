"""
strategy/portfolio_combiner.py
─────────────────────────────────────────────────────────────
Final Optimal Portfolio — 多 sleeve 組合配置（N2）

設計依據（Pairing Analyzer 結論）：
  ─────────────────────────────────────────────────────────
  Sleeve A  L3 mom_52w (no CTA)        — 動能單因子，CAGR 10%, Sharpe 0.56
  Sleeve B  L3 inst_flow + CTA filter   — 籌碼配 CTA，最佳防禦互補
  Sleeve C  PEAD (no CTA)               — 事件驅動，與其他 sleeve 低相關
  ─────────────────────────────────────────────────────────

  CTA filter 套用規則：
    - mom_52w 已是趨勢追蹤 → 加 CTA 反而稀釋 → 不加
    - inst_flow 是基本面 → 加 CTA 提升 Sharpe → 加
    - PEAD 是事件驅動 → CTA 衝突 → 不加

組合方法：
  日報酬加權（每日 rebalance 至目標權重）：
    daily_combined_ret[t] = w_A × ret_A[t] + w_B × ret_B[t] + w_C × ret_C[t]

權重搜索：
  在 (w_A, w_B, w_C) 的 simplex 上 grid search 找最大 Sharpe / 最大 Calmar

直接執行：
  python strategy/portfolio_combiner.py
"""
import sys
import warnings
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.cta_module import build_proxy_market_cap, cta_signal
from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE, REPORT_START,
    load_matrices, build_factors, build_composite, build_positions,
    COMMISSION, TAX, SLIPPAGE, RF_RATE,
)
from strategy.pairing_analyzer import overlay_signal, compute_metrics


# ══════════════════════════════════════════════════════════════
# 1. 建構各 sleeve 的日報酬
# ══════════════════════════════════════════════════════════════

def build_single_factor_returns(factors: dict,
                                 close: pd.DataFrame,
                                 factor_name: str) -> pd.Series:
    """單因子 L3 部位 → 日淨報酬。"""
    weights = {factor_name: 1.0}
    composite = build_composite(factors, weights=weights)
    positions = build_positions(factors, composite)

    asset_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross = (positions * asset_ret).sum(axis=1)
    turnover = positions.diff().abs().sum(axis=1)
    cost = turnover * (COMMISSION + SLIPPAGE + COMMISSION + TAX + SLIPPAGE) / 2.0
    net = gross - cost

    mask = net.index >= pd.Timestamp(REPORT_START)
    return net.loc[mask]


def build_pead_returns() -> pd.Series:
    """從現有 PEAD CSV 讀取日報酬（節省重跑時間）。"""
    p = Path("reports/equity_curve_pead.csv")
    if not p.exists():
        raise FileNotFoundError("先執行 strategy/pead_module.py 產生 PEAD 淨值")
    df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
    return df["equity"].pct_change().fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 2. 組合多 sleeve（日報酬加權，等於每日 rebalance）
# ══════════════════════════════════════════════════════════════

def combine_returns(returns_dict: dict, weights_dict: dict) -> pd.Series:
    """
    日報酬加權合成。
    若策略當日缺報酬，視為 0（cash 同權重 buffer）。
    """
    aligned = {n: r for n, r in returns_dict.items()}
    common = None
    for r in aligned.values():
        common = r.index if common is None else common.intersection(r.index)

    df = pd.DataFrame({n: r.reindex(common).fillna(0.0)
                        for n, r in aligned.items()})
    weighted = sum(weights_dict[n] * df[n] for n in df.columns)
    return weighted


# ══════════════════════════════════════════════════════════════
# 3. 權重 simplex grid search（找最大 Sharpe）
# ══════════════════════════════════════════════════════════════

def grid_search_weights(returns_dict: dict,
                         step: float = 0.05) -> pd.DataFrame:
    """
    對 (w_A, w_B, w_C) 做 simplex grid search，找最高 Sharpe / Calmar。

    限制：sum(w) = 1, 0 ≤ w ≤ 1
    """
    names = list(returns_dict.keys())
    if len(names) != 3:
        raise ValueError("此 grid search 設計給 3 個 sleeve")

    rows = []
    grid = np.arange(0.0, 1.0 + step, step)
    for w_a in grid:
        for w_b in grid:
            w_c = 1.0 - w_a - w_b
            if w_c < -1e-9 or w_c > 1.0 + 1e-9:
                continue
            w_a, w_b, w_c = round(w_a, 4), round(w_b, 4), max(round(w_c, 4), 0.0)
            if abs(w_a + w_b + w_c - 1.0) > 1e-6:
                continue
            ws = {names[0]: w_a, names[1]: w_b, names[2]: w_c}
            blend = combine_returns(returns_dict, ws)
            m = compute_metrics(blend)
            rows.append({
                **{f"w_{n}": ws[n] for n in names},
                "cagr":   m["cagr"]   * 100,
                "vol":    m["vol"]    * 100,
                "sharpe": m["sharpe"],
                "mdd":    m["mdd"]    * 100,
                "calmar": m["calmar"],
            })
    return pd.DataFrame(rows)


# ══════════════════════════════════════════════════════════════
# 4. 主流程
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  📊 Final Optimal Portfolio Combiner（N2）")
    print("=" * 70)

    print("\n📂 步驟 1：載入資料 + 因子...")
    data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    factors = build_factors(data)
    factors["volume"] = data["volume"].replace(0.0, np.nan)
    close = factors["close"]

    print("\n🧮 步驟 2：建構 CTA 訊號（市值加權）...")
    proxy = build_proxy_market_cap(factors, data, rebal_freq="monthly")
    signal = cta_signal(proxy, mode="graduated")
    signal = signal.loc[signal.index >= pd.Timestamp(REPORT_START)]

    print("\n🧪 步驟 3：建構 3 個 sleeve...")
    print("  Sleeve A: L3 mom_52w (no CTA)")
    sleeve_a = build_single_factor_returns(factors, close, "mom_52w")
    print(f"    {len(sleeve_a)} 天")

    print("  Sleeve B: L3 inst_flow + CTA filter")
    raw_b = build_single_factor_returns(factors, close, "inst_flow")
    sleeve_b = overlay_signal(raw_b, signal, shift_days=1)
    print(f"    {len(sleeve_b)} 天")

    print("  Sleeve C: PEAD (no CTA)")
    sleeve_c = build_pead_returns()
    sleeve_c = sleeve_c.loc[sleeve_c.index >= pd.Timestamp(REPORT_START)]
    print(f"    {len(sleeve_c)} 天")

    # 對齊到共同期間
    common = sleeve_a.index.intersection(sleeve_b.index).intersection(sleeve_c.index)
    sleeves = {
        "mom_52w_no_cta": sleeve_a.reindex(common).fillna(0.0),
        "inst_flow_cta":  sleeve_b.reindex(common).fillna(0.0),
        "pead_no_cta":    sleeve_c.reindex(common).fillna(0.0),
    }
    print(f"\n  共同期間：{common[0].date()} ~ {common[-1].date()} ({len(common)} 天)")

    # 個別 sleeve 績效
    print("\n📊 步驟 4：個別 Sleeve 績效")
    for name, ret in sleeves.items():
        m = compute_metrics(ret)
        print(f"  {name:<20}  CAGR {m['cagr']*100:+.2f}%  "
              f"Sharpe {m['sharpe']:.3f}  MDD {m['mdd']*100:+.2f}%")

    # Sleeve 之間的相關性
    print("\n🔗 步驟 5：Sleeve 相關性矩陣")
    sleeve_df = pd.DataFrame(sleeves)
    corr = sleeve_df.corr()
    print(corr.round(3).to_string())
    print(f"\n  💡 平均相關性 {(corr.values[np.triu_indices_from(corr, k=1)]).mean():.3f}")
    print(f"     越低 → 分散效益越大")

    # ── 步驟 6：grid search 最佳權重 ──────────────────
    print("\n🎯 步驟 6：權重 grid search...")
    grid = grid_search_weights(sleeves, step=0.05)

    # 找最大 Sharpe
    best_sharpe = grid.loc[grid["sharpe"].idxmax()]
    print("\n  🥇 最大 Sharpe 配置：")
    for col in [c for c in grid.columns if c.startswith("w_")]:
        print(f"      {col}: {best_sharpe[col]:.2f}")
    print(f"      CAGR    {best_sharpe['cagr']:+.2f}%")
    print(f"      Sharpe  {best_sharpe['sharpe']:.3f}")
    print(f"      MDD     {best_sharpe['mdd']:+.2f}%")
    print(f"      Calmar  {best_sharpe['calmar']:.3f}")

    # 找最大 Calmar（風險調整後最佳）
    best_calmar = grid.loc[grid["calmar"].idxmax()]
    print("\n  🛡️  最大 Calmar 配置（CAGR/MDD）：")
    for col in [c for c in grid.columns if c.startswith("w_")]:
        print(f"      {col}: {best_calmar[col]:.2f}")
    print(f"      CAGR    {best_calmar['cagr']:+.2f}%")
    print(f"      Sharpe  {best_calmar['sharpe']:.3f}")
    print(f"      MDD     {best_calmar['mdd']:+.2f}%")
    print(f"      Calmar  {best_calmar['calmar']:.3f}")

    # 等權配置作對照
    eq_w = {n: 1/3 for n in sleeves}
    eq_blend = combine_returns(sleeves, eq_w)
    m_eq = compute_metrics(eq_blend)
    print("\n  ⚖️  等權配置（1/3 each）對照：")
    print(f"      CAGR    {m_eq['cagr']*100:+.2f}%")
    print(f"      Sharpe  {m_eq['sharpe']:.3f}")
    print(f"      MDD     {m_eq['mdd']*100:+.2f}%")

    # ── 步驟 7：輸出最終淨值曲線 ──────────────────────
    print("\n💾 步驟 7：輸出最佳 Sharpe 組合淨值...")
    # 去掉 "w_" 前綴用 col[2:]，不能用 replace 否則會誤殺 "52w_"
    best_weights = {col[2:]: best_sharpe[col]
                    for col in grid.columns if col.startswith("w_")}
    best_blend = combine_returns(sleeves, best_weights)
    best_equity = (1 + best_blend).cumprod()

    Path("reports").mkdir(exist_ok=True)
    best_equity.to_frame("equity").to_csv("reports/equity_curve_final_combo.csv")
    grid.to_csv("reports/grid_search_weights.csv", index=False)
    print(f"  💾 reports/equity_curve_final_combo.csv")
    print(f"  💾 reports/grid_search_weights.csv（{len(grid)} 個權重組合）")

    # ── 步驟 8：3D plot of Sharpe surface ─────────────
    try:
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Heiti TC", "PingFang TC",
                                            "Arial Unicode MS", "DejaVu Sans"]

        fig, ax = plt.subplots(figsize=(9, 7))
        # heatmap of Sharpe over (w_A, w_B), with w_C = 1 - w_A - w_B
        pivot = grid.pivot_table(index="w_mom_52w_no_cta",
                                  columns="w_inst_flow_cta",
                                  values="sharpe")
        im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto", origin="lower",
                        extent=[pivot.columns.min(), pivot.columns.max(),
                                pivot.index.min(), pivot.index.max()])
        ax.set_xlabel("w_inst_flow_cta", fontsize=12)
        ax.set_ylabel("w_mom_52w_no_cta", fontsize=12)
        ax.set_title("Sharpe Heatmap — Sleeve A × B（C = 1 - A - B）",
                     fontsize=13, fontweight="bold")
        plt.colorbar(im, ax=ax, label="Sharpe")

        # 標註最佳點
        ax.scatter(best_sharpe["w_inst_flow_cta"],
                   best_sharpe["w_mom_52w_no_cta"],
                   marker="*", s=300, color="white", edgecolor="black",
                   linewidth=2, label=f"Best Sharpe {best_sharpe['sharpe']:.2f}")
        ax.legend()
        plt.tight_layout()
        plt.savefig("reports/pairing_plots/sharpe_surface.png", dpi=120, bbox_inches="tight")
        plt.close()
        print(f"  💾 reports/pairing_plots/sharpe_surface.png")
    except Exception as e:
        print(f"  ⚠️  3D plot 失敗：{e}")

    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    main()
