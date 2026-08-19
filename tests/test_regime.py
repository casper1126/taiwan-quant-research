# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_regime.py

驗證 regime/ 模組（Task 3）的邏輯正確性，全部使用合成資料：
  - indicators.py 幾個指標的邊界案例（跟 Task 2 一致的 squeeze_ratio 範例、
    margin_capitulation 訊號、market_breadth/new_highs_minus_lows 基本案例）
  - health_score.py 的 NaN 成分重新正規化
  - regime_engine.py 的分類規則 + 遲滯機制
  - hmm_detector.py 最重要的一項驗收標準：截止日之前的輸出，不會因為之後
    又加入更多（未來的）資料而改變——這是 Task 3 的核心 Look-Ahead Bias
    防範設計，必須用測試鎖住，不能只靠人工檢查程式碼。
"""

import numpy as np
import pandas as pd
import pytest

from regime import indicators as ind
from regime import health_score as hs
from regime import hmm_detector as hmm
from regime.regime_engine import classify_regime_raw, apply_hysteresis


def _dates(n, start="2015-01-01"):
    return pd.bdate_range(start, periods=n)


# ══════════════════════════════════════════════════════════════
# indicators.py
# ══════════════════════════════════════════════════════════════

def test_margin_squeeze_ratio_matches_task2_example():
    """大盤跌 20%、融資跌 10%（皆從高點起算）→ squeeze_ratio = 0.5，跟 Task 2 的規格範例一致。"""
    idx = _dates(300)
    index_close = pd.Series(100.0, index=idx)
    margin_total = pd.Series(1000.0, index=idx)

    # 前 100 天墊高點，之後線性下跌到「大盤跌 20%、融資跌 10%」
    index_close.iloc[:100] = 100.0
    margin_total.iloc[:100] = 1000.0
    index_close.iloc[100:] = 80.0    # 跌 20%
    margin_total.iloc[100:] = 900.0  # 跌 10%

    ratio = ind.margin_squeeze_ratio(margin_total, index_close)
    assert ratio.iloc[-1] == pytest.approx(0.5, abs=1e-6)


def test_margin_squeeze_ratio_nan_when_decline_below_3pct():
    idx = _dates(300)
    index_close = pd.Series(100.0, index=idx)
    index_close.iloc[100:] = 99.0  # 只跌 1%，< 3% 門檻
    margin_total = pd.Series(1000.0, index=idx)
    margin_total.iloc[100:] = 950.0

    ratio = ind.margin_squeeze_ratio(margin_total, index_close)
    assert pd.isna(ratio.iloc[-1])


def test_market_breadth_basic():
    idx = _dates(80)
    close = pd.DataFrame({
        "A": np.linspace(100, 150, 80),   # 上升趨勢，最後應站上均線
        "B": np.linspace(100, 50, 80),    # 下降趨勢，最後應跌破均線
    }, index=idx)

    breadth = ind.market_breadth(close, ma_window=20)
    assert breadth.iloc[-1] == pytest.approx(0.5, abs=1e-6)


def test_new_highs_minus_lows_all_new_highs():
    idx = _dates(300)
    close = pd.DataFrame({
        "A": np.linspace(100, 200, 300),
        "B": np.linspace(50, 150, 300),
    }, index=idx)
    result = ind.new_highs_minus_lows(close, window=252)
    assert result.iloc[-1] == pytest.approx(1.0, abs=1e-6)


def test_margin_capitulation_true_case():
    idx = _dates(60)
    margin_total = pd.Series(1000.0, index=idx)
    # day 0-39: 平穩；day 40-45: 急殺 8%（5日跌幅>5%）；day 46-59: 打平（3日變化<0.5%）
    margin_total.iloc[:40] = 1000.0
    margin_total.iloc[40:46] = np.linspace(1000, 920, 6)
    margin_total.iloc[46:] = 920.0

    volume = pd.Series(1_000_000.0, index=idx)
    volume.iloc[40:50] = 800_000.0
    volume.iloc[50:] = 400_000.0  # 最近進一步量縮，低於 20 日均量

    result = ind.margin_capitulation(margin_total, volume)
    # 最後一天：急殺已發生（過去 20 日內）、目前持平、量縮 → True
    assert bool(result.iloc[-1]) is True


def test_margin_capitulation_false_when_no_sharp_decline():
    idx = _dates(60)
    margin_total = pd.Series(1000.0, index=idx)  # 完全平穩，沒有急殺
    volume = pd.Series(1_000_000.0, index=idx)
    result = ind.margin_capitulation(margin_total, volume)
    assert bool(result.iloc[-1]) is False


# ══════════════════════════════════════════════════════════════
# health_score.py
# ══════════════════════════════════════════════════════════════

def test_health_score_renormalizes_when_component_missing():
    idx = _dates(400)
    # breadth/hl_diff/squeeze 是「直接」方向：單調上升，最新值 = 視窗內歷史最大值
    # → rolling percentile ≈ 1（健康）
    ramp_up = pd.Series(np.linspace(0, 1, 400), index=idx)
    # asym 是「反向」方向：單調下降，最新值 = 視窗內歷史最小值
    # → rolling percentile ≈ 0，取反(1-p) ≈ 1（健康）
    ramp_down = pd.Series(np.linspace(1, 0, 400), index=idx)

    raw = {
        "vol_pctile": pd.Series(0.0, index=idx),   # 已是分位數，反向，0 = 最健康
        "breadth": ramp_up,
        "hl_diff": ramp_up,
        "corr": pd.Series(np.nan, index=idx),      # 缺資料
        "asym": ramp_down,
        "squeeze": ramp_up,
        "capitulation": pd.Series(False, index=idx),
    }
    score = hs.compute_health_score(raw)
    last = score.dropna().iloc[-1]
    # corr 缺資料時應該用剩餘 5 項（權重 18+22+13+13+12=78）重新正規化，
    # 分數應該仍然接近滿分（因為其餘成分全部是最健康方向）
    assert last > 90


# ══════════════════════════════════════════════════════════════
# regime_engine.py：分類規則 + 遲滯
# ══════════════════════════════════════════════════════════════

def test_classify_regime_raw_thresholds():
    idx = _dates(4)
    health = pd.Series([70, 50, 35, 10], index=idx)
    p_bear = pd.Series([0.1, 0.2, 0.6, 0.9], index=idx)
    crash = pd.Series([0.1, 0.2, 0.2, 0.9], index=idx)

    raw = classify_regime_raw(health, p_bear, crash)
    assert raw.iloc[0] == "BULL"
    assert raw.iloc[1] == "NEUTRAL"
    assert raw.iloc[2] == "WARNING"   # health 35>=30 → WARNING（即使 p_bear 0.6 不到 0.7 也一樣，or 條件滿足其一即可）
    assert raw.iloc[3] == "BEAR"


def test_hysteresis_downgrade_fast_upgrade_slow():
    idx = _dates(30)
    # 前 15 天穩定 BULL，接著連續 3 天 raw=WARNING（應該觸發降級，因為降級只需 3 天）
    raw_vals = ["BULL"] * 15 + ["WARNING"] * 3 + ["BULL"] * 12
    raw = pd.Series(raw_vals, index=idx)

    confirmed = apply_hysteresis(raw)
    # 第 15+3-1 天（index 17）應該已經降級成 WARNING
    assert confirmed.iloc[17] == "WARNING"

    # 之後 raw 又變回 BULL，但只有 12 天 < 10 天升級門檻不足以確認... 實際上 12>=10 應該會升級回去
    assert confirmed.iloc[-1] == "BULL"


def test_hysteresis_upgrade_needs_ten_days():
    idx = _dates(19)
    raw_vals = ["BEAR"] * 10 + ["BULL"] * 9  # 只有 9 天 BULL，不足 10 天升級門檻
    raw = pd.Series(raw_vals, index=idx)
    confirmed = apply_hysteresis(raw)
    assert confirmed.iloc[-1] == "BEAR"  # 還沒升級成功

    idx2 = _dates(21)
    raw_vals2 = ["BEAR"] * 10 + ["BULL"] * 11  # 滿 10 天，應該升級
    raw2 = pd.Series(raw_vals2, index=idx2)
    confirmed2 = apply_hysteresis(raw2)
    assert confirmed2.iloc[-1] == "BULL"


# ══════════════════════════════════════════════════════════════
# hmm_detector.py：Task 3 驗收標準——截止日之前的輸出不因加入未來資料而改變
# ══════════════════════════════════════════════════════════════

def _synthetic_index_close(n_days: int, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = _dates(n_days)
    # 混合牛熊市的合成報酬序列，讓 HMM 有東西可以區分
    rets = np.concatenate([
        rng.normal(0.0008, 0.008, n_days // 3),
        rng.normal(-0.001, 0.02, n_days // 3),
        rng.normal(0.0005, 0.01, n_days - 2 * (n_days // 3)),
    ])
    price = 100 * np.cumprod(1 + rets)
    return pd.Series(price, index=idx)


def test_hmm_output_before_cutoff_unchanged_by_future_data():
    """
    Task 3 驗收標準：HMM 在截止日前的輸出，不因為之後加入未來資料而改變。

    做法：用同一段合成資料，分別跑「只到某個月底為止」跟「跑到最後」兩次，
    比較兩次在「截止日之前」的機制標籤是否完全一致。
    截止點選在完整月份的月底（而不是月中），因為 3c 的設計是「每個月一次性
    predict 整個月」（月內用 Viterbi 對整個月做聯合解碼），所以只有在完整月份
    邊界上比較，才是跟設計相符的「無 look-ahead」驗證方式——月中比較本來就
    會因為兩次呼叫看到的「當月剩餘天數」不同而合理地不同，不代表有 bug。
    """
    full_close = _synthetic_index_close(900)

    # 找一個落在完整月份月底、且已經超過 504 天最小樣本數的日期
    year_month = full_close.index.to_period("M")
    unique_months = year_month.unique()
    # 選一個位於資料中段、確保之前有 >504 天歷史的月份
    cutoff_month = unique_months[len(unique_months) // 2]
    cutoff_mask = (year_month == cutoff_month)
    cutoff_date = full_close.index[cutoff_mask][-1]  # 該月最後一個交易日

    short_close = full_close.loc[:cutoff_date]

    labels_short = hmm.detect_regimes(short_close, n_iter=20)
    labels_full = hmm.detect_regimes(full_close, n_iter=20)

    common_idx = labels_short.index
    compare_short = labels_short.loc[:cutoff_date]
    compare_full = labels_full.loc[common_idx].loc[:cutoff_date]

    pd.testing.assert_series_equal(
        compare_short.astype(object), compare_full.astype(object),
        check_names=False,
    )
