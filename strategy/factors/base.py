"""
factors/base.py
─────────────────────────────────────────────────────────────
因子計算共用工具：橫截面標準化與極端值處理。

「橫截面」= 同一天，跨所有股票做運算（axis=1），
這是多因子選股的核心操作，所有因子最終都會用到。
"""

import numpy as np
import pandas as pd


def cross_zscore(m: pd.DataFrame, clip: float = 3.0) -> pd.DataFrame:
    """
    橫截面 z-score：每一天，把該天所有股票的因子值標準化為均值 0、標準差 1。

    Parameters
    ----------
    m    : 寬格式因子矩陣（index=date, columns=stock_id）
    clip : 標準化後裁切的絕對值上限，避免極端值主導後續加權平均

    Returns
    -------
    標準化後的矩陣，NaN 填 0（代表「無資訊、不影響排名」，
    而不是「因子值為 0」，這樣個股缺值不會被誤判為最差）。
    """
    mean = m.mean(axis=1)
    std = m.std(axis=1).replace(0, np.nan)
    z = m.sub(mean, axis=0).div(std, axis=0)
    return z.clip(-clip, clip).fillna(0)


def winsorize(m: pd.DataFrame, lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """
    橫截面 winsorize：每一天，把該天所有股票的因子值裁切到
    [lower 分位數, upper 分位數] 之間，壓制極端值對排名的影響，
    同時不像刪除離群值那樣減少樣本數。

    Parameters
    ----------
    m     : 寬格式因子矩陣（index=date, columns=stock_id）
    lower : 下分位數（預設 1%）
    upper : 上分位數（預設 99%）
    """
    lo = m.quantile(lower, axis=1)
    hi = m.quantile(upper, axis=1)
    return m.clip(lower=lo, upper=hi, axis=0)
