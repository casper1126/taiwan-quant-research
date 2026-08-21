# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_portfolio.py

驗證 strategy/portfolio.py（Task 4c）的邏輯正確性，全部使用合成資料。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

from portfolio import (
    equal_weight, risk_parity_weight, apply_buffer, decompose_turnover,
)


def test_equal_weight_basic():
    w = equal_weight(["A", "B", "C", "D"])
    assert w == {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25}
    assert sum(w.values()) == pytest.approx(1.0)


def test_equal_weight_empty():
    assert equal_weight([]) == {}


def test_risk_parity_weight_favors_low_vol():
    idx = pd.bdate_range("2024-01-01", periods=80)
    rng = np.random.default_rng(0)
    returns = pd.DataFrame({
        "LOW_VOL": rng.normal(0, 0.005, 80),
        "HIGH_VOL": rng.normal(0, 0.05, 80),
    }, index=idx)
    w = risk_parity_weight(returns, ["LOW_VOL", "HIGH_VOL"], window=60)
    assert w["LOW_VOL"] > w["HIGH_VOL"]
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-6)


def test_apply_buffer_entry_and_exit_thresholds():
    rank = pd.Series({f"S{i}": i for i in range(1, 51)})  # rank 1..50
    current = {"S28", "S29", "S46"}  # S28/29 在緩衝區內, S46 該出場了

    new_holdings = apply_buffer(rank, current, top_n=30, buffer_multiplier=1.5)
    # top_n=30, buffer=1.5 → 出場閾值 45；S46 排名 46 > 45，應該被賣掉
    assert "S46" not in new_holdings
    # S28/S29 排名在 30 之內，本來就持有，應該留著
    assert "S28" in new_holdings
    assert "S29" in new_holdings
    # 排名 1~30（扣掉已持有的）應該被買進，湊到 top_n=30 檔
    assert len(new_holdings) == 30


def test_apply_buffer_no_entry_when_disallowed():
    rank = pd.Series({f"S{i}": i for i in range(1, 51)})
    current = {"S46"}  # 已跌出緩衝區，該賣
    new_holdings = apply_buffer(rank, current, top_n=30, buffer_multiplier=1.5,
                                allow_entry=False)
    # 空頭關閉買入：不該有任何新股進場，只剩賣出後的空手
    assert new_holdings == set()


def test_decompose_turnover_pure_swap():
    """換掉全部持股（A,B → C,D），應該全部歸類為 swap，timing/reweight 為 0。"""
    dates = pd.bdate_range("2024-01-01", periods=5)
    cols = ["A", "B", "C", "D"]
    pos = pd.DataFrame(0.0, index=dates, columns=cols)
    pos.loc[dates[0], ["A", "B"]] = 0.5
    pos.loc[dates[1], ["C", "D"]] = 0.5

    decomp = decompose_turnover(pos, [dates[0], dates[1]])
    assert decomp.loc[dates[1], "swap"] == pytest.approx(2.0)  # |0.5|*4
    assert decomp.loc[dates[1], "timing"] == pytest.approx(0.0)
    assert decomp.loc[dates[1], "reweight"] == pytest.approx(0.0)


def test_decompose_turnover_pure_timing():
    """留倉股票不變，只有曝險比例從 1.0 縮到 0.5，應該全部歸類為 timing。"""
    dates = pd.bdate_range("2024-01-01", periods=5)
    cols = ["A", "B"]
    pos = pd.DataFrame(0.0, index=dates, columns=cols)
    pos.loc[dates[0], ["A", "B"]] = 0.5   # 總曝險 1.0
    pos.loc[dates[1], ["A", "B"]] = 0.25  # 總曝險 0.5，等比例縮放

    exposure = pd.Series({dates[0]: 1.0, dates[1]: 0.5})
    decomp = decompose_turnover(pos, [dates[0], dates[1]], exposure=exposure)
    assert decomp.loc[dates[1], "swap"] == pytest.approx(0.0)
    assert decomp.loc[dates[1], "timing"] == pytest.approx(0.5)  # |0.25-0.5|*2
    assert decomp.loc[dates[1], "reweight"] == pytest.approx(0.0, abs=1e-9)
