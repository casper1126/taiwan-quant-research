"""
strategy/ablation.py
─────────────────────────────────────────────────────────────
Task 6b：Ablation Study — 四版本同條件對照

四個版本只差在「選股排名依據」與「要不要用機制曝險」，其餘參數
（top_n／rebal_freq／交易成本／回測區間）完全相同，才是乾淨的對照：

  A 固定權重，無機制：use_fixed_weights=True
  B 固定權重＋機制曝險：use_fixed_weights=True + use_regime_exposure=True
  C 動態權重＋機制曝險：（預設動態 IC 加權）+ use_regime_exposure=True
  D ML＋機制曝險：use_ml_composite=True + use_regime_exposure=True

A/B/C/D 都不用 use_regime_factor_weights（Task 4a 的機制動態因子權重）
——這不是任務書 Task 6b 定義的四個版本之一，Task 4a 這個機制仍然存在
於 quant_layer2.py，只是沒有被納入這次 ablation 對照（見
docs/DECISIONS.md「Task 6 ablation D 語意決定」那筆的完整說明）。

B/C/D 共用「同一次」regime_engine.run_regime_engine() 的輸出（機制標籤、
建議曝險比例），確保它們的機制輸入完全一致，差異只來自選股邏輯本身；
D 版的 ML 分數（strategy/ml_composite.py）也重用這同一份機制輸出當
特徵，而不是各自重跑一次（regime_engine 內部的 LightGBM 崩盤預警在
不同次執行間有輕微數值不確定性，重跑兩次會讓 D 版跟 B/C 版看到不完全
一樣的機制序列，見 ml_composite.py 的說明）。

「各機制分段績效」：不管哪個版本，都用同一份機制標籤序列（BULL/
NEUTRAL/WARNING/BEAR）把該版本的每日淨報酬分成四組，各自串接成
子淨值曲線算年化報酬與最大回撤——這樣可以回答「不管這個版本自己有
沒有用機制資訊，它在客觀認定為 BULL 的日子表現如何、在 BEAR 的日子
表現如何」，比只看整體績效更能看出機制對報酬來源的影響。

直接執行：
  python strategy/ablation.py
"""

import sys
import warnings
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import quant_layer2 as q2
import ml_composite as mlc
from regime import regime_engine

REGIME_STATES = ["BULL", "NEUTRAL", "WARNING", "BEAR"]
RF_RATE = q2.RF_RATE


# ══════════════════════════════════════════════════════════════
# 統計輔助（直接用 equity 數列算數字，不解析格式化字串）
# ══════════════════════════════════════════════════════════════

def _numeric_stats(equity: pd.Series) -> Dict[str, float]:
    net_ret = equity.pct_change().fillna(0.0)
    total_ret = equity.iloc[-1] / equity.iloc[0] - 1
    n_active = max((net_ret != 0).sum(), 1)
    ann_ret = (1 + total_ret) ** (252 / n_active) - 1
    ann_vol = net_ret.std() * np.sqrt(252)
    sharpe = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0.0
    mdd = (equity / equity.cummax() - 1).min()
    return {
        "total_return": total_ret,
        "annual_return": ann_ret,
        "annual_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
    }


def _regime_conditional_breakdown(equity: pd.Series, regime_series: pd.Series) -> Dict[str, Dict]:
    """把 equity 的每日淨報酬依 regime_series 的標籤分組，各自串接子淨值曲線，
    算年化報酬／最大回撤／天數。"""
    net_ret = equity.pct_change().fillna(0.0)
    regime_aligned = regime_series.reindex(equity.index)

    breakdown = {}
    for state in REGIME_STATES:
        mask = regime_aligned == state
        n_days = int(mask.sum())
        if n_days < 5:
            breakdown[state] = {"n_days": n_days, "annual_return": None, "max_drawdown": None}
            continue
        sub_ret = net_ret.loc[mask]
        sub_equity = (1 + sub_ret).cumprod()
        total_ret = sub_equity.iloc[-1] - 1
        ann_ret = (1 + total_ret) ** (252 / n_days) - 1
        mdd = (sub_equity / sub_equity.cummax() - 1).min()
        breakdown[state] = {
            "n_days": n_days,
            "annual_return": ann_ret,
            "max_drawdown": mdd,
        }
    return breakdown


# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════

def run_ablation() -> Dict[str, dict]:
    logger.info("ablation：跑一次 regime_engine，B/C/D 三版共用同一份機制輸出...")
    regime_df, extras = regime_engine.run_regime_engine(
        start="2012-05-02", end=q2.END_DATE, run_ml_alert=True,
    )

    logger.info("ablation：跑 ml_composite walk-forward，D 版用（重用上面的機制輸出）...")
    ml_scores, ml_fold_info, _ = mlc.walk_forward_ml_composite(
        regime_df=regime_df, regime_extras=extras,
    )

    versions = {
        "A": dict(use_fixed_weights=True),
        "B": dict(use_fixed_weights=True, regime_df=regime_df, use_regime_exposure=True),
        "C": dict(regime_df=regime_df, use_regime_exposure=True),
        "D": dict(ml_scores=ml_scores, use_ml_composite=True,
                  regime_df=regime_df, use_regime_exposure=True),
    }
    labels = {
        "A": "固定權重，無機制",
        "B": "固定權重＋機制曝險",
        "C": "動態權重＋機制曝險",
        "D": "ML＋機制曝險",
    }

    results = {}
    for key, kwargs in versions.items():
        logger.info(f"ablation：跑版本 {key}（{labels[key]}）...")
        stats, equity, positions = q2.run_pipeline(save_equity_path=None, **kwargs)
        numeric = _numeric_stats(equity)
        breakdown = _regime_conditional_breakdown(equity, regime_df["regime"])
        results[key] = {
            "label": labels[key],
            "stats": stats,
            "numeric": numeric,
            "equity": equity,
            "regime_breakdown": breakdown,
        }
        logger.info(f"ablation {key} 完成：年化 {numeric['annual_return']*100:+.1f}%／"
                   f"Sharpe {numeric['sharpe']:.3f}／MDD {numeric['max_drawdown']*100:.1f}%")

    return results


