"""
O2 Feature Ablation：一個一個測 ret_5d / ret_60d / vol_60d / log_size

策略：
  1. 載入資料 + 因子（共用）
  2. 對每個 config（baseline + 4 個個別 + 全部）跑 ML pipeline
  3. 比較 CAGR / Sharpe / MDD
  4. 識別贏家（如有）
  5. 自動 sweep 贏家組合

避免 re-load DB 多次（每次 60 秒），共用 factors。
"""
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy import quant_layer3 as L3


# ══════════════════════════════════════════════════════════════
# 1. 各個額外 feature 的計算函式
# ══════════════════════════════════════════════════════════════

def make_extra_feature(name: str, factors: dict) -> pd.DataFrame:
    close = factors["close"]
    liquid = factors["liquid_mask"]

    if name == "ret_5d":
        x = close.pct_change(5).where(liquid)
    elif name == "ret_60d":
        x = close.pct_change(60).where(liquid)
    elif name == "vol_60d":
        x = close.pct_change().rolling(60, min_periods=20).std().where(liquid)
    elif name == "log_size":
        dv = factors["dollar_volume"]
        x = np.log(dv.clip(lower=1)).where(liquid)
    else:
        raise ValueError(f"unknown feature {name}")

    smoothed = x.rolling(L3.SMOOTH_WINDOW,
                          min_periods=max(1, L3.SMOOTH_WINDOW // 2)).mean()
    return L3._industry_neutral_zscore(smoothed).fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 2. 一次完整 pipeline（給定 extras dict）
# ══════════════════════════════════════════════════════════════

def run_one_config(factors: dict, data: dict, extras: dict, label: str) -> dict:
    print(f"\n────── {label} ──────")

    # ML predictions（含 extras）
    ml_pred = L3.build_ml_composite(factors, extra_features=extras if extras else None)

    # Linear composite + ensemble
    linear_comp = L3.build_composite(factors)
    linear_z = L3._cross_zscore(linear_comp).fillna(0.0)
    ml_z = L3._cross_zscore(ml_pred).fillna(0.0)
    composite = 0.5 * linear_z + 0.5 * ml_z
    any_valid = (~linear_comp.isna()) | (~ml_pred.isna())
    composite = composite.where(any_valid, np.nan)

    # Build positions + backtest
    positions = L3.build_positions(factors, composite)
    asset_ret = data["close"].pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross = (positions * asset_ret).sum(axis=1)
    turnover = positions.diff().abs().sum(axis=1)
    cost = turnover * (L3.COMMISSION + L3.SLIPPAGE
                        + L3.COMMISSION + L3.TAX + L3.SLIPPAGE) / 2.0
    net = gross - cost
    mask = net.index >= pd.Timestamp(L3.REPORT_START)
    net_r = net.loc[mask]
    eq = (1 + net_r).cumprod()

    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = net_r.std() * np.sqrt(252)
    sharpe = (cagr - L3.RF_RATE) / vol if vol > 0 else 0
    mdd = (eq / eq.cummax() - 1).min()

    print(f"   CAGR {cagr*100:+6.2f}%  Sharpe {sharpe:+5.3f}  MDD {mdd*100:+6.2f}%")
    return {"label": label, "cagr": cagr, "sharpe": sharpe, "mdd": mdd}


# ══════════════════════════════════════════════════════════════
# 3. Main：跑所有 configs
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  O2 Feature Ablation：個別測試 4 個額外 features")
    print("=" * 70)

    # 載入一次資料
    print("\n📂 載入資料 + 建構因子（一次性，共用給所有 config）...")
    data = L3.load_matrices(L3.DB_PATH, L3.WARMUP_START, L3.END_DATE)
    factors = L3.build_factors(data)

    # 先計算 4 個 extra features（一次性）
    print("\n🧮 預先計算 4 個 extras...")
    extras_pool = {}
    for name in ["ret_5d", "ret_60d", "vol_60d", "log_size"]:
        extras_pool[name] = make_extra_feature(name, factors)
        print(f"   ✅ {name}")

    # ── 跑各配置 ──────────────────────────────────────────
    results = []

    # Baseline (no extras)
    results.append(run_one_config(factors, data, {}, "Baseline (5 base only)"))

    # 個別加入
    for name in extras_pool:
        results.append(run_one_config(
            factors, data, {name: extras_pool[name]}, f"+ {name}"))

    # 全部加入（O2 已知會差，但作為對照）
    results.append(run_one_config(
        factors, data, extras_pool, "+ All 4"))

    # ── 報表 ──────────────────────────────────────────────
    df = pd.DataFrame(results)
    df["cagr"] = df["cagr"] * 100
    df["mdd"] = df["mdd"] * 100

    print("\n" + "=" * 70)
    print("  📊 Ablation 結果排序（按 Sharpe）")
    print("=" * 70)
    df_sorted = df.sort_values("sharpe", ascending=False)

    baseline = df[df["label"] == "Baseline (5 base only)"].iloc[0]
    print(f"\n  baseline: CAGR {baseline['cagr']:+6.2f}%  "
          f"Sharpe {baseline['sharpe']:+5.3f}  MDD {baseline['mdd']:+6.2f}%")
    print("─" * 70)
    print(f"  {'Config':<28} {'CAGR':<10} {'Sharpe':<10} {'ΔSharpe':<10} {'MDD':<10}")
    print("─" * 70)
    for _, r in df_sorted.iterrows():
        d_sharpe = r["sharpe"] - baseline["sharpe"]
        marker = "  ✅" if d_sharpe > 0.005 else ("  ❌" if d_sharpe < -0.01 else "")
        print(f"  {r['label']:<28} {r['cagr']:+7.2f}%  {r['sharpe']:+5.3f}    "
              f"{d_sharpe:+5.3f}    {r['mdd']:+7.2f}%{marker}")

    # 識別贏家
    winners = df[
        (df["sharpe"] > baseline["sharpe"] + 0.005)
        & (df["label"] != "Baseline (5 base only)")
        & (df["label"] != "+ All 4")
    ]
    if not winners.empty:
        print(f"\n  🏆 個別贏家：{', '.join(winners['label'].tolist())}")
        print("     建議下一輪測試這些贏家的組合")
    else:
        print("\n  ⚠️  沒有任何 feature 個別測試能超過 baseline")
        print("     確認最終策略不需要這些 extras")

    df.to_csv("reports/o2_ablation.csv", index=False)
    print(f"\n  💾 reports/o2_ablation.csv")
    print("=" * 70)


if __name__ == "__main__":
    main()
