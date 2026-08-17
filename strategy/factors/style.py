"""
factors/style.py
─────────────────────────────────────────────────────────────
通用風格因子：動能、價值、低波動。從 quant_layer2.py v5 的
inline 邏輯抽出，數學定義不變（momentum_52w／low_vol_ivol 逐行對應原本的
52 週新高動能／60 日 IVOL），value_composite 依 Task 2 規格加入 PBR，
是唯一的行為變更（原本只用 1/PER）。
"""

from typing import Dict

import numpy as np
import pandas as pd


def momentum_52w(data: Dict[str, pd.DataFrame], window: int = 252) -> pd.DataFrame:
    """
    52 週新高動能（George & Hwang, 2004）：收盤價 / 過去 window 日內最高價。

    數值越接近 1 代表離 52 週新高越近，動能越強；
    這是相對量測（不是報酬率），對不同價位的股票可直接比較。
    """
    close = data["close"]
    high = close.rolling(window, min_periods=60).max()
    return close / high


def value_composite(data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    價值因子：0.5 × (1/PER) + 0.5 × (1/PBR)。

    原本 v5 只用 1/PER，這裡依 Task 2 規格加入 PBR，
    讓價值因子不只看獲利倍數，也看淨值倍數——
    純用 PER 會漏掉「獲利暫時掛零但帳上淨值扎實」的深度價值股。
    PBR 缺值（例如 `daily_valuation` 尚未涵蓋）時退化為單純 1/PER。
    """
    PER = data["PER"]
    PBR = data.get("PBR")

    inv_per = (1.0 / PER).replace([np.inf, -np.inf], np.nan)
    if PBR is None or PBR.empty or PBR.isna().all().all():
        return inv_per

    inv_pbr = (1.0 / PBR).replace([np.inf, -np.inf], np.nan)
    return 0.5 * inv_per + 0.5 * inv_pbr


def low_vol_ivol(data: Dict[str, pd.DataFrame], window: int = 60) -> pd.DataFrame:
    """
    低波動因子（Frazzini & Pedersen, 2014）：window 日已實現波動度的倒數。

    低波動異常：長期而言低波動股票的風險調整後報酬優於高波動股票，
    與傳統 CAPM（高風險應有高報酬）矛盾，是穩定存在的市場異常。
    """
    close = data["close"]
    daily_ret = close.pct_change()
    ivol = daily_ret.rolling(window, min_periods=max(20, window // 3)).std()
    return (1.0 / ivol).replace([np.inf, -np.inf], np.nan)
