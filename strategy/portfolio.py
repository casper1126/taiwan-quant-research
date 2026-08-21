"""
strategy/portfolio.py
─────────────────────────────────────────────────────────────
Task 4c：部位建構的共用邏輯抽出成獨立模組（跟 Task 2 把因子邏輯抽到
strategy/factors/ 是同樣的精神）：權重分配方式（等權／風險平價）、
緩衝區進出場規則、換手率分解報告。

quant_layer2.py 的 build_positions() 呼叫這裡的函式，不重複實作邏輯。
"""

from pathlib import Path
from typing import Dict, List, Optional, Set

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════
# 權重分配方式
# ══════════════════════════════════════════════════════════════

def equal_weight(holdings: List[str]) -> Dict[str, float]:
    """等權分配：每檔持股權重 = 1 / 檔數。"""
    if not holdings:
        return {}
    w = 1.0 / len(holdings)
    return {s: w for s in holdings}


def risk_parity_weight(returns: pd.DataFrame, holdings: List[str],
                       window: int = 60) -> Dict[str, float]:
    """
    風險平價權重：權重 ∝ 1 / window 日波動度，波動越低分到的權重越高，
    讓每檔股票對投資組合總風險的貢獻趨於一致。權重正規化後總和 = 1。

    returns : 日報酬寬格式矩陣（index=date, columns=stock_id），只需要
              截止到「今天」為止的資料（呼叫端自己做 .loc[:today] 切片，
              避免用到未來報酬造成 look-ahead）
    holdings: 目前持股清單
    window  : 波動度計算窗口，預設 60 個交易日
    """
    if not holdings:
        return {}
    vols = returns[holdings].iloc[-window:].std().replace(0, np.nan)
    vols = vols.dropna()
    if vols.empty:
        return equal_weight(holdings)
    inv_vol = 1.0 / vols
    normed = inv_vol / inv_vol.sum()
    return {s: float(normed.get(s, 1.0 / len(holdings))) for s in holdings}


# ══════════════════════════════════════════════════════════════
# 緩衝區進出場規則
# ══════════════════════════════════════════════════════════════

def apply_buffer(rank_today: pd.Series, current_holdings: Set[str],
                 top_n: int = 30, buffer_multiplier: float = 1.5,
                 allow_entry: bool = True) -> Set[str]:
    """
    緩衝區換股規則：
      進場：排名進入前 top_n 才買（allow_entry=False 時完全不買，
            用於機制擇時暫停買入的舊行為，quant_layer2.py 目前的機制
            模式已經不用這個開關，但保留給其他呼叫端使用）
      出場：排名跌出前 top_n × buffer_multiplier（預設 top_n=30,
            multiplier=1.5 → 出場閾值 45）才賣

    這讓已持倉的股票有更多空間，大幅降低邊界替換頻率（不會因為排名
    在 29/31 之間微幅震盪就一直買賣同一檔股票）。

    Returns
    -------
    Set[str]：套用完進出場規則後的新持股清單
    """
    exit_threshold = int(top_n * buffer_multiplier)

    to_sell = {s for s in current_holdings
               if pd.isna(rank_today.get(s, np.nan)) or rank_today[s] > exit_threshold}
    holdings = current_holdings - to_sell

    if allow_entry:
        entry_candidates = set(rank_today[rank_today <= top_n].index.tolist())
        holdings |= (entry_candidates - holdings)

        if len(holdings) > top_n:
            ranked = sorted(
                [(s, rank_today.get(s, 9999)) for s in holdings],
                key=lambda x: x[1]
            )
            holdings = {s for s, _ in ranked[:top_n]}

    return holdings


# ══════════════════════════════════════════════════════════════
# 換手率分解（Task 4c：換股 vs 權重調整 vs 擇時貢獻）
# ══════════════════════════════════════════════════════════════

