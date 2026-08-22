"""
strategy/walk_forward.py
─────────────────────────────────────────────────────────────
Task 6a：Walk-Forward Out-of-Sample 驗證

設計邏輯：
  quant_layer2.py 的「動態 IC 加權」複合分數，權重是用全樣本的滾動 IC
  算出來的——這證明了因子在全樣本裡有效，但沒有回答「如果我只看得到
  過去的資料，未來的權重猜得準不準」這個更嚴格的問題。Walk-Forward
  驗證的核心正是要回答這個問題：
    1. 只用訓練期（[2015-01-01, 該年 12/31 前]）的資料，計算 ICIR
       （IC 均值 / IC 標準差）比例當作因子權重——這一步「看不到」
       測試年及之後的任何資料
    2. 把這組凍結的權重原封不動套用到「下一年」（測試年），不再
       用測試年的資料重新調整權重
    3. 把每一年的測試期（OOS）績效串接起來 = 真實的樣本外績效

跟 quant_layer2.py 的關係：因子的數學定義（momentum_52w／
value_composite／low_vol_ivol／rev_yoy）直接呼叫 strategy/factors/
模組與 quant_layer2.build_rev_yoy()，不在這裡重新手刻一份公式——
Task 2 已經把因子邏輯的單一事實來源放在 factors/，這裡重複定義只會
造成兩份公式後續各自演化、悄悄產生分歧的風險。複合因子只用四個核心
因子（momentum/value/rev_yoy/low_vol），跟 quant_layer2.py 目前的
生產環境複合權重一致——不含 inst_flow／margin_usage（Task 2 已排查
確認這兩個因子在線性方法下沒有穩定訊號，正式移出主策略複合，見
docs/DECISIONS.md／reports/factor_negative_findings.md），Walk-Forward
驗證的對象應該是「目前實際在用的策略」，不是一個任務書字面規格但
已知較弱的舊版本。

窗口設定（Expanding Window，訓練起點固定在 2015-01-01）：
  訓練 2015–2019 → 測試 2020
  訓練 2015–2020 → 測試 2021
  訓練 2015–2021 → 測試 2022
  訓練 2015–2022 → 測試 2023
  訓練 2015–2023 → 測試 2024
  訓練 2015–2024 → 測試 2025

直接執行：
  python strategy/walk_forward.py

或透過 run.py：
  python run.py --step 3
"""

import sys
import json
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import quant_layer2 as q2
from factors import base as factor_base
from factors import style as factor_style

# ── 設定 ──────────────────────────────────────────────────────
DB_PATH        = q2.DB_PATH
TRAIN_START    = "2015-01-01"    # 訓練集起點（固定）
FIRST_TEST_YR  = 2020            # 第一個測試年份
LAST_TEST_YR   = 2025            # 最後一個測試年份

TOP_N          = q2.TOP_N
REBAL_FREQ     = q2.REBAL_FREQ
BUFFER_MULT    = 1.5
COMMISSION     = q2.COMMISSION
TAX            = q2.TAX
SLIPPAGE       = q2.SLIPPAGE
RF_RATE        = q2.RF_RATE

CORE_FACTORS = ["momentum", "value", "rev_yoy", "low_vol"]


# ══════════════════════════════════════════════════════════════
# 因子建構（單一窗口，重用 factors/ 模組，不重新手刻公式）
# ══════════════════════════════════════════════════════════════

def build_factors_slice(data: Dict[str, pd.DataFrame],
                        start: str, end: str) -> Dict[str, pd.DataFrame]:
    """在指定日期範圍內建構四個核心因子矩陣（跟 quant_layer2.py 生產環境一致）。"""
    close = data["close"].replace(0.0, np.nan).loc[start:end]
    volume = data["volume"].replace(0.0, np.nan).loc[start:end]
    PER = data["PER"].loc[start:end]

    slice_data = {**data, "close": close, "volume": volume, "PER": PER}

    dv_rank = (volume * close).rolling(252, min_periods=60).mean().rank(axis=1, ascending=False)
    liquid = dv_rank <= 300

    close_l = close.where(liquid, np.nan)
    PER_l = PER.where(liquid, np.nan)

    momentum = factor_style.momentum_52w(slice_data).where(liquid, np.nan)
    value = factor_style.value_composite(slice_data).where(liquid, np.nan)
    low_vol = factor_style.low_vol_ivol(slice_data).where(liquid, np.nan)
    rev_yoy = q2.build_rev_yoy(data["revenue"], close.index).reindex(
        columns=close.columns).where(liquid, np.nan)

    bias = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    return {
        "momentum": momentum,
        "value": value,
        "rev_yoy": rev_yoy,
        "low_vol": low_vol,
        "bias": bias,
        "PER": PER_l,
        "close": close,
        "liquid": liquid,
    }


