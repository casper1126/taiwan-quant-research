# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_task7.py

驗證 Task 7（研究誠信模組）三個子模組的純邏輯函式，全部用合成資料：
  - data_pipeline/survivorship.py：compute_stale_candidates()（用臨時
    SQLite）、cross_check_against_finmind()
  - regime/decay_monitor.py：compute_ic()、compute_rolling_ic()、
    compute_decay_table()、latest_alerts()
  - strategy/capacity.py：detect_rebalance_dates()、compute_adv()、
    compute_capacity_at_rebalance()

完整的三份真實報告/圖（survivorship_analysis.md／factor_decay_history.png／
capacity_analysis.md）已經用 `python <module>.py` 真實跑過一次，結果記錄
在 docs/DECISIONS.md，這裡不重複跑一次真實資料（太慢），只驗證邏輯正確性。
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))
sys.path.insert(0, str(Path(__file__).parent.parent / "regime"))
sys.path.insert(0, str(Path(__file__).parent.parent / "data_pipeline"))

import capacity as cap
import decay_monitor as dm
import survivorship as sv


def _dates(n, start="2020-01-01"):
    return pd.bdate_range(start, periods=n)


# ══════════════════════════════════════════════════════════════
# survivorship.py
# ══════════════════════════════════════════════════════════════

