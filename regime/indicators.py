"""
regime/indicators.py
─────────────────────────────────────────────────────────────
Task 3a：11 個機制偵測指標。

設計原則（跟 factors/ 一致）：每個函式吃「已經算好的原始資料」
（pd.Series 或寬格式 pd.DataFrame），只負責時間序列/橫截面運算，
不做任何 SQL 查詢——資料載入統一在 regime/data_loader.py 做。
這樣每個函式都能用合成資料單獨測試，不需要真實資料庫。

全部輸出 pd.Series(index=date)，NaN 代表「資料不足，尚無法計算」。
"""

import numpy as np
import pandas as pd


def realized_vol_percentile(index_close: pd.Series, window: int = 20,
                            lookback: int = 252) -> pd.Series:
    """
    大盤已實現波動度的歷史分位數（0-1）。

    先算 20 日已實現波動度（年化），再看「今天的波動度」在過去 lookback
    天的波動度分佈裡排第幾百分位——分位數越高代表當下波動度相對過去
    處於偏高的位置（越接近恐慌），不是看波動度的絕對水準。
    """
    ret = index_close.pct_change()
    vol = ret.rolling(window, min_periods=window).std() * np.sqrt(252)

    def _pct_rank(x: np.ndarray) -> float:
        if np.isnan(x[-1]):
            return np.nan
        valid = x[~np.isnan(x)]
        if len(valid) < 2:
            return np.nan
        return float((valid <= x[-1]).mean())

    min_p = max(int(lookback * 0.5), window + 1)
    return vol.rolling(lookback, min_periods=min_p).apply(_pct_rank, raw=True)


def vol_of_vol(index_close: pd.Series, window: int = 20) -> pd.Series:
    """波動度的波動度：20 日已實現波動度本身，再取其 window 日標準差。"""
    ret = index_close.pct_change()
    vol = ret.rolling(window, min_periods=window).std() * np.sqrt(252)
    return vol.rolling(window, min_periods=window).std()


def market_breadth(close_matrix: pd.DataFrame, ma_window: int = 60) -> pd.Series:
    """市場寬度：當日收盤站上 ma_window 日均線的股票比例（0-1）。"""
    ma = close_matrix.rolling(ma_window, min_periods=ma_window).mean()
    above = (close_matrix > ma)
    valid = close_matrix.notna() & ma.notna()
    n_valid = valid.sum(axis=1)
    n_above = (above & valid).sum(axis=1)
    breadth = n_above / n_valid.replace(0, np.nan)
    return breadth


def new_highs_minus_lows(close_matrix: pd.DataFrame, window: int = 252) -> pd.Series:
    """(創新高家數 − 創新低家數) / 有效樣本數。"""
    roll_max = close_matrix.rolling(window, min_periods=window).max()
    roll_min = close_matrix.rolling(window, min_periods=window).min()
    valid = close_matrix.notna() & roll_max.notna() & roll_min.notna()
    is_high = (close_matrix >= roll_max) & valid
    is_low = (close_matrix <= roll_min) & valid
    n_valid = valid.sum(axis=1)
    result = (is_high.sum(axis=1) - is_low.sum(axis=1)) / n_valid.replace(0, np.nan)
    return result