# ══════════════════════════════════════════════════════════════
# ICIR 加權（訓練期估計，凍結後套用到測試期）
# ══════════════════════════════════════════════════════════════

def icir_weights(train_factors: Dict[str, pd.DataFrame]) -> Dict[str, float]:
    """
    用訓練集的 ICIR（IC 均值 / IC 標準差）比例計算各因子權重。
    ICIR 越高代表因子在訓練期越穩定；負 ICIR 不給權重（避免反向訊號
    被當成正權重使用）。全部因子 ICIR ≤ 0 時 fallback 回等權。
    """
    close = train_factors["close"]
    fwd_ret = close.pct_change(20).shift(-20)

    icir_map: Dict[str, float] = {}
    for name in CORE_FACTORS:
        mat = train_factors[name]
        if mat.isna().all().all():
            continue
        ic = q2.compute_ic(mat, fwd_ret)
        if ic.empty or ic.std() == 0:
            continue
        icir_map[name] = max(ic.mean() / ic.std(), 0.0)

    total = sum(icir_map.values())
    if total == 0:
        n = len(CORE_FACTORS)
        return {k: 1.0 / n for k in CORE_FACTORS}
    return {k: v / total for k, v in icir_map.items()}


def build_composite(factors: Dict[str, pd.DataFrame],
                    weights: Dict[str, float]) -> pd.DataFrame:
    """用凍結的訓練期權重合成複合因子分數（橫截面 z-score 後加權）。"""
    close = factors["close"]
    cross_z = factor_base.cross_zscore

    composite = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for name, w in weights.items():
        if name in factors and w > 0:
            composite = composite.add(cross_z(factors[name]) * w, fill_value=0.0)

    valid = (factors["bias"].abs() < 0.10) & factors["liquid"]
    return composite.where(valid, np.nan)


# ══════════════════════════════════════════════════════════════
# 部位建構（OOS 用，跟 quant_layer2.py 的緩衝區規則一致但不含機制/ML）
# ══════════════════════════════════════════════════════════════

def build_positions_oos(masked_composite: pd.DataFrame,
                        close: pd.DataFrame,
                        top_n: int = TOP_N,
                        rebal_freq: int = REBAL_FREQ,
                        buffer_mult: float = BUFFER_MULT) -> pd.DataFrame:
    """簡化版建倉（OOS 用，不含大盤擇時，隔離「因子權重猜得準不準」這個
    Walk-Forward 唯一想驗證的變因，避免跟擇時效果混在一起）。"""
    import portfolio as portfolio_module

    cols = close.columns
    rebal_idx = np.where(np.arange(len(close)) % rebal_freq == 0)[0]
    eq_w = 1.0 / top_n
    current_holds: set = set()
    pos = pd.DataFrame(0.0, index=close.index, columns=cols)

    for day_idx in rebal_idx:
        today = close.index[day_idx]
        rank_today = masked_composite.loc[today].rank(ascending=False)
        current_holds = portfolio_module.apply_buffer(
            rank_today, current_holds, top_n=top_n, buffer_multiplier=buffer_mult,
        )
        if current_holds:
            pos.loc[today, list(current_holds)] = eq_w

    anchor = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor.iloc[rebal_idx] = pos.iloc[rebal_idx].values
    return anchor.ffill().fillna(0.0).shift(1).fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 回測
# ══════════════════════════════════════════════════════════════

def backtest_slice(close: pd.DataFrame, position: pd.DataFrame) -> Tuple[dict, pd.Series]:
    """輕量向量化回測，回傳績效指標 + 淨值序列（跟 quant_layer2.run_backtest 同一套公式）。"""
    asset_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0)
    gross = (position * asset_ret).sum(axis=1)
    turnover = position.diff().abs().sum(axis=1)
    cost = turnover * (COMMISSION + SLIPPAGE + (COMMISSION + TAX + SLIPPAGE)) / 2
    net_ret = gross - cost
    equity = (1 + net_ret).cumprod()

    total_ret = equity.iloc[-1] - 1
    n_active = max((net_ret != 0).sum(), 1)
    ann_ret = (1 + total_ret) ** (252 / n_active) - 1
    ann_vol = net_ret.std() * np.sqrt(252)
    sharpe = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0.0
    mdd = (equity / equity.cummax() - 1).min()
    ann_to = turnover.mean() * 252 * 2

    stats = {
        "annual_return": round(ann_ret * 100, 2),
        "annual_vol": round(ann_vol * 100, 2),
        "sharpe": round(sharpe, 3),
        "max_drawdown": round(mdd * 100, 2),
        "annual_turnover": round(ann_to * 100, 1),
        "total_return": round(total_ret * 100, 2),
    }
    return stats, net_ret


