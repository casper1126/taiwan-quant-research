"""
walk_forward_l3.py
─────────────────────────────────────────────────────────────
Layer 3 專用 — Anchored Walk-Forward Out-of-Sample 驗證

設計差異 vs 舊 walk_forward.py：
  舊版（Layer 2）：用訓練集 ICIR 動態調整因子權重
                  → 必須做 walk-forward 防止 IC 估計時的 look-ahead

  Layer 3：靜態因子權重（不從資料學）
          → 沒有「擬合」可言，但仍需驗證每年 OOS 績效穩定
          → Anchored Walk-Forward：第 Y 年的回測只用 Y 年底之前的資料

驗證流程：
  for Y in 2017..2025:
      1. END_DATE = f"{Y}-12-31"
      2. 載入 [WARMUP_START, Y-12-31] 的所有資料
      3. 重新建構因子、複合分數、部位
      4. 取 Y 年的淨值與報酬
      5. 紀錄 Y 年的績效

驗收標準：
  ✅ 各年 OOS 報酬與 in-sample 全期回測接近 → 無 look-ahead bias
  ✅ 各年績效不極端發散 → 策略穩健
  ✅ 整體 OOS 年化報酬 ≥ 全期回測（無正向偏差）→ 沒有 overfit

輸出：
  reports/walk_forward_l3.json  — 每年指標
  reports/walk_forward_l3.md    — 表格報告

直接執行：
  python strategy/walk_forward_l3.py
"""
import json
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START,
    REPORT_START, COMMISSION, TAX, SLIPPAGE, RF_RATE,
    load_matrices, build_factors, build_composite, build_ml_composite,
    build_positions, _cross_zscore,
)

FIRST_TEST_YEAR = 2017      # 從 2017 起測（前兩年因子暖機）
LAST_TEST_YEAR  = 2025      # 2026 還沒結束，跳過

# ══════════════════════════════════════════════════════════════
# 單年 OOS 回測
# ══════════════════════════════════════════════════════════════

def backtest_year(year: int, use_ml: bool = False) -> Tuple[Dict, pd.Series]:
    """
    在「截至 year-12-31」的資料上跑 Layer 3，取 year 那年的績效。
    這保證 year 的所有部位決策都是 OOS（沒看到 year+1 的資料）。

    use_ml=True 時用 ML ensemble（與 quant_layer3.py 主程式一致）。
    """
    end_date = f"{year}-12-31"
    mode_str = "ML Ensemble" if use_ml else "線性"
    print(f"\n── Fold {year}（資料截至 {end_date}，{mode_str}）──")

    # Step 1: 只載入到 year-12-31 的資料
    data = load_matrices(DB_PATH, WARMUP_START, end_date)

    # Step 2: 建構因子
    factors = build_factors(data)

    # Step 3: 合成（線性 / ML ensemble）
    if use_ml:
        linear_comp = build_composite(factors)
        linear_z    = _cross_zscore(linear_comp).fillna(0.0)
        ml_pred     = build_ml_composite(factors)
        ml_z        = _cross_zscore(ml_pred).fillna(0.0)
        composite   = 0.5 * linear_z + 0.5 * ml_z
        any_valid   = (~linear_comp.isna()) | (~ml_pred.isna())
        composite   = composite.where(any_valid, np.nan)
    else:
        composite = build_composite(factors)

    positions = build_positions(factors, composite)

    # Step 3: 抽出 year 那年回測
    close       = data["close"]
    asset_ret   = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross_ret   = (positions * asset_ret).sum(axis=1)
    turnover    = positions.diff().abs().sum(axis=1)
    cost        = turnover * (COMMISSION + SLIPPAGE + COMMISSION + TAX + SLIPPAGE) / 2.0
    net_ret     = gross_ret - cost

    # 只取 year 範圍
    year_mask  = (net_ret.index >= f"{year}-01-01") & (net_ret.index <= end_date)
    year_ret   = net_ret.loc[year_mask]
    year_eq    = (1 + year_ret).cumprod()

    if len(year_eq) == 0:
        return {"year": year}, pd.Series(dtype=float)

    total_ret  = year_eq.iloc[-1] - 1
    n_days     = max((year_ret != 0).sum(), 1)
    ann_ret    = (1 + total_ret) ** (252 / n_days) - 1
    ann_vol    = year_ret.std() * np.sqrt(252)
    sharpe     = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0.0
    mdd        = (year_eq / year_eq.cummax() - 1).min()
    avg_expo   = positions.loc[year_mask].sum(axis=1).mean()

    stats = {
        "year":           year,
        "annual_return":  round(total_ret * 100, 2),
        "annual_vol":     round(ann_vol * 100, 2),
        "sharpe":         round(sharpe, 3),
        "max_drawdown":   round(mdd * 100, 2),
        "avg_exposure":   round(avg_expo * 100, 1),
    }
    print(f"    年報酬     : {stats['annual_return']:+.2f}%")
    print(f"    Sharpe     : {stats['sharpe']:.3f}")
    print(f"    MDD        : {stats['max_drawdown']:.2f}%")
    print(f"    平均曝險   : {stats['avg_exposure']:.1f}%")
    return stats, year_eq


