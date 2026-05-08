"""
O2 組合測試：3 個最有可能成功的 feature 組合
  情境 A: ret_60d + log_size       （最不傷的 2 個）
  情境 B: vol_60d + log_size       （最不重疊的 2 個）
  情境 C: ret_60d + vol_60d + log_size （除掉最差的 ret_5d）

如果三個情境都 ≤ baseline (Sharpe 0.698)，可確認單純的 5 base feature 就是最優。
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy import quant_layer3 as L3
from analysis.o2_ablation import make_extra_feature, run_one_config


def main():
    print("=" * 70)
    print("  O2 組合測試（3 個 scenarios）")
    print("=" * 70)

    # 載入一次（共用）
    print("\n📂 載入資料 + 因子...")
    data = L3.load_matrices(L3.DB_PATH, L3.WARMUP_START, L3.END_DATE)
    factors = L3.build_factors(data)

    # 計算 4 個 extras 一次
    print("\n🧮 預計算 extras...")
    pool = {n: make_extra_feature(n, factors)
            for n in ["ret_5d", "ret_60d", "vol_60d", "log_size"]}

    BASELINE_SHARPE = 0.698
    print(f"\n📊 Baseline Sharpe: {BASELINE_SHARPE:.3f}（從 ablation 結果）")

    scenarios = [
        ("A: ret_60d + log_size",          ["ret_60d", "log_size"]),
        ("B: vol_60d + log_size",          ["vol_60d", "log_size"]),
        ("C: ret_60d + vol_60d + log_size", ["ret_60d", "vol_60d", "log_size"]),
    ]

    results = []
    for label, names in scenarios:
        extras = {n: pool[n] for n in names}
        r = run_one_config(factors, data, extras, label)
        r["delta_sharpe"] = r["sharpe"] - BASELINE_SHARPE
        results.append(r)

    # 報告
    print("\n" + "=" * 70)
    print("  📊 組合結果")
    print("=" * 70)
    df = pd.DataFrame(results)
    df["cagr"] = df["cagr"] * 100
    df["mdd"] = df["mdd"] * 100
    df_sorted = df.sort_values("sharpe", ascending=False)

    print(f"\n  {'Scenario':<35} {'CAGR':<10} {'Sharpe':<10} {'ΔSharpe':<10}")
    print("─" * 70)
    for _, r in df_sorted.iterrows():
        marker = "  ✅ 贏！" if r["delta_sharpe"] > 0.005 else "  ❌"
        print(f"  {r['label']:<35} {r['cagr']:+7.2f}%  {r['sharpe']:+5.3f}    "
              f"{r['delta_sharpe']:+5.3f}{marker}")

    winners = df[df["delta_sharpe"] > 0.005]
    if not winners.empty:
        print(f"\n  🏆 找到贏家！{winners['label'].iloc[0]}")
        print("     建議：將這個組合加入正式策略")
    else:
        print("\n  📌 全數確認：沒有任何組合超過 baseline")
        print("     最終答案：N1 v2 ML 5 base features 為最優（不需 extras）")

    df.to_csv("reports/o2_combinations.csv", index=False)
    print("\n  💾 reports/o2_combinations.csv")
    print("=" * 70)


if __name__ == "__main__":
    main()