def avg_pairwise_correlation(returns: pd.DataFrame, window: int = 60,
                             sample_n: int = 100, seed: int = 42) -> pd.Series:
    """
    平均兩兩相關係數（抽樣計算，避免 O(N^2) 全樣本相關矩陣太慢）。

    方法（精確公式，非近似）：
    在每個滾動視窗內，把每支股票的報酬做視窗內標準化（mean=0, std=1）
    得到 z_i(t)，令 S(t) = Σ_i z_i(t)。因為每支股票的變異數都是 1：
        Var(S) = N + Σ_{i≠j} Cov(z_i, z_j) = N + N(N-1)·ρ̄
    所以 ρ̄ = (Var(S) − N) / (N(N-1))，一次矩陣運算就能算出整個視窗的
    平均兩兩相關係數，不需要真的算 N×N 相關矩陣。

    sample_n：抽樣的股票數上限（固定亂數種子，跑第二次結果一樣）。
    抽樣依據「全期資料量最多」的前 sample_n 檔（近似大型/長期上市股），
    純粹是為了控制計算量，不是選股邏輯，不影響任何交易決策。
    """
    cols = returns.columns
    if len(cols) > sample_n:
        counts = returns.count().sort_values(ascending=False)
        sample_cols = counts.index[:sample_n]
    else:
        sample_cols = cols
    sample = returns[sample_cols]

    n_dates = len(sample)
    result = pd.Series(np.nan, index=sample.index)
    values = sample.values

    for t in range(window - 1, n_dates):
        w = values[t - window + 1: t + 1, :]
        col_valid = ~np.isnan(w).any(axis=0)
        n = int(col_valid.sum())
        if n < 3:
            continue
        w_valid = w[:, col_valid]
        mean = w_valid.mean(axis=0)
        std = w_valid.std(axis=0)
        std = np.where(std == 0, np.nan, std)
        z = (w_valid - mean) / std
        if np.isnan(z).any():
            continue
        S = z.sum(axis=1)
        var_S = S.var()
        rho = (var_S - n) / (n * (n - 1))
        result.iloc[t] = rho

    return result


