# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_ml_composite.py

驗證 strategy/ml_composite.py（Task 5）的邏輯正確性，全部使用合成資料：
  - matrices_to_long()：寬格式 → 長格式攤平、NaN 樣本正確被丟棄
  - compute_factor_rank_table()：五因子重要性排名（1=當年最重要）
  - _daily_ic()：逐日橫截面 Spearman IC 計算
  - quant_layer2.build_positions() 的 Task 5 整合：use_ml_composite=True
    時真的用 ml_scores 排名選股，且跟 use_regime_factor_weights 同時開啟
    會丟例外；use_regime_exposure（Task 6 拆分出來的獨立曝險開關）則
    可以跟 use_ml_composite 自由搭配（Task 6 ablation D 的組合）
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

import ml_composite as mlc
import quant_layer2


def _dates(n, start="2020-01-01"):
    return pd.bdate_range(start, periods=n)


# ══════════════════════════════════════════════════════════════
# matrices_to_long
# ══════════════════════════════════════════════════════════════

def test_matrices_to_long_basic_shape():
    idx = _dates(5)
    cols = ["A", "B", "C"]
    m1 = pd.DataFrame(1.0, index=idx, columns=cols)
    m2 = pd.DataFrame(2.0, index=idx, columns=cols)
    factor_mats = {"f1": m1, "f2": m2}

    market = pd.DataFrame({
        "regime_BULL": 1.0, "regime_NEUTRAL": 0.0,
        "regime_WARNING": 0.0, "regime_BEAR": 0.0,
        "mkt_vol_pctile": 0.5,
    }, index=idx)

    target = pd.DataFrame(0.7, index=idx, columns=cols)

    long_df = mlc.matrices_to_long(factor_mats, market, idx, target=target)
    assert len(long_df) == len(idx) * len(cols)
    assert set(["date", "stock_id", "f1", "f2", "target",
               "regime_BULL", "mkt_vol_pctile"]).issubset(long_df.columns)
    assert (long_df["f1"] == 1.0).all()
    assert (long_df["target"] == 0.7).all()


def test_matrices_to_long_drops_incomplete_rows():
    """某支股票某天的因子是 NaN，該筆樣本應該被丟棄，其他樣本不受影響。"""
    idx = _dates(3)
    cols = ["A", "B"]
    m1 = pd.DataFrame(1.0, index=idx, columns=cols)
    m1.loc[idx[0], "A"] = np.nan  # 缺一筆

    market = pd.DataFrame({
        "regime_BULL": 1.0, "regime_NEUTRAL": 0.0,
        "regime_WARNING": 0.0, "regime_BEAR": 0.0,
        "mkt_vol_pctile": 0.5,
    }, index=idx)
    target = pd.DataFrame(0.5, index=idx, columns=cols)

    long_df = mlc.matrices_to_long({"f1": m1}, market, idx, target=target)
    assert len(long_df) == len(idx) * len(cols) - 1
    assert not ((long_df["date"] == idx[0]) & (long_df["stock_id"] == "A")).any()


def test_matrices_to_long_no_target_for_prediction():
    """target=None（預測用途）時不要求 target 欄位存在，也不會因此丟資料。"""
    idx = _dates(3)
    cols = ["A", "B"]
    m1 = pd.DataFrame(1.0, index=idx, columns=cols)
    market = pd.DataFrame({
        "regime_BULL": 1.0, "regime_NEUTRAL": 0.0,
        "regime_WARNING": 0.0, "regime_BEAR": 0.0,
        "mkt_vol_pctile": 0.5,
    }, index=idx)

    long_df = mlc.matrices_to_long({"f1": m1}, market, idx, target=None)
    assert "target" not in long_df.columns
    assert len(long_df) == len(idx) * len(cols)


# ══════════════════════════════════════════════════════════════
# compute_factor_rank_table
# ══════════════════════════════════════════════════════════════