def _make_temp_db(rows):
    """rows: list of (stock_id, date, close, volume) tuples。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = sqlite3.connect(tmp.name)
    conn.execute("""CREATE TABLE daily_price (
        date TEXT, stock_id TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER)""")
    conn.executemany(
        "INSERT INTO daily_price (date, stock_id, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
        [(d, s, c, c, c, c, v) for s, d, c, v in rows],
    )
    conn.commit()
    conn.close()
    return tmp.name


def test_compute_stale_candidates_detects_stopped_stock():
    rows = []
    # 股票 A：一路更新到最新日
    for d in pd.bdate_range("2020-01-01", "2020-06-30"):
        rows.append(("A", d.strftime("%Y-%m-%d"), 100.0, 1000))
    # 股票 B：只更新到 2020-02-01 就停了（明顯 stale）
    for d in pd.bdate_range("2020-01-01", "2020-02-01"):
        rows.append(("B", d.strftime("%Y-%m-%d"), 50.0, 500))

    db_path = _make_temp_db(rows)
    stale = sv.compute_stale_candidates(db_path, start="2020-01-01", end="2020-06-30",
                                        stale_threshold_days=30)
    assert "B" in set(stale["stock_id"])
    assert "A" not in set(stale["stock_id"])


def test_cross_check_against_finmind_flags_correctly():
    stale = pd.DataFrame({"stock_id": ["A", "B", "C"], "days_stale": [200, 300, 400]})
    finmind_universe = pd.DataFrame({"stock_id": ["A", "X", "Y"]})  # 只有 A 還在現行清單

    result = sv.cross_check_against_finmind(stale, finmind_universe)
    assert result.set_index("stock_id")["still_in_finmind_current_list"]["A"] == True
    assert result.set_index("stock_id")["still_in_finmind_current_list"]["B"] == False
    assert result.set_index("stock_id")["still_in_finmind_current_list"]["C"] == False


def test_cross_check_against_finmind_empty_universe():
    stale = pd.DataFrame({"stock_id": ["A"], "days_stale": [200]})
    result = sv.cross_check_against_finmind(stale, pd.DataFrame())
    assert result["still_in_finmind_current_list"].iloc[0] == False


# ══════════════════════════════════════════════════════════════
# decay_monitor.py
# ══════════════════════════════════════════════════════════════

def test_compute_ic_perfect_correlation():
    idx = _dates(5)
    cols = ["A", "B", "C", "D", "E"]
    factor = pd.DataFrame({c: [i] * 5 for i, c in enumerate(cols)}, index=idx)
    fwd_ret = factor.copy()  # 完全同排序

    ic = dm.compute_ic(factor, fwd_ret, min_stocks=3)
    assert (ic > 0.99).all()


def test_compute_rolling_ic_shape_matches_factor_index():
    idx = _dates(400)
    cols = [f"S{i}" for i in range(20)]
    rng = np.random.default_rng(0)
    factor = pd.DataFrame(rng.normal(0, 1, size=(400, 20)), index=idx, columns=cols)
    fwd_ret = pd.DataFrame(rng.normal(0, 1, size=(400, 20)), index=idx, columns=cols)

    rolling_ic = dm.compute_rolling_ic(factor, fwd_ret, window=60, min_periods=20)
    assert len(rolling_ic) == len(idx)
    assert rolling_ic.dropna().abs().le(1.0 + 1e-9).all()


def test_compute_decay_table_flags_when_recent_ic_drops():
    idx = _dates(300)
    # 前 200 天 IC 穩定在 0.2，後 100 天掉到 0.05（明顯衰退）
    rolling_ic = pd.Series([0.2] * 200 + [0.05] * 100, index=idx)

    decay_table = dm.compute_decay_table(rolling_ic, threshold_ratio=0.5)
    # 後段應該被標記衰退（0.05 < 0.5 * 長期均值，長期均值會被前段 0.2 拉高）
    assert decay_table["decayed"].iloc[-1] == True
    # 前段（還沒有衰退前）不該被標記
    assert decay_table["decayed"].iloc[100] == False


def test_compute_decay_table_no_flag_when_long_term_mean_nonpositive():
    idx = _dates(300)
    rolling_ic = pd.Series(-0.1, index=idx)  # 長期均值是負的，不該判定「衰退」
    decay_table = dm.compute_decay_table(rolling_ic)
    assert not decay_table["decayed"].any()


def test_latest_alerts_reports_last_valid_row():
    idx = _dates(10)
    table = pd.DataFrame({
        "recent_ic": [0.1] * 8 + [np.nan, np.nan],
        "long_term_mean": [0.2] * 8 + [np.nan, np.nan],
        "decayed": [True] * 8 + [False, False],
    }, index=idx)
    alerts = dm.latest_alerts({"momentum": table})
    assert alerts["momentum"]["decayed"] is True
    assert alerts["momentum"]["recent_ic"] == pytest.approx(0.1)


def test_latest_alerts_handles_all_nan_factor():
    idx = _dates(5)
    table = pd.DataFrame({"recent_ic": [np.nan]*5, "long_term_mean": [np.nan]*5, "decayed": [False]*5}, index=idx)
    alerts = dm.latest_alerts({"empty_factor": table})
    assert alerts["empty_factor"]["decayed"] is None


# ══════════════════════════════════════════════════════════════
# capacity.py
# ══════════════════════════════════════════════════════════════

def test_detect_rebalance_dates_only_flags_real_changes():
    idx = _dates(10)
    cols = ["A", "B"]
    pos = pd.DataFrame(0.0, index=idx, columns=cols)
    pos.loc[idx[2]:, "A"] = 0.5   # day 2 開始有部位
    pos.loc[idx[6]:, "B"] = 0.5   # day 6 加碼 B

    rebal_dates = cap.detect_rebalance_dates(pos)
    assert idx[2] in rebal_dates
    assert idx[6] in rebal_dates
    assert idx[4] not in rebal_dates  # 中間沒變化的日子不該被偵測到


def test_compute_adv_basic():
    idx = _dates(30)
    cols = ["A"]
    close = pd.DataFrame({"A": [100.0] * 30}, index=idx)
    volume = pd.DataFrame({"A": [1000.0] * 30}, index=idx)
    adv = cap.compute_adv(close, volume, window=10)
    # 穩定值的滾動均值應該收斂到 100*1000 = 100000
    assert adv["A"].iloc[-1] == pytest.approx(100_000.0)


def test_compute_capacity_binding_stock_is_least_liquid():
    idx = _dates(5)
    cols = ["LIQUID", "ILLIQUID"]
    pos = pd.DataFrame(0.0, index=idx, columns=cols)
    pos.loc[idx[2], ["LIQUID", "ILLIQUID"]] = [0.5, 0.5]  # 均分權重

    adv = pd.DataFrame({"LIQUID": [10_000_000.0]*5, "ILLIQUID": [100_000.0]*5}, index=idx)

    result = cap.compute_capacity_at_rebalance(pos, adv, [idx[2]], adv_pct_limit=0.05)
    assert result.loc[idx[2], "binding_stock"] == "ILLIQUID"
    # ILLIQUID 隱含的組合資金上限 = 0.05*100000/0.5 = 10000
    assert result.loc[idx[2], "max_capital_ntd"] == pytest.approx(10_000.0)


def test_compute_capacity_empty_when_no_holdings():
    idx = _dates(3)
    cols = ["A"]
    pos = pd.DataFrame(0.0, index=idx, columns=cols)
    adv = pd.DataFrame({"A": [1000.0]*3}, index=idx)
    result = cap.compute_capacity_at_rebalance(pos, adv, [idx[1]])
    assert result.empty