def downside_asymmetry(index_returns: pd.Series, window: int = 60) -> pd.Series:
    """下跌波動 / 上漲波動：比例越高代表下跌時波動明顯放大（risk-off）。"""
    down = index_returns.where(index_returns < 0)
    up = index_returns.where(index_returns > 0)
    down_std = down.rolling(window, min_periods=max(5, window // 4)).std()
    up_std = up.rolling(window, min_periods=max(5, window // 4)).std()
    return down_std / up_std.replace(0, np.nan)


def foreign_flow_pressure(inst_data: pd.DataFrame, window: int = 20) -> pd.Series:
    """
    外資 window 日累積賣超佔比。

    inst_data：市場層級的外資 buy/sell/net（regime/data_loader.py 的
    inst_foreign，已跨股票加總，只保留 Foreign_Investor + Foreign_Dealer_Self）。

    佔比 = window 日累積淨賣超金額 / window 日累積成交金額（buy+sell）。
    正值代表賣超佔成交量的比例，數值越大＝外資撤出力道越強。
    """
    if inst_data is None or inst_data.empty:
        return pd.Series(dtype=float)

    net_roll = inst_data["net"].rolling(window, min_periods=max(3, window // 4)).sum()
    turnover_roll = (inst_data["buy"] + inst_data["sell"]).rolling(
        window, min_periods=max(3, window // 4)).sum()

    pressure = -1.0 * net_roll / turnover_roll.replace(0, np.nan)
    return pressure


def margin_stress(margin_total: pd.Series, window: int = 5) -> pd.Series:
    """融資餘額 window 日變化率（負值＝融資餘額快速下降＝去槓桿壓力）。"""
    if margin_total is None or margin_total.empty:
        return pd.Series(dtype=float)
    return margin_total.pct_change(window)


def margin_squeeze_ratio(margin_total: pd.Series, index_close: pd.Series) -> pd.Series:
    """
    全市場融資緊縮訊號，與 Task 2 的 factors.taiwan.margin_squeeze_market 同款
    （squeeze_ratio = 融資回檔幅度 / 大盤回檔幅度，皆從 252 日高點起算；
    大盤回檔 < 3% 視為雜訊回傳 NaN）。這裡獨立複製一份實作，避免 regime/
    模組反向 import strategy/factors（保持 Task 3 的獨立性）。
    """
    if margin_total is None or index_close is None or margin_total.empty or index_close.empty:
        return pd.Series(dtype=float)

    margin_high = margin_total.rolling(252, min_periods=60).max()
    index_high = index_close.rolling(252, min_periods=60).max()

    margin_decline = (margin_high - margin_total) / margin_high
    index_decline = (index_high - index_close) / index_high

    ratio = margin_decline / index_decline.replace(0, np.nan)
    return ratio.where(index_decline >= 0.03, np.nan)


def margin_capitulation(margin_total: pd.Series, volume: pd.Series) -> pd.Series:
    """
    融資投降訊號（布林）：急縮後已穩、且量能萎縮。

    條件（三者同時成立才是 True）：
      1. 過去已發生急縮：過去 20 個交易日內，曾出現任何 5 日跌幅 > 5%
         （用 20 日滾動視窗中「曾經」達到 -5% 的最大 5 日跌幅來判斷「已發生
          過」，不是只看最新一天——急縮跟穩定之間通常會間隔幾天到兩週）
      2. 現在已經穩定：最近 3 日融資變化率的絕對值 < 0.5%
      3. 量縮：成交量低於 20 日均量

    語意是「急殺後籌碼面已經洗清、量能也縮了」，常被視為短線止穩訊號。
    """
    if margin_total is None or margin_total.empty or volume is None or volume.empty:
        return pd.Series(dtype=object)

    chg_5d = margin_total.pct_change(5)
    had_sharp_decline = (chg_5d.rolling(20, min_periods=1).min() < -0.05)

    chg_3d = margin_total.pct_change(3)
    now_stable = chg_3d.abs() < 0.005

    vol_ma20 = volume.rolling(20, min_periods=20).mean()
    vol_low = volume < vol_ma20

    idx = margin_total.index.union(volume.index)
    result = (had_sharp_decline.reindex(idx) &
              now_stable.reindex(idx) &
              vol_low.reindex(idx))

    valid = chg_5d.reindex(idx).notna() & chg_3d.reindex(idx).notna() & vol_ma20.reindex(idx).notna()
    result = result.where(valid, np.nan)
    return result


def price_trend_vs_ma(index_close: pd.Series, window: int = 200) -> pd.Series:
    """
    大盤價格趨勢濾網：TAIEX 收盤價相對 window 日均線的乖離率。

    2026-08-19 新增（Task 3 收尾，見 docs/DECISIONS.md）：原本 3b 的六個
    健康分數成分全部是「市場廣度／微結構」型指標（寬度、高低差、相關性、
    不對稱性、波動、融資緊縮），沒有任何一個直接反映「大盤價格本身在
    漲還是在跌」，導致 2023-24 這種「指數漲、但漲勢集中在少數權值股、
    多數個股沒有跟著漲」的市場，健康分數容易失真地偏低。

    參考 Kritzman, Page & Turkington (2012)〈Regime Shifts: Implications
    for Dynamic Strategies〉的機制模型設計精神——同時納入「趨勢」與
    「風險」兩類指標，不能只看風險面——這裡補上最直接的趨勢指標：
    價格 vs 長期均線，正值代表站上均線（趨勢偏多），負值代表跌破
    （趨勢偏空）。

    傳回值本身是連續的乖離率（不是 0/1 二元訊號），交給 health_score.py
    的 rolling-252 分位數轉換去標準化，跟其他成分的處理方式一致。
    """
    if index_close is None or index_close.empty:
        return pd.Series(dtype=float)
    ma = index_close.rolling(window, min_periods=window).mean()
    return (index_close / ma) - 1.0


def turnover_structure(index_volume: pd.Series, window: int = 20) -> pd.Series:
    """量能相對 window 日均量的比值：>1 表示量能放大，<1 表示量縮。"""
    if index_volume is None or index_volume.empty:
        return pd.Series(dtype=float)
    ma = index_volume.rolling(window, min_periods=window).mean()
    return index_volume / ma.replace(0, np.nan)
