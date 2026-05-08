"""
O3：N1 (L3 ML 60/40) + PEAD overlay 權重 sweep

找最佳 N1 vs PEAD 配置：
  各種 (w_N1, w_PEAD) 組合的 CAGR / Sharpe / MDD
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

RF_RATE = 0.015


def metrics(eq: pd.Series, label: str = "") -> dict:
    rets = eq.pct_change().dropna()
    if len(rets) < 2:
        return {}
    yrs = (rets.index[-1] - rets.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = rets.std() * np.sqrt(252)
    sharpe = (cagr - RF_RATE) / vol if vol > 0 else 0
    mdd = (eq / eq.cummax() - 1).min()
    return {"label": label, "cagr": cagr * 100, "sharpe": sharpe,
            "mdd": mdd * 100, "vol": vol * 100,
            "calmar": cagr / abs(mdd) if mdd != 0 else 0}


def main():
    print("=" * 70)
    print("  O3: N1 (L3 ML) + PEAD Overlay Sweep")
    print("=" * 70)

    # 載入兩個策略的淨值
    n1_eq = pd.read_csv("reports/equity_curve_L3_ml.csv",
                         parse_dates=["date"]).set_index("date")["equity"]
    pead_eq = pd.read_csv("reports/equity_curve_pead.csv",
                          parse_dates=["date"]).set_index("date")["equity"]

    common = n1_eq.index.intersection(pead_eq.index)
    n1_norm   = n1_eq.reindex(common)   / n1_eq.reindex(common).iloc[0]
    pead_norm = pead_eq.reindex(common) / pead_eq.reindex(common).iloc[0]

    ret_n1   = n1_norm.pct_change().fillna(0.0)
    ret_pead = pead_norm.pct_change().fillna(0.0)

    print(f"\n  期間: {common[0].date()} ~ {common[-1].date()}")
    print(f"  N1 vs PEAD 報酬相關性: {ret_n1.corr(ret_pead):.3f}")

    # 個別策略
    print("\n  個別策略：")
    print("  ─" * 35)
    for label, eq in [("N1 (L3 ML)", n1_norm), ("PEAD", pead_norm)]:
        m = metrics(eq, label)
        print(f"    {label:<12} CAGR {m['cagr']:+6.2f}%  "
              f"Sharpe {m['sharpe']:+5.3f}  MDD {m['mdd']:+6.2f}%")

    # Sweep
    print(f"\n  Sweep 結果 (N1 weight 從 100% 到 0%)：")
    print("  ─" * 35)
    print(f"  {'w_N1':<6} {'w_PEAD':<8} {'CAGR':<10} {'Sharpe':<9} {'MDD':<10} {'Calmar':<8}")
    print("  " + "─" * 60)

    rows = []
    best_sharpe = (0, None)
    best_calmar = (0, None)

    for w_n1 in np.arange(1.0, -0.01, -0.05):
        w_pead = 1.0 - w_n1
        blend = (1 + w_n1 * ret_n1 + w_pead * ret_pead).cumprod()
        m = metrics(blend, f"N1 {w_n1:.0%} + PEAD {w_pead:.0%}")
        marker = ""
        if m["sharpe"] > best_sharpe[0]:
            best_sharpe = (m["sharpe"], dict(w_n1=w_n1, w_pead=w_pead, **m))
            marker = "  ⭐ Sharpe"
        if m["calmar"] > best_calmar[0]:
            best_calmar = (m["calmar"], dict(w_n1=w_n1, w_pead=w_pead, **m))
            marker += "  🛡️ Calmar"
        print(f"  {w_n1:<6.2f} {w_pead:<8.2f} {m['cagr']:+7.2f}%  "
              f"{m['sharpe']:+5.3f}    {m['mdd']:+7.2f}%  {m['calmar']:+5.3f}{marker}")
        rows.append({**m, "w_n1": w_n1, "w_pead": w_pead})

    print("\n  🏆 最佳建議：")
    print("  ─" * 35)
    bs = best_sharpe[1]
    print(f"    🥇 最高 Sharpe：N1 {bs['w_n1']:.0%} + PEAD {bs['w_pead']:.0%}")
    print(f"        CAGR {bs['cagr']:+.2f}%  Sharpe {bs['sharpe']:.3f}  MDD {bs['mdd']:+.2f}%")
    bc = best_calmar[1]
    print(f"    🛡️  最高 Calmar：N1 {bc['w_n1']:.0%} + PEAD {bc['w_pead']:.0%}")
    print(f"        CAGR {bc['cagr']:+.2f}%  Sharpe {bc['sharpe']:.3f}  MDD {bc['mdd']:+.2f}%")

    # 儲存最佳組合
    best_w_n1 = bs["w_n1"]
    best_w_pead = bs["w_pead"]
    best_blend = (1 + best_w_n1 * ret_n1 + best_w_pead * ret_pead).cumprod()
    best_blend.to_frame("equity").to_csv("reports/equity_curve_n1_pead_best.csv")

    df = pd.DataFrame(rows)
    df.to_csv("reports/n1_pead_sweep.csv", index=False)

    print(f"\n  💾 reports/equity_curve_n1_pead_best.csv（最佳 Sharpe 組合）")
    print(f"  💾 reports/n1_pead_sweep.csv（{len(df)} 個權重組合）")
    print("=" * 70)


if __name__ == "__main__":
    main()