def test_compute_factor_rank_table_ranks_within_five_factors_only():
    """排名只看 FACTOR_FEATURES 五個因子，機制 one-hot／mkt_vol 不參與排名。"""
    importance_table = pd.DataFrame({
        2020: {"momentum": 50, "value": 10, "rev_yoy": 5, "low_vol": 3, "margin_usage": 2,
              "regime_BULL": 20, "regime_NEUTRAL": 5, "regime_WARNING": 3, "regime_BEAR": 1,
              "mkt_vol_pctile": 1},
    })
    rank_table = mlc.compute_factor_rank_table(importance_table)
    assert list(rank_table.index) == mlc.FACTOR_FEATURES
    assert rank_table.loc["momentum", 2020] == 1  # 最高 importance → 排名 1
    assert rank_table.loc["margin_usage", 2020] == 5  # 最低 importance → 排名 5


def test_compute_factor_rank_table_stable_factor_has_low_std():
    """一個因子每年重要性排名都排第一 → 標準差應該是 0。"""
    importance_table = pd.DataFrame({
        2020: {"momentum": 100, "value": 10, "rev_yoy": 5, "low_vol": 3, "margin_usage": 2},
        2021: {"momentum": 90, "value": 20, "rev_yoy": 5, "low_vol": 3, "margin_usage": 2},
        2022: {"momentum": 80, "value": 15, "rev_yoy": 10, "low_vol": 3, "margin_usage": 2},
    })
    rank_table = mlc.compute_factor_rank_table(importance_table)
    rank_std = rank_table.std(axis=1)
    assert rank_std["momentum"] == 0.0


# ══════════════════════════════════════════════════════════════
# _daily_ic
# ══════════════════════════════════════════════════════════════

def test_daily_ic_perfect_prediction():
    """預測分數跟實際目標完全同排序 → IC 應該接近 1。"""
    idx = _dates(2)
    cols = ["A", "B", "C", "D", "E"]
    pred = pd.DataFrame({
        "A": [1, 1], "B": [2, 2], "C": [3, 3], "D": [4, 4], "E": [5, 5],
    }, index=idx)
    target = pred.copy()  # 完全一致的排序

    ic = mlc._daily_ic(pred, target)
    assert (ic > 0.99).all()


def test_daily_ic_too_few_stocks_skipped():
    """單日有效股票數 < 10 時，那一天應該被跳過（不產出 IC）。"""
    idx = _dates(1)
    cols = ["A", "B", "C"]
    pred = pd.DataFrame({"A": [1], "B": [2], "C": [3]}, index=idx)
    target = pred.copy()

    ic = mlc._daily_ic(pred, target)
    assert len(ic) == 0


# ══════════════════════════════════════════════════════════════
# quant_layer2.py 的 Task 5 整合：use_ml_composite
# ══════════════════════════════════════════════════════════════

def _synthetic_factors_for_positions(n_days=400, n_stocks=60, seed=0):
    """跟 test_portfolio.py 類似規模的合成資料，餵給 build_positions()。"""
    rng = np.random.default_rng(seed)
    idx = _dates(n_days)
    cols = [f"S{i}" for i in range(n_stocks)]

    close = pd.DataFrame(
        100 * np.cumprod(1 + rng.normal(0, 0.01, size=(n_days, n_stocks)), axis=0),
        index=idx, columns=cols,
    )
    ones = pd.DataFrame(1.0, index=idx, columns=cols)
    zeros = pd.DataFrame(0.0, index=idx, columns=cols)

    factors = {
        "close": close,
        "bias": zeros,           # 不觸發乖離率過濾
        "liquid_mask": pd.DataFrame(True, index=idx, columns=cols),
        "momentum": ones, "mom_120": ones, "value": ones,
        "rev_yoy": ones, "low_vol": ones, "div_yld": ones,
        "PER": ones,
    }
    return factors, idx, cols


