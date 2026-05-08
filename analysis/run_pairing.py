"""
analysis/run_pairing.py
─────────────────────────────────────────────────────────────
執行 Strategy Pairing Analyzer：

1. 建構市值加權 CTA 訊號（mcap proxy + TSMOM/MA combined signal）
2. 載入多個 L3 子策略的歷史日報酬：
   - L3 ML Ensemble（v12）
   - PEAD
   - Pure ML
   - 單因子變體（Mom-only / Value-only / Rev-only / Inst-only / LowVol-only）
3. 疊加 CTA 並做配對分析
4. 輸出文字報告 + 圖表

直接執行：
  python analysis/run_pairing.py
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.cta_module import build_proxy_market_cap, cta_signal
from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE, REPORT_START,
    load_matrices, build_factors, build_composite, build_positions,
    SMOOTH_WINDOW, _industry_neutral_zscore,
)
from strategy.pairing_analyzer import (
    analyze_pairings, print_report,
    plot_overlay_comparison, plot_sharpe_bars, plot_metrics_heatmap,
    overlay_signal,
)

REPORTS_DIR = Path("reports")
PLOTS_DIR   = REPORTS_DIR / "pairing_plots"


# ══════════════════════════════════════════════════════════════
# 1. 載入既有策略淨值 → 日報酬
# ══════════════════════════════════════════════════════════════

def load_equity_returns(name: str, path: str) -> pd.Series:
    """從 CSV 讀淨值並轉日報酬。"""
    p = Path(path)
    if not p.exists():
        return pd.Series(dtype=float, name=name)
    df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
    eq = df["equity"]
    return eq.pct_change().fillna(0.0).rename(name)


# ══════════════════════════════════════════════════════════════
# 2. 建構單因子 L3 變體（共用一份 factors，只改 FACTOR_WEIGHTS）
# ══════════════════════════════════════════════════════════════

def build_single_factor_returns(factors: dict,
                                 close: pd.DataFrame,
                                 factor_name: str) -> pd.Series:
    """
    用單一因子（100% 權重）建構 L3 部位並計算日報酬。
    其他結構（風險平價+等權混合、產業 cap、4 級擇時）保持。
    """
    weights = {factor_name: 1.0}
    composite = build_composite(factors, weights=weights)
    positions = build_positions(factors, composite)

    asset_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross = (positions * asset_ret).sum(axis=1)
    turnover = positions.diff().abs().sum(axis=1)

    # 簡化交易成本（同 quant_layer3）
    cost = turnover * (0.001425 + 0.001 + 0.001425 + 0.003 + 0.001) / 2.0
    net = gross - cost

    mask = net.index >= pd.Timestamp(REPORT_START)
    return net.loc[mask].rename(f"L3_{factor_name}")


# ══════════════════════════════════════════════════════════════
# 3. 主流程
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  🔍 Strategy Pairing Analyzer 執行")
    print("=" * 70)

    # ── (a) 建構 CTA 訊號（市值加權 proxy）──────────────────
    print("\n📊 步驟 1：建構市值加權 CTA 訊號...")
    data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    factors = build_factors(data)
    factors["volume"] = data["volume"].replace(0.0, np.nan)

    proxy = build_proxy_market_cap(factors, data, rebal_freq="monthly")
    signal = cta_signal(proxy, mode="graduated")  # 0/0.5/1
    signal = signal.loc[signal.index >= pd.Timestamp(REPORT_START)]

    print(f"  CTA 訊號統計：")
    print(f"    全多頭 (1.0): {(signal == 1.0).sum()} 天")
    print(f"    半倉 (0.5):   {(signal == 0.5).sum()} 天")
    print(f"    空手 (0.0):   {(signal == 0.0).sum()} 天")

    # ── (b) 載入既有策略 ──────────────────────────────────
    print("\n📂 步驟 2：載入既有策略淨值...")
    existing_strategies = {
        "L3_ML_Ensemble": "reports/equity_curve_L3_ml.csv",
        "PEAD":           "reports/equity_curve_pead.csv",
        "Pure_ML":        "reports/equity_curve_pure_ml.csv",
    }
    returns_dict = {}
    for name, path in existing_strategies.items():
        ret = load_equity_returns(name, path)
        if not ret.empty:
            ret = ret.loc[ret.index >= pd.Timestamp(REPORT_START)]
            returns_dict[name] = ret
            print(f"  ✅ {name}: {len(ret)} 天")
        else:
            print(f"  ⚠️  {name}: 找不到 {path}")

    # ── (c) 建構單因子 L3 變體 ────────────────────────────
    print("\n🧪 步驟 3：建構單因子 L3 變體（5 個）...")
    close = factors["close"]
    single_factors = ["value", "mom_52w", "low_vol", "rev_mom", "inst_flow"]
    for fname in single_factors:
        try:
            ret = build_single_factor_returns(factors, close, fname)
            returns_dict[f"L3_{fname}"] = ret
            print(f"  ✅ L3_{fname}: {len(ret)} 天，年化 {(1 + ret).prod()**(252/len(ret)) * 100 - 100:+.2f}%")
        except Exception as e:
            print(f"  ⚠️  L3_{fname} 失敗：{e}")

    # ── (d) 整合成 DataFrame ──────────────────────────────
    print("\n🔗 步驟 4：對齊所有策略到共同時間軸...")
    common_idx = None
    for ret in returns_dict.values():
        common_idx = ret.index if common_idx is None else common_idx.intersection(ret.index)
    print(f"  共同期間：{common_idx[0].date()} ~ {common_idx[-1].date()} ({len(common_idx)} 天)")

    returns_df = pd.DataFrame({
        n: r.reindex(common_idx).fillna(0.0)
        for n, r in returns_dict.items()
    })
    signal_aligned = signal.reindex(common_idx).fillna(0.0)

    # ── (e) 跑配對分析 ────────────────────────────────────
    print("\n🔬 步驟 5：跑 Pairing Analysis...")
    results = analyze_pairings(returns_df, signal_aligned, shift_days=1)

    # ── (f) 輸出報告 ──────────────────────────────────────
    print_report(results)

    REPORTS_DIR.mkdir(exist_ok=True)
    PLOTS_DIR.mkdir(exist_ok=True)

    # 儲存 CSV
    csv_path = REPORTS_DIR / "pairing_results.csv"
    results.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  💾 {csv_path}")

    # ── (g) 視覺化 ────────────────────────────────────────
    print("\n📈 步驟 6：產生圖表...")

    # Sharpe bar chart
    sharpe_path = PLOTS_DIR / "sharpe_improvement_bars.png"
    plot_sharpe_bars(results, save_path=str(sharpe_path))
    print(f"  💾 {sharpe_path}")

    # Heatmap
    heat_path = PLOTS_DIR / "metrics_heatmap.png"
    plot_metrics_heatmap(results, save_path=str(heat_path))
    print(f"  💾 {heat_path}")

    # 個別策略對比圖（取前 4 名）
    for name in returns_df.columns:
        ret_before = returns_df[name]
        ret_after = overlay_signal(ret_before, signal_aligned, shift_days=1)
        out_path = PLOTS_DIR / f"overlay_{name}.png"
        plot_overlay_comparison(name, ret_before, ret_after, save_path=str(out_path))
    print(f"  💾 {len(returns_df.columns)} 個 overlay 對比圖 → {PLOTS_DIR}/")

    # ── (h) 最終建議 ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("  🏆 最終搭配建議")
    print("=" * 70)

    # 找出最佳防禦
    best_def = results[results["category"] == "BEST_DEFENSIVE"]
    if not best_def.empty:
        top = best_def.iloc[0]
        print(f"\n  🥇 最佳防禦互補：{top['strategy']}")
        print(f"     CAGR {top['cagr_before']:+.2f}% → {top['cagr_after']:+.2f}%  "
              f"(Δ {top['cagr_delta']:+.2f}%)")
        print(f"     Sharpe {top['sharpe_before']:.3f} → {top['sharpe_after']:.3f}  "
              f"(Δ {top['sharpe_delta']:+.3f})")
        print(f"     MDD {top['mdd_before']:.2f}% → {top['mdd_after']:.2f}%  "
              f"(改善 {top['mdd_improvement']:+.2f}%)")

    # 找出趨勢衝突
    conflicts = results[results["category"] == "CONFLICT"]
    if not conflicts.empty:
        print(f"\n  ⚠️  趨勢衝突的策略（不建議與此 CTA 搭配）：")
        for _, r in conflicts.iterrows():
            print(f"     {r['strategy']}: Sharpe Δ {r['sharpe_delta']:+.3f}")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
