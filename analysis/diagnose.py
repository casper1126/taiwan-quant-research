"""
diagnose.py — 一鍵診斷工具

用法：
  python analysis/diagnose.py                  # 比較 L2 vs L3
  python analysis/diagnose.py reports/xxx.csv  # 單一檔案診斷
"""
import sys
import pandas as pd
import numpy as np
from pathlib import Path


def metrics(eq: pd.Series, label: str = "") -> dict:
    eq = eq.dropna()
    eq = eq[eq > 0]
    if len(eq) < 2:
        return {}
    rets = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    vol = rets.std() * np.sqrt(252)
    sharpe = (rets.mean() * 252 - 0.015) / vol if vol > 0 else 0
    dd_curve = eq / eq.cummax() - 1
    mdd = dd_curve.min()
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    win_rate = (rets > 0).mean()
    return {
        "label": label,
        "起始": eq.index[0].date(),
        "結束": eq.index[-1].date(),
        "年數": round(years, 2),
        "總報酬": f"{(eq.iloc[-1] / eq.iloc[0] - 1) * 100:+.2f}%",
        "年化報酬": f"{cagr * 100:+.2f}%",
        "年波動度": f"{vol * 100:.2f}%",
        "Sharpe": f"{sharpe:.3f}",
        "最大回撤": f"{mdd * 100:+.2f}%",
        "Calmar": f"{calmar:.2f}",
        "日勝率": f"{win_rate * 100:.1f}%",
    }


def yearly_table(curves: dict) -> pd.DataFrame:
    """並排顯示每年報酬。"""
    out = {}
    for label, eq in curves.items():
        yearly = eq.resample("YE").last().pct_change()
        out[label] = (yearly * 100).round(2)
    df = pd.DataFrame(out)
    df.index = df.index.year
    return df


def print_block(stats: dict) -> None:
    if not stats:
        return
    print(f"\n{'═' * 50}")
    print(f"  {stats.pop('label', ''):<46}")
    print(f"{'═' * 50}")
    for k, v in stats.items():
        print(f"  {k:<10} {v}")


def health_check(stats: dict) -> None:
    cagr_v = float(stats["年化報酬"].rstrip("%"))
    sharpe_v = float(stats["Sharpe"])
    mdd_v = float(stats["最大回撤"].rstrip("%"))
    print("\n  🩺 健康診斷")
    print(f"    年化 > 10%   : {'✅' if cagr_v > 10 else '❌'}  ({cagr_v:+.2f}%)")
    print(f"    Sharpe > 1.0 : {'✅' if sharpe_v > 1.0 else '❌'}  ({sharpe_v:.2f})")
    print(f"    MDD > -25%   : {'✅' if mdd_v > -25 else '❌'}  ({mdd_v:+.2f}%)")


def main():
    paths = sys.argv[1:] or [
        "reports/equity_curve.csv",
        "reports/equity_curve_L3.csv",
    ]
    labels = ["Layer 2 (4 因子)", "Layer 3 (5 因子 RP)"]

    curves = {}
    for path, label in zip(paths, labels):
        if not Path(path).exists():
            print(f"⚠️  找不到 {path}，跳過")
            continue
        df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
        eq = df["equity"]
        curves[label] = eq
        stats = metrics(eq, label)
        print_block(dict(stats))
        health_check(stats)

    if len(curves) >= 2:
        print(f"\n{'═' * 50}")
        print("  📅 年度報酬比較")
        print(f"{'═' * 50}")
        print(yearly_table(curves).to_string())


if __name__ == "__main__":
    main()