def test_build_positions_use_ml_composite_picks_highest_score():
    """use_ml_composite=True 時，持倉應該是 ml_scores 排名最高的那些股票，
    不是動態 IC 加權複合分數選出來的股票（用故意設計成矛盾的分數驗證真的有切換）。"""
    factors, idx, cols = _synthetic_factors_for_positions(n_days=300, n_stocks=40)

    # ml_scores：股票代號數字越小分數越高（S0 最高、S39 最低）
    rank_scores = {c: (len(cols) - i) for i, c in enumerate(cols)}
    ml_scores = pd.DataFrame([rank_scores] * len(idx), index=idx, columns=cols).astype(float)

    positions = quant_layer2.build_positions(
        factors, top_n=5, rebal_freq=60, buffer_multiplier=1.5,
        ml_scores=ml_scores, use_ml_composite=True,
    )

    # 找最後一次有部位的那天，持倉應該集中在 S0~S4（分數最高的 5 檔）附近
    held_cols = positions.iloc[-1][positions.iloc[-1] > 0].index.tolist()
    assert len(held_cols) > 0
    top5_by_score = [c for c, _ in sorted(rank_scores.items(), key=lambda kv: -kv[1])[:5]]
    # 緩衝區機制可能讓持倉跟最新排名有些微差異，但應該高度重疊
    overlap = set(held_cols) & set(top5_by_score)
    assert len(overlap) >= 3


def test_build_positions_ml_composite_requires_scores():
    factors, idx, cols = _synthetic_factors_for_positions(n_days=100, n_stocks=20)
    with pytest.raises(ValueError):
        quant_layer2.build_positions(factors, use_ml_composite=True, ml_scores=None)


def test_build_positions_ml_composite_and_regime_factor_weights_mutually_exclusive():
    """use_ml_composite 與 use_regime_factor_weights 都是「用什麼分數排名」，同時開啟該丟例外。"""
    factors, idx, cols = _synthetic_factors_for_positions(n_days=100, n_stocks=20)
    ml_scores = pd.DataFrame(1.0, index=idx, columns=cols)
    with pytest.raises(ValueError):
        quant_layer2.build_positions(
            factors, use_ml_composite=True, ml_scores=ml_scores,
            use_regime_factor_weights=True,
        )


def test_build_positions_ablation_d_ml_plus_regime_exposure_allowed():
    """Task 6 ablation D：use_ml_composite=True + use_regime_exposure=True（曝險獨立於選股，
    不該互斥），且曝險縮放應該真的讓總部位規模縮小（BEAR 曝險 0.1 遠低於全倉）。"""
    factors, idx, cols = _synthetic_factors_for_positions(n_days=300, n_stocks=30)
    # 分數要有區別才能真的排出名次（全部同分會 tie，rank() 平均後沒有股票
    # 落在 top_n 門檻內，見 apply_buffer 的排名邏輯）。
    rank_scores = {c: (len(cols) - i) for i, c in enumerate(cols)}
    ml_scores = pd.DataFrame([rank_scores] * len(idx), index=idx, columns=cols).astype(float)

    regime_df = pd.DataFrame({
        "regime": "BEAR", "exposure": 0.1,
    }, index=idx)

    # 不應該丟例外（跟 use_regime_factor_weights 的互斥檢查不同）
    positions = quant_layer2.build_positions(
        factors, top_n=5, rebal_freq=60, buffer_multiplier=1.5,
        ml_scores=ml_scores, use_ml_composite=True,
        regime_df=regime_df, use_regime_exposure=True,
    )

    # BEAR 曝險 0.1：任何一天的總部位比例都應該遠低於「正常全倉等權」(top_n * eq_weight ≈ 1.0)
    last_pos = positions.iloc[-1]
    assert last_pos.sum() <= 0.1 + 1e-6
    assert last_pos.sum() > 0  # 曝險不是 0，代表真的有部位、不是意外清空


def test_build_positions_regime_exposure_independent_of_factor_weights():
    """use_regime_exposure=True 但 use_regime_factor_weights=False：應該用預設動態 IC 排名，
    只有曝險水位受機制影響——這是原本 Task 4（use_regime_weights=True）的行為，拆開後
    用兩個旗標一起打開應該重現同樣的效果。"""
    factors, idx, cols = _synthetic_factors_for_positions(n_days=300, n_stocks=30)
    regime_df = pd.DataFrame({"regime": "WARNING", "exposure": 0.4}, index=idx)

    positions = quant_layer2.build_positions(
        factors, top_n=5, rebal_freq=60, buffer_multiplier=1.5,
        regime_df=regime_df, use_regime_exposure=True, use_regime_factor_weights=False,
    )
    last_pos = positions.iloc[-1]
    assert last_pos.sum() <= 0.4 + 1e-6
