"""
tests/test_factors.py
─────────────────────────────────────────────────────────────
本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，
使用構造資料屬正常做法。

這裡測試的是「函式在已知輸入下是否算出已知答案」（例如：融資暴增的
股票，margin_usage 是否真的算出負值），不是在驗證真實資料上的因子
績效（IC、Sharpe 等）——那些數字只能來自真實資料庫查詢，見
docs/PROJECT_STATUS.md 的 Task 2 章節。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

from factors.base import cross_zscore, winsorize
from factors.taiwan import margin_usage, margin_squeeze_market


# ══════════════════════════════════════════════════════════════
# cross_zscore
# ══════════════════════════════════════════════════════════════

def test_cross_zscore_basic_standardization():
    """同一天三檔股票，z-score 後均值應為 0。"""
    dates = pd.date_range("2024-01-01", periods=3)
    m = pd.DataFrame(
        {"A": [1.0, 2.0, 3.0], "B": [2.0, 4.0, 6.0], "C": [3.0, 6.0, 9.0]},
        index=dates,
    )
    z = cross_zscore(m)
    assert np.allclose(z.mean(axis=1).values, 0.0, atol=1e-9)


def test_cross_zscore_nan_filled_with_zero_not_dropped():
    """
    NaN 應該被填 0（代表「無資訊、中性」），不是被丟掉整列，
    也不能讓 NaN 被誤判成「因子值最差」（例如填 -999）。
    """
    dates = pd.date_range("2024-01-01", periods=2)
    m = pd.DataFrame(
        {"A": [1.0, np.nan], "B": [2.0, 5.0], "C": [3.0, 6.0]},
        index=dates,
    )
    z = cross_zscore(m)
    assert z.loc[dates[1], "A"] == 0.0
    # 其餘欄位仍正常計算，不應該因為 A 是 NaN 就整天都是 0
    assert z.loc[dates[1], "B"] != 0.0


def test_cross_zscore_all_nan_row_returns_zero():
    """整天所有股票都是 NaN（std 也是 NaN）時，不應該炸掉，應該全部填 0。"""
    dates = pd.date_range("2024-01-01", periods=1)
    m = pd.DataFrame({"A": [np.nan], "B": [np.nan]}, index=dates)
    z = cross_zscore(m)
    assert (z.loc[dates[0]] == 0.0).all()


def test_cross_zscore_clips_extreme_values():
    """極端值應該被裁切在 [-3, 3]，不能讓單一離群值主導後續加權平均。"""
    dates = pd.date_range("2024-01-01", periods=1)
    m = pd.DataFrame({"A": [1.0], "B": [1.0], "C": [1.0], "D": [1000.0]}, index=dates)
    z = cross_zscore(m)
    assert z.loc[dates[0], "D"] <= 3.0


# ══════════════════════════════════════════════════════════════
# winsorize
# ══════════════════════════════════════════════════════════════

def test_winsorize_clips_to_quantile_bounds():
    """極端值應該被裁切到分位數邊界，但不改變樣本數（不刪列）。"""
    dates = pd.date_range("2024-01-01", periods=1)
    m = pd.DataFrame(
        {str(i): [float(i)] for i in range(100)}, index=dates
    )
    w = winsorize(m, lower=0.01, upper=0.99)
    assert w.shape == m.shape
    assert w.loc[dates[0]].max() < m.loc[dates[0]].max()
    assert w.loc[dates[0]].min() > m.loc[dates[0]].min()


# ══════════════════════════════════════════════════════════════
# margin_usage
# ══════════════════════════════════════════════════════════════

def test_margin_usage_negative_for_margin_surge_stock():
    """
    融資暴增的股票（20 日內餘額翻倍）＝散戶追高，
    margin_usage 依規格取負號，應該算出負值。
    """
    dates = pd.date_range("2024-01-01", periods=25, freq="D")
    close = pd.DataFrame({"SURGE": np.linspace(100, 110, 25)}, index=dates)
    # 前 5 天平穩，之後 20 天內餘額從 1000 漲到 2000（暴增）
    margin_balance = pd.DataFrame(
        {"SURGE": [1000.0] * 5 + list(np.linspace(1000, 2000, 20))},
        index=dates,
    )
    data = {"close": close, "margin_balance": margin_balance}

    result = margin_usage(data, window=20)
    last_value = result["SURGE"].iloc[-1]
    assert last_value < 0


def test_margin_usage_positive_for_margin_shrink_stock():
    """反例：融資餘額萎縮（去槓桿）應該算出正值，確認正負號沒有反過來。"""
    dates = pd.date_range("2024-01-01", periods=25, freq="D")
    close = pd.DataFrame({"SHRINK": np.linspace(100, 95, 25)}, index=dates)
    margin_balance = pd.DataFrame(
        {"SHRINK": [1000.0] * 5 + list(np.linspace(1000, 500, 20))},
        index=dates,
    )
    data = {"close": close, "margin_balance": margin_balance}

    result = margin_usage(data, window=20)
    assert result["SHRINK"].iloc[-1] > 0


def test_margin_usage_missing_data_returns_nan_placeholder():
    """margin_trading 尚未載入時（例如 backfill 還沒跑完），回傳 NaN 佔位矩陣，不應該報錯。"""
    dates = pd.date_range("2024-01-01", periods=5)
    close = pd.DataFrame({"A": [1.0] * 5}, index=dates)
    data = {"close": close, "margin_balance": pd.DataFrame()}

    result = margin_usage(data)
    assert result.isna().all().all()
    assert result.shape == close.shape


# ══════════════════════════════════════════════════════════════
# margin_squeeze_market
# ══════════════════════════════════════════════════════════════

def test_margin_squeeze_ratio_half_when_margin_declines_half_as_much_as_index():
    """
    規格範例：大盤跌 20%、融資跌 10% 時，squeeze_ratio 應該 = 0.5。

    建構方式：先用 300 天的平穩期讓 rolling(252) 高點穩定下來，
    再在最後一天讓大盤跌 20%、融資跌 10%（同一個高點起算）。
    """
    n = 300
    dates = pd.date_range("2023-01-01", periods=n, freq="D")

    index_close = pd.Series(100.0, index=dates)
    margin_total = pd.Series(1000.0, index=dates)

    # 最後一天：大盤從高點 100 跌到 80（跌 20%），融資從高點 1000 跌到 900（跌 10%）
    index_close.iloc[-1] = 80.0
    margin_total.iloc[-1] = 900.0

    data = {"index_close": index_close, "margin_total": margin_total}
    result = margin_squeeze_market(data)

    assert result.iloc[-1] == pytest.approx(0.5, abs=1e-6)


def test_margin_squeeze_ratio_nan_when_index_barely_pulled_back():
    """大盤回檔 < 3% 時視為雜訊，squeeze_ratio 應該回傳 NaN。"""
    n = 300
    dates = pd.date_range("2023-01-01", periods=n, freq="D")
    index_close = pd.Series(100.0, index=dates)
    margin_total = pd.Series(1000.0, index=dates)

    # 大盤只跌 1%（< 3% 門檻）
    index_close.iloc[-1] = 99.0
    margin_total.iloc[-1] = 950.0

    data = {"index_close": index_close, "margin_total": margin_total}
    result = margin_squeeze_market(data)

    assert pd.isna(result.iloc[-1])


def test_margin_squeeze_ratio_missing_data_returns_empty_series():
    """margin_trading 或 market_index 尚未載入時，回傳空 Series，不應該報錯。"""
    data = {"index_close": pd.Series(dtype=float), "margin_total": pd.Series(dtype=float)}
    result = margin_squeeze_market(data)
    assert result.empty
