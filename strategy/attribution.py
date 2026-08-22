"""
strategy/attribution.py
─────────────────────────────────────────────────────────────
Task 6d：績效歸因

把 Task 2 基準策略（固定權重版，見下方「為什麼用固定權重不用動態 IC」）
的總報酬拆解成：

    總報酬 = Σ 各因子邊際貢獻（單因子版回測比較）+ 擇時貢獻 − 成本 + 殘差

每一項都是用真實回測算出來的數字（GROSS，也就是還沒扣成本的毛報酬），
不是估計值：

  1. **各因子邊際貢獻**：對 momentum/value/rev_yoy/low_vol 各自跑一個
     「只用這個因子」的單因子版回測（其他因子權重設 0），乘上它在
     完整複合裡的實際權重，就是這個因子對總報酬的邊際貢獻。
  2. **擇時貢獻**：完整版（有大盤擇時開關）的毛報酬，減去「同一組
     選股邏輯，但關掉擇時（永遠視為多頭、允許買進）」版本的毛報酬。
  3. **成本**：完整版的毛報酬減淨報酬（交易成本 + 稅 + 滑價的總拖累）。
  4. **殘差**：因子分數用線性加權合成、再統一排名選股，這個過程本身
     不是「各因子單獨選股報酬的線性組合」——緩衝區、inertia 平滑、
     排名的非線性都會讓合成版跟單因子版簡單加權平均有落差，這個落差
     就是殘差。

這個分解是**設計成恆等式**（見 `_decompose()` 的推導註解），不是四個
獨立估計湊出來的近似值——每一項都用同一組真實回測的毛報酬互相相減
定義出來，四項加總保證等於總報酬，不會有「兜不起來」的誤差需要藏起來。

**為什麼用固定權重版而不是 quant_layer2.py 預設的動態 IC 加權**：
動態 IC 加權每天的因子權重都不一樣，「這個因子權重 × 它的邊際貢獻」
這個算法在權重逐日變動時沒有一個乾淨的定義。固定權重版本（Task 6b 的
A 版：momentum 0.34／value 0.18／rev_yoy 0.18／low_vol 0.30，跟動態版
的 IC 實測基礎相同，只是不隨時間調整）讓「因子的權重」是一個單一、
明確的數字，才能做這種線性歸因分解。

直接執行：
  python strategy/attribution.py
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

# 跟 quant_layer2.py 的 build_positions() 內部 default_weights 保持同步
# （momentum/value/rev_yoy/low_vol 依 2026-08-18 Task 2 決定的實測 IC
# 相對強弱訂出，見 quant_layer2.py 該常數旁的完整推導註解）。
CORE_WEIGHTS = {"momentum": 0.34, "value": 0.18, "rev_yoy": 0.18, "low_vol": 0.30}


def _total_return(ret_series: pd.Series) -> float:
    """把逐日報酬序列複利成總報酬（%，小數形式，例如 0.921 = 92.1%）。"""
    return float((1 + ret_series).cumprod().iloc[-1] - 1)


def _run_variant(factors: dict, close: pd.DataFrame,
                 fixed_weights: Dict[str, float] = None,
                 no_timing: bool = False) -> Tuple[float, float]:
    """跑一個版本的回測，回傳 (毛報酬總報酬, 淨報酬總報酬)。

    no_timing=True：用 use_regime_exposure=True + 一個全期 BULL/exposure=1.0
    的假 regime_df，強制 is_bull 永遠成立、曝險永遠 1.0——這不是真的在用
    機制資訊，是借用「機制曝險模式下不受大盤擇時二元開關限制」這個既有
    行為，做出一個「同樣的選股邏輯，但沒有擇時」的乾淨對照組，不用另外
    在 build_positions() 裡開一個新分支。
    """
    kwargs = dict(use_fixed_weights=True, fixed_weights=fixed_weights)
    if no_timing:
        const_regime_df = pd.DataFrame(
            {"regime": "BULL", "exposure": 1.0}, index=close.index,
        )
        kwargs.update(regime_df=const_regime_df, use_regime_exposure=True)

    positions = q2.build_positions(factors, **kwargs)
    _, _, diagnostics = q2.run_backtest_detailed(close, positions)
    gross_total = _total_return(diagnostics["gross_ret"])
    net_total = _total_return(diagnostics["net_ret"])
    return gross_total, net_total


def run_attribution() -> Dict:
    logger.info("attribution：載入資料、建構因子...")
    data = q2.load_matrices(q2.DB_PATH, q2.START_DATE, q2.END_DATE)
    factors = q2.build_factors(data)
    close = factors["close"]

    logger.info("attribution：跑完整版（固定權重＋擇時）...")
    gross_full, net_full = _run_variant(factors, close, fixed_weights=None, no_timing=False)

    logger.info("attribution：跑「無擇時」版（同樣選股邏輯，關掉大盤擇時開關）...")
    gross_no_timing, net_no_timing = _run_variant(factors, close, fixed_weights=None, no_timing=True)

    single_factor_gross: Dict[str, float] = {}
    for name in CORE_WEIGHTS:
        logger.info(f"attribution：跑單因子版（{name}，其餘因子權重=0）...")
        g, _ = _run_variant(factors, close, fixed_weights={name: 1.0}, no_timing=True)
        single_factor_gross[name] = g

    marginal_contributions = {
        name: CORE_WEIGHTS[name] * single_factor_gross[name] for name in CORE_WEIGHTS
    }
    selection_return_approx = sum(marginal_contributions.values())
    timing_contribution = gross_full - gross_no_timing
    cost = gross_full - net_full
    residual = gross_no_timing - selection_return_approx

    reconstructed = selection_return_approx + timing_contribution - cost + residual
    identity_check = abs(reconstructed - net_full) < 1e-9

    result = {
        "net_full": net_full,
        "gross_full": gross_full,
        "gross_no_timing": gross_no_timing,
        "single_factor_gross": single_factor_gross,
        "marginal_contributions": marginal_contributions,
        "selection_return_approx": selection_return_approx,
        "timing_contribution": timing_contribution,
        "cost": cost,
        "residual": residual,
        "reconstructed": reconstructed,
        "identity_check": identity_check,
    }

    logger.info(f"attribution：完整版總報酬 {net_full*100:+.1f}%，"
               f"重建值 {reconstructed*100:+.1f}%（恆等式檢查："
               f"{'通過' if identity_check else '未通過，有 bug'}）")
    return result


def write_attribution_report(result: Dict, path: str = "reports/attribution_report.md") -> None:
    lines = []
    lines.append("# 績效歸因（Task 6d）\n")
    lines.append("把 Task 2 基準策略（固定權重版）的總報酬拆解成：因子邊際貢獻 + 擇時貢獻 "
                 "− 成本 + 殘差。這是一個**恆等式**（四項加總保證等於總報酬，見"
                 "`strategy/attribution.py` 的推導註解），不是四個獨立估計湊出來的近似值。\n")
    lines.append(f"完整版（固定權重＋擇時）淨總報酬：**{result['net_full']*100:+.1f}%**"
                 f"（毛報酬 {result['gross_full']*100:+.1f}%）\n")

    lines.append("## 1. 各因子邊際貢獻（單因子版回測比較）\n")
    lines.append("| 因子 | 複合權重 | 單因子版毛報酬 | 邊際貢獻（權重×單因子毛報酬）|")
    lines.append("|---|---:|---:|---:|")
    for name, w in CORE_WEIGHTS.items():
        single = result["single_factor_gross"][name]
        contrib = result["marginal_contributions"][name]
        lines.append(f"| {name} | {w:.2f} | {single*100:+.1f}% | {contrib*100:+.1f}% |")
    lines.append(f"| **合計** |  |  | **{result['selection_return_approx']*100:+.1f}%** |\n")

    lines.append("## 2. 分解結果\n")
    lines.append("| 項目 | 數值 |")
    lines.append("|---|---:|")
    lines.append(f"| Σ 各因子邊際貢獻 | {result['selection_return_approx']*100:+.1f}% |")
    lines.append(f"| 擇時貢獻（完整版毛報酬 − 無擇時版毛報酬） | {result['timing_contribution']*100:+.1f}% |")
    lines.append(f"| − 成本（毛報酬 − 淨報酬） | {-result['cost']*100:+.1f}% |")
    lines.append(f"| 殘差（無擇時版毛報酬 − Σ因子邊際貢獻，來自緩衝區/inertia等非線性效果） "
                 f"| {result['residual']*100:+.1f}% |")
    lines.append(f"| **重建總報酬（應等於完整版淨報酬）** | **{result['reconstructed']*100:+.1f}%** |")
    lines.append(f"| 完整版實際淨報酬 | {result['net_full']*100:+.1f}% |")
    lines.append(f"| 恆等式檢查 | {'✅ 通過（兩者一致）' if result['identity_check'] else '❌ 未通過（有 bug，需要排查）'} |\n")

    lines.append("## 白話解讀\n")
    sorted_factors = sorted(result["marginal_contributions"].items(), key=lambda kv: -kv[1])
    top_name, top_val = sorted_factors[0]
    bottom_name, bottom_val = sorted_factors[-1]
    lines.append(f"貢獻最大的因子是 **{top_name}**（{top_val*100:+.1f}%），"
                 f"貢獻最小的是 **{bottom_name}**（{bottom_val*100:+.1f}%）。"
                 f"擇時（大盤 60 日均線二元開關）對總報酬的貢獻是 "
                 f"{result['timing_contribution']*100:+.1f}%，成本總共拖累了 "
                 f"{result['cost']*100:.1f}% 個百分點。殘差 "
                 f"{result['residual']*100:+.1f}% 反映的是「把因子分數線性加權合成後"
                 "再統一排名選股」跟「單獨用每個因子各自選股、事後加權平均報酬」"
                 "之間的差異——正殘差代表因子組合在一起選股比單獨用有加分效果"
                 "（因子之間有互補性），負殘差則相反。")
    lines.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"attribution：報告已存到 {path}")


if __name__ == "__main__":
    result = run_attribution()
    write_attribution_report(result)
    print("\n✅ Task 6d 績效歸因完成。")
    print(f"   完整版總報酬 {result['net_full']*100:+.1f}%，"
         f"恆等式檢查：{'通過' if result['identity_check'] else '未通過'}")