# ══════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════

def walk_forward_l3(use_ml: bool = False) -> None:
    mode_str = "ML Ensemble" if use_ml else "線性合成"
    print("\n" + "=" * 60)
    print(f"  Layer 3 Anchored Walk-Forward OOS 驗證（{mode_str}）")
    print(f"  測試年度：{FIRST_TEST_YEAR}–{LAST_TEST_YEAR}")
    print("=" * 60)

    fold_stats: List[Dict] = []
    oos_curves: List[pd.Series] = []

    for year in range(FIRST_TEST_YEAR, LAST_TEST_YEAR + 1):
        try:
            stats, eq = backtest_year(year, use_ml=use_ml)
            if eq.empty:
                continue
            fold_stats.append(stats)
            oos_curves.append(eq)
        except Exception as e:
            print(f"  ⚠️  Fold {year} 失敗：{e}")

    # ── 整體 OOS（串接每年）──────────────────────────────────
    print("\n" + "=" * 60)
    if oos_curves:
        full = oos_curves[0].copy()
        for nxt in oos_curves[1:]:
            full = pd.concat([full, nxt * full.iloc[-1]])
        full = full[~full.index.duplicated(keep="last")]

        total_ret = full.iloc[-1] - 1
        years     = (full.index[-1] - full.index[0]).days / 365.25
        cagr      = (1 + total_ret) ** (1 / years) - 1
        rets      = full.pct_change().dropna()
        vol       = rets.std() * np.sqrt(252)
        sharpe    = (rets.mean() * 252 - RF_RATE) / vol if vol > 0 else 0.0
        mdd       = (full / full.cummax() - 1).min()

        overall = {
            "cagr":         round(cagr * 100, 2),
            "annual_vol":   round(vol * 100, 2),
            "sharpe":       round(sharpe, 3),
            "max_drawdown": round(mdd * 100, 2),
            "n_years":      round(years, 2),
        }

        print("\n  📊 整體 OOS 績效")
        print(f"    回測年數    : {overall['n_years']:.2f}")
        print(f"    CAGR        : {overall['cagr']:+.2f}%")
        print(f"    年化波動    : {overall['annual_vol']:.2f}%")
        print(f"    Sharpe      : {overall['sharpe']:.3f}")
        print(f"    最大回撤    : {overall['max_drawdown']:.2f}%")

        # 年度報酬一致性
        yearly_rets = [s["annual_return"] for s in fold_stats]
        print(f"\n  📈 年度報酬一致性")
        print(f"    最佳年:    {max(yearly_rets):+.2f}%")
        print(f"    最差年:    {min(yearly_rets):+.2f}%")
        print(f"    平均:      {np.mean(yearly_rets):+.2f}%")
        print(f"    標準差:    {np.std(yearly_rets):.2f}%")
        print(f"    正報酬年: {sum(1 for r in yearly_rets if r > 0)} / {len(yearly_rets)}")
    else:
        overall = {}

    # ── 儲存結果（ML/線性 各自寫一份）───────────────────────
    Path("reports").mkdir(exist_ok=True)
    suffix = "_ml" if use_ml else ""
    json_path = f"reports/walk_forward_l3{suffix}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"folds": fold_stats, "overall": overall},
                  f, ensure_ascii=False, indent=2)

    md_path = f"reports/walk_forward_l3{suffix}.md"
    _write_md(fold_stats, overall, md_path)

    print(f"\n  💾 結果：{json_path}")
    print(f"  💾 報告：{md_path}")
    print("=" * 60 + "\n")


def _write_md(folds: List[Dict], overall: Dict, path: str) -> None:
    lines = [
        "# Layer 3 Walk-Forward OOS Validation",
        "",
        "**策略**：Layer 3（5 因子 + 風險平價 + 4 級擇時 + 產業 cap）",
        "**因子**：Value 0.30 / Mom_52w 0.20 / Low_Vol 0.20 / Rev_Mom 0.20 / Inst_Flow 0.10",
        f"**OOS 期間**：{FIRST_TEST_YEAR}–{LAST_TEST_YEAR}",
        "",
        "## 各年 OOS 績效",
        "",
        "| Year | Return | Sharpe | MDD | Avg Exposure |",
        "|:----:|:------:|:------:|:---:|:------------:|",
    ]
    for s in folds:
        lines.append(
            f"| {s['year']} | {s['annual_return']:+.2f}% | "
            f"{s['sharpe']:.3f} | {s['max_drawdown']:.2f}% | "
            f"{s['avg_exposure']:.1f}% |"
        )
    if overall:
        lines += [
            "",
            "## 整體 OOS",
            "",
            "| CAGR | Vol | Sharpe | MDD |",
            "|:----:|:---:|:------:|:---:|",
            f"| {overall['cagr']:+.2f}% | {overall['annual_vol']:.2f}% | "
            f"{overall['sharpe']:.3f} | {overall['max_drawdown']:.2f}% |",
        ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--ml", action="store_true", help="使用 ML ensemble（C-1）")
    args = p.parse_args()
    walk_forward_l3(use_ml=args.ml)
