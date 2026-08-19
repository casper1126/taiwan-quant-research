"""
regime/health_score.py
─────────────────────────────────────────────────────────────
Task 3b：把 regime/indicators.py 的原始指標合成一個 0-100 的市場健康分數。

方法：
1. 每個成分先轉成「rolling-252 日分位數」（0-1）——把不同單位、不同尺度的
   指標統一轉成「跟過去一年相比，現在算高還是低」，才能加權合成。
   （realized_vol_percentile 在 indicators.py 裡本身就已經是分位數輸出，
   這裡直接使用，不重複轉換；其餘成分在本檔案內轉換。）
2. 加權合成：波動(反) 18、寬度 22、高低差 13、相關性(反) 13、不對稱(反) 13、
   squeeze 12（六項權重總和 91）；融資投降(margin_capitulation) 額外加 9 分
   的「加分項」（不是分位數，是布林訊號直接加分），滿分正好 100。
3. NaN 成分：該日若某成分是 NaN，跳過該成分並用剩餘成分的權重比例重新
   正規化（例如只有 3 項有值，就用那 3 項的權重比例分配到 91 分）。
   若六項核心成分全部 NaN，當日健康分數輸出 NaN。
"""

from typing import Dict

import numpy as np
import pandas as pd

# 六項核心成分權重（不含 capitulation 加分項），總和 91
CORE_WEIGHTS = {
    "vol_pctile":     (18, True),   # (權重, 是否反向)
    "breadth":        (22, False),
    "hl_diff":        (13, False),
    "corr":           (13, True),
    "asym":           (13, True),
    "squeeze":        (12, False),
}
CAPITULATION_BONUS = 9


def _rolling_percentile(series: pd.Series, lookback: int = 252) -> pd.Series:
    """把原始指標序列轉成 rolling-lookback 日的百分位排名（0-1）。"""
    if series is None or series.empty:
        return pd.Series(dtype=float)

    def _pct_rank(x: np.ndarray) -> float:
        if np.isnan(x[-1]):
            return np.nan
        valid = x[~np.isnan(x)]
        if len(valid) < 2:
            return np.nan
        return float((valid <= x[-1]).mean())

    min_p = max(int(lookback * 0.5), 2)
    return series.rolling(lookback, min_periods=min_p).apply(_pct_rank, raw=True)


def compute_health_score(raw: Dict[str, pd.Series]) -> pd.Series:
    """
    raw 需要的 key（皆為 pd.Series(index=date)，未轉分位數的原始值，
    唯一例外是 vol_pctile 本身已經是分位數）：
      vol_pctile   : indicators.realized_vol_percentile() 的輸出（已是 0-1）
      breadth      : indicators.market_breadth() 的輸出
      hl_diff      : indicators.new_highs_minus_lows() 的輸出
      corr         : indicators.avg_pairwise_correlation() 的輸出
      asym         : indicators.downside_asymmetry() 的輸出
      squeeze      : indicators.margin_squeeze_ratio() 的輸出
      capitulation : indicators.margin_capitulation() 的輸出（布林/NaN）
    """
    idx = None
    for key in ("vol_pctile", "breadth", "hl_diff", "corr", "asym", "squeeze"):
        s = raw.get(key)
        if s is not None and not s.empty:
            idx = s.index if idx is None else idx.union(s.index)
    if idx is None:
        return pd.Series(dtype=float)

    pctiles = {}
    pctiles["vol_pctile"] = raw.get("vol_pctile", pd.Series(dtype=float)).reindex(idx)
    for key in ("breadth", "hl_diff", "corr", "asym", "squeeze"):
        pctiles[key] = _rolling_percentile(raw.get(key, pd.Series(dtype=float)).reindex(idx))

    pctile_df = pd.DataFrame(pctiles)

    weight_sum = pd.Series(0.0, index=idx)
    weighted_val = pd.Series(0.0, index=idx)
    for name, (weight, invert) in CORE_WEIGHTS.items():
        col = pctile_df[name]
        contrib = (1.0 - col) if invert else col
        available = contrib.notna()
        weighted_val = weighted_val.add(contrib.where(available, 0.0) * weight, fill_value=0.0)
        weight_sum = weight_sum.add(available.astype(float) * weight, fill_value=0.0)

    core_score = np.where(weight_sum > 0, weighted_val / weight_sum * 91.0, np.nan)
    core_score = pd.Series(core_score, index=idx)

    capitulation = raw.get("capitulation")
    if capitulation is not None and not capitulation.empty:
        cap_aligned = capitulation.reindex(idx)
        bonus = cap_aligned.map(lambda v: CAPITULATION_BONUS if v is True else 0.0)
        bonus = pd.to_numeric(bonus, errors="coerce").fillna(0.0)
    else:
        bonus = pd.Series(0.0, index=idx)

    health = core_score + bonus
    return health.clip(lower=0, upper=100)