# ══════════════════════════════════════════════════════════════
# Walk-Forward 主程式
# ══════════════════════════════════════════════════════════════

def walk_forward(db_path: str = DB_PATH,
                 first_test_yr: int = FIRST_TEST_YR,
                 last_test_yr: int = LAST_TEST_YR) -> Tuple[List[dict], dict, pd.Series]:
    print("\n" + "=" * 60)
    print("  Walk-Forward Out-of-Sample 驗證（Task 6a）")
    print("=" * 60)

    print("📂 載入全部資料...")
    all_data = q2.load_matrices(db_path, TRAIN_START, f"{last_test_yr}-12-31")

    folds = range(first_test_yr, last_test_yr + 1)
    fold_stats = []
    oos_net_ret_parts: List[pd.Series] = []

    for test_year in folds:
        train_end = f"{test_year - 1}-12-31"
        test_start = f"{test_year}-01-01"
        test_end = f"{test_year}-12-31"

        print(f"\n── Fold {test_year} "
              f"（訓練 {TRAIN_START}~{train_end}｜測試 {test_start}~{test_end}）──")

        # ── 訓練：只用訓練期資料計算 ICIR 權重（凍結，不再看測試年）──
        train_factors = build_factors_slice(all_data, TRAIN_START, train_end)
        weights = icir_weights(train_factors)
        weight_str = "  ".join(f"{k}={v:.3f}" for k, v in weights.items())
        print(f"  因子權重（訓練期 ICIR 比例，凍結套用到測試年）：{weight_str}")

        # ── 測試：用凍結權重在 OOS 期間建倉回測 ─────────────────
        # 因子建構需要前期資料做暖機（252 天動能/60 天波動），從前一年
        # 初開始拉一段 warm-up，只統計測試年當年的績效。
        warmup_start = f"{test_year - 1}-01-01"
        test_factors = build_factors_slice(all_data, warmup_start, test_end)

        composite_full = build_composite(test_factors, weights)
        close_full = test_factors["close"]

        position_full = build_positions_oos(composite_full, close_full)
        close_oos = close_full.loc[test_start:test_end]
        position_oos = position_full.loc[test_start:test_end].reindex(
            columns=close_oos.columns, fill_value=0.0)

        stats, net_ret = backtest_slice(close_oos, position_oos)
        stats["year"] = test_year
        fold_stats.append(stats)
        oos_net_ret_parts.append(net_ret)

        print(f"  OOS 年化報酬 : {stats['annual_return']:+.1f}%")
        print(f"  OOS Sharpe   : {stats['sharpe']:.3f}")
        print(f"  OOS 最大回撤 : {stats['max_drawdown']:.1f}%")
        print(f"  OOS 換手率   : {stats['annual_turnover']:.1f}%")

    # ── 整體 OOS 績效（逐日報酬串接，複利連續計算，不是每年獨立重置）──
    print("\n" + "=" * 60)
    if oos_net_ret_parts:
        oos_net_ret = pd.concat(oos_net_ret_parts).sort_index()
        oos_equity = (1 + oos_net_ret).cumprod()

        total_ret = oos_equity.iloc[-1] - 1
        n_days = max((oos_net_ret != 0).sum(), 1)
        ann_ret = (1 + total_ret) ** (252 / n_days) - 1
        ann_vol = oos_net_ret.std() * np.sqrt(252)
        sharpe_all = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0.0
        mdd_all = (oos_equity / oos_equity.cummax() - 1).min()
        n_positive_years = sum(1 for s in fold_stats if s["annual_return"] > 0)

        overall = {
            "total_return": round(total_ret * 100, 2),
            "annual_return": round(ann_ret * 100, 2),
            "sharpe": round(sharpe_all, 3),
            "max_drawdown": round(mdd_all * 100, 2),
            "n_positive_years": n_positive_years,
            "n_folds": len(fold_stats),
        }

        print("  Walk-Forward 整體 OOS 績效（逐日串接）")
        print(f"  總報酬    : {overall['total_return']:+.1f}%")
        print(f"  年化報酬  : {overall['annual_return']:+.1f}%")
        print(f"  Sharpe    : {overall['sharpe']:.3f}")
        print(f"  最大回撤  : {overall['max_drawdown']:.1f}%")
        print(f"  正報酬年數 : {n_positive_years}/{len(fold_stats)}")
    else:
        overall = {}
        oos_net_ret = pd.Series(dtype=float)

    # ── 儲存結果 ───────────────────────────────────────────────
    Path("reports").mkdir(exist_ok=True)

    result = {"folds": fold_stats, "overall": overall}
    json_path = "reports/walk_forward_results.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    md_path = "reports/walk_forward_results.md"
    _write_md_report(fold_stats, overall, md_path)

    print(f"\n  💾 結果已儲存：{json_path}")
    print(f"  💾 報告已儲存：{md_path}")
    print("=" * 60 + "\n")

    return fold_stats, overall, oos_net_ret


