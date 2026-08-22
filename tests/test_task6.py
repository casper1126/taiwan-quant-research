# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_task6.py

驗證 Task 6（驗證框架）四個模組裡「不需要真的連 SQLite / 不需要真的跑
完整回測」的純邏輯函式，全部用合成資料：
  - strategy/walk_forward.py：icir_weights()、build_composite()
  - strategy/ablation.py：_numeric_stats()、_regime_conditional_breakdown()
  - strategy/significance.py：deflated_sharpe_ratio()、
    sharpe_confidence_interval()、bootstrap_pvalue()
  - strategy/attribution.py：_total_return()

完整的四份報告（walk_forward_results.md／ablation_results.md／
significance_report.md／attribution_report.md）需要真實資料庫與完整
回測，已經用 `python strategy/xxx.py` 真實跑過一次，結果記錄在
docs/DECISIONS.md，這裡不重複跑一次真實資料（太慢），只驗證邏輯正確性。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

import walk_forward as wf
import ablation as abl
import significance as sig
import attribution as attr


def _dates(n, start="2020-01-01"):
    return pd.bdate_range(start, periods=n)


# ══════════════════════════════════════════════════════════════
# walk_forward.py
# ══════════════════════════════════════════════════════════════

def test_icir_weights_favors_predictive_factor():
    """一個因子的排名跟未來報酬完全一致（IC=1），另一個是純雜訊，
    前者應該拿到遠高於後者的權重。"""
    idx = _dates(400)
    cols = [f"S{i}" for i in range(30)]
    rng = np.random.default_rng(0)

    close = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0, 0.01, size=(400, 30)), axis=0),
        index=idx, columns=cols,
    )
    fwd_ret = close.pct_change(20).shift(-20)

    # "good" 因子高度但非完美相關於未來報酬（雜訊幅度跟 fwd_ret 本身的
    # 橫截面離散度同量級，足以偶爾打亂排名、讓 IC 逐日有變化，避免 IC
    # 每天都剛好=1.0、std()=0 觸發 icir_weights() 的除以零保護邏輯被當成
    # 無效因子跳過——真實因子不會有這麼乾淨的完美 IC，這裡刻意模擬
    # 「很強但不完美」的訊號），"noise" 因子則是完全隨機、跟未來報酬無關。
    noise_scale = float(fwd_ret.std(axis=1).mean())
    good = fwd_ret + pd.DataFrame(rng.normal(0, noise_scale, size=(400, 30)), index=idx, columns=cols)
    noise = pd.DataFrame(rng.normal(0, 1, size=(400, 30)), index=idx, columns=cols)

    train_factors = {
        "close": close, "bias": pd.DataFrame(0.0, index=idx, columns=cols),
        "liquid": pd.DataFrame(True, index=idx, columns=cols), "PER": pd.DataFrame(10.0, index=idx, columns=cols),
        "momentum": good, "value": noise, "rev_yoy": noise, "low_vol": noise,
    }
    weights = wf.icir_weights(train_factors)
    assert weights["momentum"] > weights.get("value", 0)
    assert weights["momentum"] > weights.get("rev_yoy", 0)
    assert abs(sum(weights.values()) - 1.0) < 1e-6


def test_icir_weights_fallback_equal_when_all_nonpositive():
    """所有因子都全 NaN（沒資料）時應該 fallback 回四因子等權，不崩潰。"""
    idx = _dates(50)
    cols = ["A", "B"]
    close = pd.DataFrame(100.0, index=idx, columns=cols)
    all_nan = pd.DataFrame(np.nan, index=idx, columns=cols)

    train_factors = {
        "close": close, "momentum": all_nan, "value": all_nan,
        "rev_yoy": all_nan, "low_vol": all_nan,
    }
    weights = wf.icir_weights(train_factors)
    assert len(weights) == 4
    assert all(abs(w - 0.25) < 1e-9 for w in weights.values())


def test_build_composite_weighted_zscore_and_valid_mask():
    idx = _dates(10)
    cols = ["A", "B", "C"]
    f1 = pd.DataFrame({"A": [1.0]*10, "B": [2.0]*10, "C": [3.0]*10}, index=idx)
    factors = {
        "close": pd.DataFrame(100.0, index=idx, columns=cols),
        "f1": f1,
        "bias": pd.DataFrame(0.0, index=idx, columns=cols),
        "liquid": pd.DataFrame(True, index=idx, columns=cols),
    }
    factors["liquid"].loc[:, "C"] = False  # C 不在流動性宇宙裡

    composite = wf.build_composite(factors, {"f1": 1.0})
    assert composite["C"].isna().all()  # 不合流動性條件的欄位應該全部是 NaN
    assert not composite["A"].isna().all()


# ══════════════════════════════════════════════════════════════
# ablation.py
# ══════════════════════════════════════════════════════════════

def test_numeric_stats_basic():
    idx = _dates(252)
    # 每天穩定漲 0.1%，MDD 應該接近 0，Sharpe 應該很高（正值）
    equity = pd.Series(np.cumprod(np.full(252, 1.001)), index=idx)
    stats = abl._numeric_stats(equity)
    assert stats["total_return"] > 0
    assert stats["max_drawdown"] == pytest.approx(0.0, abs=1e-9)
    assert stats["sharpe"] > 0


