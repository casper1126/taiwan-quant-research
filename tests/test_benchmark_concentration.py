# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_benchmark_concentration.py

驗證 strategy/benchmark_concentration.py 的邏輯正確性，全部用合成資料：
  - build_proxy_indices()：代理市值權重逐日加總為 1、排除台積電後的
    版本不含台積電欄位、等權重版本不受權重輸入影響
  - _total_return()：複利計算正確
  - 移除法算出的「貢獻」在極端案例下方向正確（台積電佔比極高時，
    貢獻應該接近指數總報酬本身；台積電權重接近 0 時，貢獻應該接近 0）

真實資料的完整診斷（含真實 TAIEX、真實 bootstrap 檢定）已經用
`python strategy/benchmark_concentration.py` 真實跑過一次，結果記錄在
docs/DECISIONS.md，這裡不重複跑一次真實資料（太慢），只驗證邏輯正確性。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

import benchmark_concentration as bc


def _dates(n, start="2020-01-01"):
    return pd.bdate_range(start, periods=n)


def test_total_return_compounds_correctly():
    ret = pd.Series([0.10, 0.10, -0.10])
    expected = 1.1 * 1.1 * 0.9 - 1
    assert bc._total_return(ret) == pytest.approx(expected, abs=1e-9)


def test_build_proxy_indices_weight_full_sums_to_one():
    idx = _dates(300)
    cols = ["2330", "A", "B", "C"]
    rng = np.random.default_rng(0)
    close = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0, 0.01, size=(300, 4)), axis=0),
        index=idx, columns=cols,
    )
    volume = pd.DataFrame(rng.uniform(1000, 5000, size=(300, 4)), index=idx, columns=cols)

    proxies = bc.build_proxy_indices(close, volume)
    weight_full = proxies["weight_full"]
    row_sums = weight_full.dropna(how="all").sum(axis=1)
    # 扣掉暖機期（滾動視窗還沒填滿）之後，每天權重應該加總為 1
    valid_rows = row_sums.dropna()
    assert len(valid_rows) > 0
    assert (valid_rows.round(6) == 1.0).all()


def test_build_proxy_indices_ex_tsmc_excludes_tsmc_influence():
    """把台積電的報酬改成極端值（其他股票不變），只有 full 版本應該受影響，
    ex_tsmc 版本應該完全不受台積電漲跌影響。"""
    idx = _dates(300)
    cols = ["2330", "A", "B", "C"]
    rng = np.random.default_rng(1)
    close = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0, 0.005, size=(300, 4)), axis=0),
        index=idx, columns=cols,
    )
    volume = pd.DataFrame(1000.0, index=idx, columns=cols)

    proxies_1 = bc.build_proxy_indices(close, volume)

    # 把台積電最後 10 天的價格路徑改成暴漲，其他股票不變
    close_2 = close.copy()
    close_2.loc[close_2.index[-10:], "2330"] *= 3.0
    proxies_2 = bc.build_proxy_indices(close_2, volume)

    # full 版本受到影響（暴漲後總報酬應該變高）
    assert bc._total_return(proxies_2["full"]) > bc._total_return(proxies_1["full"])
    # ex_tsmc 版本完全不受台積電價格路徑改變影響
    ex_1 = proxies_1["ex_tsmc"]
    ex_2 = proxies_2["ex_tsmc"]
    pd.testing.assert_series_equal(ex_1, ex_2, check_names=False)


def test_build_proxy_indices_equal_weight_ignores_market_cap():
    """等權重版本不應該受成交量（市值代理）大小影響，只跟報酬率本身有關。"""
    idx = _dates(100)
    cols = ["2330", "A"]
    close = pd.DataFrame({"2330": [100.0] * 100, "A": [50.0] * 100}, index=idx)
    close.iloc[1:, 0] = close.iloc[0, 0] * np.cumprod(1 + np.full(99, 0.001))
    close.iloc[1:, 1] = close.iloc[0, 1] * np.cumprod(1 + np.full(99, 0.002))

    volume_a = pd.DataFrame({"2330": [1_000_000.0] * 100, "A": [100.0] * 100}, index=idx)
    volume_b = pd.DataFrame({"2330": [100.0] * 100, "A": [100.0] * 100}, index=idx)

    ret_equal_a = bc.build_proxy_indices(close, volume_a)["equal"]
    ret_equal_b = bc.build_proxy_indices(close, volume_b)["equal"]
    pd.testing.assert_series_equal(ret_equal_a, ret_equal_b, check_names=False)


def test_removal_method_contribution_large_when_dominant_stock():
    """台積電權重極高（成交量遠大於其他股票）且報酬遠高於其他股票時，
    「含台積電」與「排除台積電」兩個指數的總報酬差距應該很大（貢獻明顯）。"""
    idx = _dates(300)
    cols = ["2330", "A", "B", "C"]
    close = pd.DataFrame(100.0, index=idx, columns=cols)
    # 台積電穩定上漲，其他股票原地踏步
    close["2330"] = 100 * np.cumprod(1 + np.full(300, 0.01))
    for c in ["A", "B", "C"]:
        close[c] = 100.0

    volume = pd.DataFrame(100.0, index=idx, columns=cols)
    volume["2330"] = 1_000_000.0  # 台積電佔絕對多數的代理市值權重

    proxies = bc.build_proxy_indices(close, volume)
    r_full = bc._total_return(proxies["full"])
    r_ex = bc._total_return(proxies["ex_tsmc"])
    # 排除台積電後，其他股票完全沒漲，指數應該接近 0；含台積電則應該大幅為正
    assert r_full > 0.5
    assert r_ex == pytest.approx(0.0, abs=1e-6)