# ══════════════════════════════════════════════════════════════
# 報告輸出
# ══════════════════════════════════════════════════════════════

def write_ablation_report(results: Dict[str, dict], path: str = "reports/ablation_results.md") -> Dict[str, float]:
    lines = []
    lines.append("# Ablation Study：四版本同條件對照（Task 6b）\n")
    lines.append("同一段回測區間、同樣的 top_n／再平衡頻率／交易成本，只改變「選股排名依據」"
                 "與「要不要用機制曝險」，四個版本才是乾淨的對照：\n")
    lines.append("- **A 固定權重，無機制**：`use_fixed_weights=True`")
    lines.append("- **B 固定權重＋機制曝險**：`use_fixed_weights=True` + `use_regime_exposure=True`")
    lines.append("- **C 動態權重＋機制曝險**：預設動態 IC 加權 + `use_regime_exposure=True`")
    lines.append("- **D ML＋機制曝險**：`use_ml_composite=True` + `use_regime_exposure=True`\n")
    lines.append("B/C/D 共用同一次 `regime_engine.run_regime_engine()` 輸出（同一份機制標籤"
                 "序列），差異只來自選股邏輯本身，不是機制輸入不一致造成的。\n")

    lines.append("## 1. 整體績效對照\n")
    header = "| 版本 | 說明 | 總報酬 | 年化報酬 | 年化波動度 | Sharpe | 最大回撤 | 年化換手率 |"
    sep = "|---|---|---:|---:|---:|---:|---:|---:|"
    lines.append(header)
    lines.append(sep)
    for key in ["A", "B", "C", "D"]:
        r = results[key]
        n = r["numeric"]
        turnover = r["stats"].get("年化換手率", "n/a")
        lines.append(
            f"| {key} | {r['label']} | {n['total_return']*100:+.1f}% "
            f"| {n['annual_return']*100:+.1f}% | {n['annual_vol']*100:.1f}% "
            f"| {n['sharpe']:.3f} | {n['max_drawdown']*100:.1f}% | {turnover} |"
        )
    lines.append("")

    lines.append("## 2. 各機制分段績效（BULL/NEUTRAL/WARNING/BEAR）\n")
    lines.append("每個版本的每日淨報酬依當天客觀認定的機制標籤分組，各自串接成子淨值曲線"
                 "算年化報酬與最大回撤（天數 <5 天的分組不計算，樣本太少沒有意義）。\n")
    for key in ["A", "B", "C", "D"]:
        r = results[key]
        lines.append(f"### 版本 {key}（{r['label']}）\n")
        lines.append("| 機制狀態 | 天數 | 年化報酬 | 最大回撤 |")
        lines.append("|---|---:|---:|---:|")
        for state in REGIME_STATES:
            b = r["regime_breakdown"][state]
            if b["annual_return"] is None:
                lines.append(f"| {state} | {b['n_days']} | n/a（樣本太少） | n/a |")
            else:
                lines.append(f"| {state} | {b['n_days']} | {b['annual_return']*100:+.1f}% "
                             f"| {b['max_drawdown']*100:.1f}% |")
        lines.append("")

    lines.append("## 3. 驗收標準檢查\n")
    a_mdd = results["A"]["numeric"]["max_drawdown"]
    b_mdd = results["B"]["numeric"]["max_drawdown"]
    a_sharpe = results["A"]["numeric"]["sharpe"]
    b_sharpe = results["B"]["numeric"]["sharpe"]

    mdd_improve = (abs(a_mdd) - abs(b_mdd)) / abs(a_mdd) if a_mdd != 0 else 0.0
    mdd_pass = mdd_improve >= 0.25
    sharpe_pass = b_sharpe >= a_sharpe - 0.1

    lines.append(f"- **B 版 MDD 比 A 改善 ≥25%**：A={a_mdd*100:.1f}%，B={b_mdd*100:.1f}%，"
                 f"改善幅度={mdd_improve*100:.1f}% → {'✅ 通過' if mdd_pass else '❌ 未通過'}")
    lines.append(f"- **B 版 Sharpe ≥ A − 0.1**：A={a_sharpe:.3f}，B={b_sharpe:.3f}，"
                 f"門檻={a_sharpe-0.1:.3f} → {'✅ 通過' if sharpe_pass else '❌ 未通過'}")
    lines.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"ablation：報告已存到 {path}")

    return {"mdd_pass": mdd_pass, "sharpe_pass": sharpe_pass,
           "mdd_improve": mdd_improve, "a_mdd": a_mdd, "b_mdd": b_mdd,
           "a_sharpe": a_sharpe, "b_sharpe": b_sharpe}


if __name__ == "__main__":
    results = run_ablation()
    acceptance = write_ablation_report(results)
    print("\n✅ Task 6b Ablation 完成。")
    print(f"   B vs A MDD 改善：{acceptance['mdd_improve']*100:.1f}%"
         f"（門檻 25%，{'通過' if acceptance['mdd_pass'] else '未通過'}）")
    print(f"   B vs A Sharpe：{acceptance['b_sharpe']:.3f} vs 門檻 {acceptance['a_sharpe']-0.1:.3f}"
         f"（{'通過' if acceptance['sharpe_pass'] else '未通過'}）")