def _write_md_report(fold_stats: list, overall: dict, path: str) -> None:
    lines = [
        "# Walk-Forward Out-of-Sample 驗證（Task 6a）",
        "",
        "## 閱讀指南：IS（樣本內）vs OOS（樣本外）是什麼意思",
        "",
        "`reports/equity_curve.csv`（`python run.py --step 2` 的輸出）是**樣本內（In-Sample,",
        "IS）**回測：因子權重是用全樣本（含測試期本身）的滾動 IC 算出來的，等於「用未來",
        "已經發生的結果，去驗證這個方法在過去有沒有用」——這種回測結構性地偏樂觀，因為",
        "任何策略多少都會對它看過的資料「合身」。",
        "",
        "這份報告是**樣本外（Out-of-Sample, OOS）**驗證：每一個 fold 的因子權重只用",
        "「測試年之前」的資料算出來，算完就凍結，完全不再用測試年（或更之後）的任何資料",
        "去調整——測試年的績效是模型「沒看過」這段資料、純粹用過去學到的權重去賭出來的",
        "結果。OOS 數字通常會比 IS 差（這是正常且健康的現象，代表沒有嚴重過擬合）；如果",
        "OOS 數字反而比 IS 好，反而要懷疑是不是哪裡的因子計算不小心洩漏了未來資訊。",
        "",
        "**這裡驗證的是目前 quant_layer2.py 生產環境實際在用的策略**（動態 IC 加權、",
        "四個核心因子 momentum/value/rev_yoy/low_vol，不含 inst_flow/margin_usage——",
        "這兩個因子已經在 Task 2 被排查確認沒有穩定訊號，見",
        "`reports/factor_negative_findings.md`），不含 Task 3-5 的機制/ML 功能——",
        "那些版本的同條件對照在 `reports/ablation_results.md`（Task 6b）。",
        "",
        "**Strategy**: Taiwan Multi-Factor (Momentum / Value / Revenue YoY / Low-Vol)",
        f"**Training start**: {TRAIN_START} (expanding window)",
        f"**OOS period**: {FIRST_TEST_YR}–{LAST_TEST_YR}",
        "",
        "## Per-Year OOS Results",
        "",
        "| Year | Annual Return | Sharpe | Max Drawdown | Turnover |",
        "|------|:-------------:|:------:|:------------:|:--------:|",
    ]
    for s in fold_stats:
        lines.append(
            f"| {s['year']} "
            f"| {s['annual_return']:+.1f}% "
            f"| {s['sharpe']:.3f} "
            f"| {s['max_drawdown']:.1f}% "
            f"| {s['annual_turnover']:.0f}% |"
        )

    if overall:
        lines += [
            "",
            "## Overall OOS Performance（逐日串接，非每年獨立複利重置）",
            "",
            "| Total Return | Annual Return | Sharpe | Max Drawdown | 正報酬年數 |",
            "|:------------:|:-------------:|:------:|:------------:|:----------:|",
            f"| {overall['total_return']:+.1f}% "
            f"| {overall['annual_return']:+.1f}% "
            f"| {overall['sharpe']:.3f} "
            f"| {overall['max_drawdown']:.1f}% "
            f"| {overall['n_positive_years']}/{overall['n_folds']} |",
        ]

    lines += [
        "",
        "> OOS results use ICIR-proportional factor weights estimated on the training window",
        "> only（該年之前的資料），frozen and applied unchanged to the test year — this is what",
        "> makes it a genuine walk-forward test rather than a re-fit-every-day rolling backtest.",
        "> Each fold's weights are recalculated independently from scratch to avoid look-ahead bias.",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    walk_forward()