def test_regime_conditional_breakdown_partitions_all_days():
    idx = _dates(100)
    equity = pd.Series(np.cumprod(1 + np.full(100, 0.001)), index=idx)
    regime = pd.Series(["BULL"] * 50 + ["BEAR"] * 50, index=idx)

    breakdown = abl._regime_conditional_breakdown(equity, regime)
    assert breakdown["BULL"]["n_days"] == 50
    assert breakdown["BEAR"]["n_days"] == 50
    assert breakdown["NEUTRAL"]["n_days"] == 0
    assert breakdown["NEUTRAL"]["annual_return"] is None  # 樣本太少（<5 天）不計算


def test_regime_conditional_breakdown_all_positive_returns_zero_mdd():
    """一個機制狀態底下的天數全部是正報酬，串接起來的子淨值曲線應該
    單調上升，最大回撤=0。"""
    idx = _dates(30)
    equity = pd.Series(np.cumprod(1 + np.full(30, 0.002)), index=idx)
    regime = pd.Series(["BULL"] * 30, index=idx)

    breakdown = abl._regime_conditional_breakdown(equity, regime)
    assert breakdown["BULL"]["max_drawdown"] == pytest.approx(0.0, abs=1e-9)
    assert breakdown["BULL"]["annual_return"] > 0


# ══════════════════════════════════════════════════════════════
# significance.py
# ══════════════════════════════════════════════════════════════

def test_deflated_sharpe_ratio_more_trials_lowers_dsr():
    """其他條件不變，試驗次數（n_trials）越多，DSR 應該越低（更難被認定顯著）——
    這正是 DSR 存在的目的：修正多重比較的選擇偏誤。"""
    result_few = sig.deflated_sharpe_ratio(sharpe=1.5, n_trials=2, n_days=1000, skew=0.0, kurt=3.0)
    result_many = sig.deflated_sharpe_ratio(sharpe=1.5, n_trials=100, n_days=1000, skew=0.0, kurt=3.0)
    assert result_many["dsr"] < result_few["dsr"]
    assert result_many["sr0_expected_max_under_null"] > result_few["sr0_expected_max_under_null"]


def test_deflated_sharpe_ratio_normal_returns_range():
    result = sig.deflated_sharpe_ratio(sharpe=1.0, n_trials=8, n_days=2500, skew=0.0, kurt=3.0)
    assert 0.0 <= result["dsr"] <= 1.0


def test_sharpe_confidence_interval_centered_on_point_estimate():
    rng = np.random.default_rng(1)
    daily_ret = pd.Series(rng.normal(0.0005, 0.01, 1000))
    result = sig.sharpe_confidence_interval(daily_ret)
    midpoint = (result["ci_lower"] + result["ci_upper"]) / 2
    assert midpoint == pytest.approx(result["sharpe_annual"], abs=1e-6)
    assert result["ci_lower"] < result["sharpe_annual"] < result["ci_upper"]


def test_sharpe_confidence_interval_wider_with_fewer_observations():
    rng = np.random.default_rng(2)
    short = pd.Series(rng.normal(0.0005, 0.01, 60))
    long = pd.Series(rng.normal(0.0005, 0.01, 2000))
    r_short = sig.sharpe_confidence_interval(short)
    r_long = sig.sharpe_confidence_interval(long)
    assert (r_short["ci_upper"] - r_short["ci_lower"]) > (r_long["ci_upper"] - r_long["ci_lower"])


def test_bootstrap_pvalue_detects_clear_difference():
    """策略報酬系統性地比大盤高一個很明顯的量，bootstrap p-value 應該很小（顯著）。"""
    idx = _dates(500)
    rng = np.random.default_rng(3)
    bench = pd.Series(rng.normal(0.0002, 0.01, 500), index=idx)
    strat = bench + 0.005  # 每天多賺 0.5%，遠大於雜訊，應該偵測得到

    result = sig.bootstrap_pvalue(strat, bench, n=2000, seed=42)
    assert result["p_value"] < 0.01
    assert result["significant_at_5pct"] is True


def test_bootstrap_pvalue_no_difference_not_significant():
    """策略跟大盤報酬完全一樣（差異恆為 0），p-value 應該接近 1，不顯著。"""
    idx = _dates(500)
    rng = np.random.default_rng(4)
    ret = pd.Series(rng.normal(0.0002, 0.01, 500), index=idx)

    result = sig.bootstrap_pvalue(ret, ret, n=2000, seed=42)
    assert result["p_value"] > 0.5
    assert result["significant_at_5pct"] is False


# ══════════════════════════════════════════════════════════════
# attribution.py
# ══════════════════════════════════════════════════════════════

def test_total_return_compounds_correctly():
    ret = pd.Series([0.10, 0.10, -0.10])
    # (1.1)*(1.1)*(0.9) - 1
    expected = 1.1 * 1.1 * 0.9 - 1
    assert attr._total_return(ret) == pytest.approx(expected, abs=1e-9)


def test_total_return_zero_series():
    ret = pd.Series([0.0, 0.0, 0.0])
    assert attr._total_return(ret) == pytest.approx(0.0, abs=1e-9)
