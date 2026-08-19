"""
regime/hmm_detector.py
─────────────────────────────────────────────────────────────
Task 3c：GaussianHMM 3 狀態機制偵測。

特徵：[TAIEX 日報酬, 20 日已實現波動度（年化）]

防止 Look-Ahead Bias 的核心設計（Expanding Window）：
  每個月初，只用「這個月開始之前」的全部歷史資料 fit 一個新模型，
  然後只拿這個模型去 predict 這一個月的狀態。下個月換一個（用更多歷史
  資料重新 fit 的）新模型，再 predict 下個月。
  這保證任何一天的機制標籤，只用得到當時已經發生過的資料——不會因為
  之後又抓了更多資料，導致「過去」的機制標籤跟著改變。
  tests/test_regime.py 直接斷言這個性質（同一段歷史，資料截止日往後
  延伸不會改變之前月份的輸出）。

最少需要 504 個交易日（約兩年）的歷史資料才開始輸出，之前一律 NaN
（樣本太少，HMM fit 出來的狀態不可靠）。
"""

from typing import Optional

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

MIN_HISTORY_DAYS = 504
N_STATES = 3
VOL_WINDOW = 20
RANDOM_STATE = 42


def _build_features(index_close: pd.Series) -> pd.DataFrame:
    ret = index_close.pct_change()
    vol = ret.rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std() * np.sqrt(252)
    feat = pd.DataFrame({"ret": ret, "vol": vol}).dropna()
    return feat


def _label_states(model: GaussianHMM) -> dict:
    """依模型的 means_ 把 0/1/2 對應到 bull/bear/neutral。"""
    means = model.means_  # shape (3, 2): col0=ret均值, col1=vol均值
    ret_means = means[:, 0]
    vol_means = means[:, 1]

    bull_state = int(np.argmax(ret_means))
    remaining = [s for s in range(N_STATES) if s != bull_state]

    # 剩下兩個狀態裡，波動均值較高者視為 bear（理想上其報酬均值也是負的，
    # 若不是負的仍以波動較高者為 bear，避免產生第四種未定義狀態）
    remaining_sorted = sorted(remaining, key=lambda s: vol_means[s], reverse=True)
    bear_state = remaining_sorted[0]
    neutral_state = remaining_sorted[1]

    return {bull_state: "bull", bear_state: "bear", neutral_state: "neutral"}


def detect_regimes_full(index_close: pd.Series,
                        min_history: int = MIN_HISTORY_DAYS,
                        n_iter: int = 100) -> "tuple[pd.Series, pd.Series]":
    """
    一次算完機制標籤與 p_bear（同一個模型，避免重複 fit 兩次）。

    每個月第一個交易日之後才會出現新的模型；同一個月內每一天都用
    同一個（月初 fit 好的）模型 predict。

    回傳 (labels, p_bear)：
      labels  : pd.Series(index=date, values in {'bull','neutral','bear', NaN})
      p_bear  : pd.Series(index=date, values in [0,1] 或 NaN)
    """
    feat = _build_features(index_close)
    if feat.empty:
        return pd.Series(dtype=object), pd.Series(dtype=float)

    labels_out = pd.Series(np.nan, index=index_close.index, dtype=object)
    pbear_out = pd.Series(np.nan, index=index_close.index, dtype=float)

    feat_dates = feat.index
    year_month = feat_dates.to_period("M")
    unique_months = year_month.unique()

    for month in unique_months:
        month_mask = (year_month == month)
        month_dates = feat_dates[month_mask]
        first_date_of_month = month_dates[0]

        train_feat = feat.loc[feat.index < first_date_of_month]
        if len(train_feat) < min_history:
            continue

        model = GaussianHMM(
            n_components=N_STATES,
            covariance_type="diag",
            n_iter=n_iter,
            random_state=RANDOM_STATE,
        )
        try:
            model.fit(train_feat.values)
        except Exception:
            continue

        label_map = _label_states(model)
        bear_state = [s for s, name in label_map.items() if name == "bear"][0]

        month_feat = feat.loc[month_dates]
        states = model.predict(month_feat.values)
        proba = model.predict_proba(month_feat.values)

        labels_out.loc[month_dates] = [label_map[s] for s in states]
        pbear_out.loc[month_dates] = proba[:, bear_state]

    return labels_out, pbear_out


def detect_regimes(index_close: pd.Series,
                   min_history: int = MIN_HISTORY_DAYS,
                   n_iter: int = 100) -> pd.Series:
    """僅回傳機制標籤（見 detect_regimes_full）。"""
    labels, _ = detect_regimes_full(index_close, min_history, n_iter)
    return labels


def bear_probability(index_close: pd.Series,
                     min_history: int = MIN_HISTORY_DAYS,
                     n_iter: int = 100) -> pd.Series:
    """僅回傳 p_bear（見 detect_regimes_full）。"""
    _, pbear = detect_regimes_full(index_close, min_history, n_iter)
    return pbear