def decompose_turnover(position: pd.DataFrame, rebal_dates: List[pd.Timestamp],
                       exposure: Optional[pd.Series] = None) -> pd.DataFrame:
    """
    把每次再平衡的換手率拆成三部分：

      換股（swap）      ：因為股票整個換掉（新進場／出場）造成的換手
      權重調整（reweight）：留在持股名單裡的股票，權重比例微調造成的換手
      擇時貢獻（timing）  ：留倉股票裡，可以用「單純曝險比例縮放」解釋掉
                            的那部分權重調整（例如機制從 BULL 降到 WARNING，
                            exposure 從 1.0 降到 0.4，留倉股票權重等比例
                            縮小——這部分歸類為擇時貢獻，不是選股邏輯換手）

    方法：
      1. 換股 = Σ|新進場股票的新權重| + Σ|出場股票的舊權重|
      2. 對仍留倉的股票，先算「如果只是曝險比例從 e_prev 變成 e_new，
         等比例縮放舊權重會變成多少」（timing_implied），
         擇時貢獻 = Σ|timing_implied − 舊權重|
      3. 權重調整 = 總換手 − 換股 − 擇時貢獻（殘差，代表選股邏輯本身
         （例如 IC 動態權重、風險平價）造成的權重重分配，跟擇時無關）

    exposure 是 regime_engine 的建議曝險比例序列（index=date）；如果沒有
    提供（例如非機制模式），視為每期都是 1.0，這時擇時貢獻恆為 0，全部
    歸類到換股/權重調整兩類。

    Returns
    -------
    pd.DataFrame，index=re-balance date（從第二次再平衡開始，因為第一次
    沒有「前一期」可比較），欄位：swap／reweight／timing／total
    """
    records = []
    for i in range(1, len(rebal_dates)):
        prev_date, today = rebal_dates[i - 1], rebal_dates[i]
        prev_pos = position.loc[prev_date]
        new_pos = position.loc[today]

        held_prev = set(prev_pos[prev_pos > 0].index)
        held_new = set(new_pos[new_pos > 0].index)

        entered = held_new - held_prev
        exited = held_prev - held_new
        held = held_prev & held_new

        swap = new_pos[list(entered)].abs().sum() + prev_pos[list(exited)].abs().sum()

        if exposure is not None and prev_date in exposure.index and today in exposure.index:
            e_prev = exposure.loc[prev_date]
            e_new = exposure.loc[today]
        else:
            e_prev = e_new = 1.0

        if held:
            held_list = list(held)
            prev_held_w = prev_pos[held_list]
            new_held_w = new_pos[held_list]
            if e_prev and e_prev > 0:
                timing_implied = prev_held_w * (e_new / e_prev)
            else:
                timing_implied = prev_held_w.copy()
            timing = (timing_implied - prev_held_w).abs().sum()
            reweight = (new_held_w - timing_implied).abs().sum()
        else:
            timing = 0.0
            reweight = 0.0

        total = swap + timing + reweight
        records.append({
            "date": today, "swap": swap, "timing": timing,
            "reweight": reweight, "total": total,
        })

    return pd.DataFrame(records).set_index("date")


def write_turnover_report(decomposition: pd.DataFrame,
                          path: str = "reports/turnover_decomposition.md") -> str:
    """把 decompose_turnover() 的結果寫成 Markdown 報告。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    if decomposition.empty:
        content = "# 換手率分解報告\n\n（再平衡次數不足，無法計算分解，至少需要 2 次再平衡）\n"
        Path(path).write_text(content, encoding="utf-8")
        return path

    total_swap = decomposition["swap"].sum()
    total_timing = decomposition["timing"].sum()
    total_reweight = decomposition["reweight"].sum()
    total_all = decomposition["total"].sum()

    def pct(x):
        return f"{x/total_all*100:.1f}%" if total_all > 0 else "N/A"

    lines = [
        "# 換手率分解報告",
        "",
        "把每次再平衡的換手率拆成三個來源：**換股**（整檔股票進出場）、",
        "**權重調整**（留倉股票的權重重分配，來自因子權重/風險平價變化）、",
        "**擇時貢獻**（留倉股票因為機制曝險比例縮放造成的等比例權重變化）。",
        "",
        "## 全期加總",
        "",
        f"| 來源 | 累積換手 | 佔比 |",
        f"|---|---|---|",
        f"| 換股 | {total_swap:.4f} | {pct(total_swap)} |",
        f"| 權重調整 | {total_reweight:.4f} | {pct(total_reweight)} |",
        f"| 擇時貢獻 | {total_timing:.4f} | {pct(total_timing)} |",
        f"| **總計** | **{total_all:.4f}** | 100.0% |",
        "",
        f"再平衡次數（可分解，不含第一次）：{len(decomposition)}",
        "",
        "## 逐次再平衡明細",
        "",
        decomposition.round(4).to_markdown(),
        "",
    ]
    content = "\n".join(lines)
    Path(path).write_text(content, encoding="utf-8")
    return path
