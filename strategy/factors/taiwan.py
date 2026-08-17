"""
factors/taiwan.py
─────────────────────────────────────────────────────────────
台股獨有因子：籌碼（三大法人）、融資融券、（品質，佔位）。

inst_flow 相較 quant_layer2.py v5 的舊版有一個行為變更：
舊版是「60 日累積買超金額」原始值，沒有依成交量正規化，
大型股買超金額天生大、會系統性贏過小型股。這裡依 Task 2 規格
除以 60 日平均成交金額，讓不同市值的股票可以公平比較。
"""

from typing import Dict

import numpy as np
import pandas as pd
from loguru import logger


def inst_flow(data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    籌碼因子：外資 + 投信 60 日累積淨買超 / 60 日平均成交金額（正規化）。

    `data["institutional"]` 由 quant_layer2.py 的 load_matrices() 從
    institutional_investors 表讀取並已篩選 investor_type ∈
    {Foreign_Investor, Foreign_Dealer_Self, Investment_Trust}，
    這裡只負責時間序列運算，不重複做篩選。

    正規化理由：大型股的買超金額天生比小型股大很多（絕對金額，不是比例），
    直接排名會系統性偏好大型股，除以流動性（60 日平均成交金額）
    把它轉成「買超力道相對於這支股票的正常交易量有多強」，
    才是公平的橫截面比較基礎。
    """
    close = data["close"]
    inst_raw = data.get("institutional")

    if inst_raw is None or inst_raw.empty:
        logger.warning("institutional_investors 資料尚未載入，inst_flow 回傳 NaN 佔位矩陣")
        return pd.DataFrame(np.nan, index=close.index, columns=close.columns)

    inst_aligned = inst_raw.reindex(index=close.index, columns=close.columns).fillna(0.0)
    inst_flow_60 = inst_aligned.rolling(60, min_periods=10).sum()

    volume = data["volume"]
    dollar_volume_60 = (volume * close).rolling(60, min_periods=20).mean()

    normalized = inst_flow_60 / dollar_volume_60.replace(0, np.nan)
    return normalized.replace([np.inf, -np.inf], np.nan)


def margin_usage(data: Dict[str, pd.DataFrame], window: int = 20) -> pd.DataFrame:
    """
    融資使用率因子：個股融資餘額 window 日變化率 × (−1)。

    融資快速增加＝散戶（融資買盤多為散戶）追高，是負向訊號，
    所以取負號——融資餘額漲得越快，因子值越低。

    `data["margin_balance"]` 由 load_matrices() 從 margin_trading 表
    讀取（margin_trading 是 Task 1 新增的表，可能還在回填中）。
    """
    close = data["close"]
    margin_balance = data.get("margin_balance")

    if margin_balance is None or margin_balance.empty:
        logger.warning("margin_trading 資料尚未載入，margin_usage 回傳 NaN 佔位矩陣")
        return pd.DataFrame(np.nan, index=close.index, columns=close.columns)

    margin_aligned = margin_balance.reindex(index=close.index, columns=close.columns)
    change = margin_aligned.pct_change(window)
    return -1.0 * change


def margin_squeeze_market(data: Dict[str, pd.DataFrame]) -> pd.Series:
    """
    全市場融資緊縮訊號（不是選股因子，供 Task 3 機制偵測使用）。

    squeeze_ratio = 融資總餘額回檔幅度 / TAIEX 回檔幅度（皆從 252 日高點起算）。
    數值越小代表融資的回檔幅度遠小於大盤（融資的人不動、可能還沒認賠），
    數值接近或超過 1 則代表融資追殺力道跟大盤跌幅同步甚至更兇。

    大盤回檔 < 3% 時視為雜訊（正常波動而非真正的下跌段），回傳 NaN。

    `data["margin_total"]`：全市場融資餘額加總，pd.Series(index=date)。
    `data["index_close"]`：TAIEX 收盤價，pd.Series(index=date)。
    """
    margin_total = data.get("margin_total")
    index_close = data.get("index_close")

    if margin_total is None or index_close is None or margin_total.empty or index_close.empty:
        logger.warning("margin_trading 或 market_index 資料尚未載入，margin_squeeze_market 回傳空 Series")
        return pd.Series(dtype=float)

    margin_high = margin_total.rolling(252, min_periods=60).max()
    index_high = index_close.rolling(252, min_periods=60).max()

    margin_decline = (margin_high - margin_total) / margin_high
    index_decline = (index_high - index_close) / index_high

    squeeze_ratio = margin_decline / index_decline.replace(0, np.nan)
    return squeeze_ratio.where(index_decline >= 0.03, np.nan)


def quality(data: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    品質因子（佔位）：目前沒有 ROE / 負債比等財報資料可用，回傳全 NaN 矩陣。

    佔位而非省略這個函式，是為了讓 quant_layer2.py 的因子清單一開始就
    包含 "quality" 這個 key，之後真的接上財報資料時只需要改這裡，
    不用去改呼叫端。
    """
    close = data["close"]
    logger.warning("quality 因子尚未實作（缺財報資料來源），回傳 NaN 佔位矩陣")
    return pd.DataFrame(np.nan, index=close.index, columns=close.columns)
