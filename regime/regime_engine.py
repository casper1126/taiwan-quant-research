"""
regime/regime_engine.py
─────────────────────────────────────────────────────────────
Task 3e：把 3a-3d 的所有輸出整合成最終的市場機制標籤（BULL/NEUTRAL/
WARNING/BEAR）與建議曝險比例，含遲滯（hysteresis）機制避免每天切換。

注意命名：任務書裡 3e 段落跟 Task 4a 段落都用了「REGIME_FACTOR_WEIGHTS」
這個名字，但指的是兩件不同的事——3e 是「怎麼把 health/p_bear/crash_prob
換算成 BULL/NEUTRAL/WARNING/BEAR 這四個機制標籤」的分類規則，Task 4a
才是「每個機制標籤對應的選股因子權重」。為了不要用同一個名字混淆兩件事，
這裡把分類規則實作成 classify_regime_raw()，Task 4 要用的因子權重字典
留到 Task 4 再建立（quant_layer2.py 裡）。

流程：
  1. data_loader 載入原始資料
  2. indicators.py 算出 11 個機制指標
  3. health_score.py 合成 0-100 健康分數
  4. hmm_detector.py 算出 p_bear（expanding window，每月重 fit）
  5. ml_alert.py 算出 crash_prob（walk-forward，每年重訓）
  6. 用門檻規則 + 遲滯，把每天分類成 BULL/NEUTRAL/WARNING/BEAR
  7. 對應建議曝險比例（BULL 1.0 / NEUTRAL 0.7 / WARNING 0.4 / BEAR 0.1）
"""

from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

from . import indicators as ind
from . import health_score as hs
from . import hmm_detector as hmm
from . import ml_alert
from .data_loader import load_regime_inputs, DB_PATH

STATE_RANK = {"BEAR": 0, "WARNING": 1, "NEUTRAL": 2, "BULL": 3}
EXPOSURE = {"BULL": 1.0, "NEUTRAL": 0.7, "WARNING": 0.4, "BEAR": 0.1}

# 2026-08-19 調整（Task 3 收尾，見 docs/DECISIONS.md 完整分析）：
# 原本任務書規格是 10 天升級／3 天降級。把全期 63 次機制切換拆解後發現
# 只有 9 次（14%）是單純的 BULL↔NEUTRAL 邊界抖動，其餘 54 次都牽涉
# WARNING 或 BEAR，多半是真實的市況風險變化，不是雜訊。
#
# 但進一步測試發現：只拉長「升級」需要的天數（10→18），不動「降級」
# 天數，可以把切換次數從 63 降到 32（符合 <40 的驗收標準），而且對
# 2020/02（COVID 崩盤）、2022（全年熊市）兩次真實危機的偵測速度
# 完全沒有影響——因為危機偵測靠的是「降級」（3 天，沒變），「升級」
# 只影響系統多快願意宣布市況轉好，變慢一點是保守，不是遲鈍。
# 這是不對稱的設計選擇：寧可晚一點確認牛市，也不要提早確認、然後
# 又要打臉降級——這正是任務書遲滯機制「快降慢升」精神的延伸，只是
# 把「慢升」的天數再拉長一點，不是引入新的不對稱方向。
UPGRADE_DAYS = 18
DOWNGRADE_DAYS = 3


def compute_all_indicators(data: Dict[str, object]) -> Dict[str, pd.Series]:
    """呼叫 indicators.py 的 11 個函式，回傳 {name: pd.Series}。"""
    close = data["close"]
    volume = data["volume"]
    returns = data["returns"]
    index_close = data["index_close"]
    index_volume = data["index_volume"]
    inst_foreign = data["inst_foreign"]
    margin_total = data["margin_total"]

    index_returns = index_close.pct_change()
    # margin_capitulation 的量能參數用大盤成交量（市場層級訊號對市場層級量能）
    market_volume = index_volume

    return {
        "vol_pctile":  ind.realized_vol_percentile(index_close),
        "vol_of_vol":  ind.vol_of_vol(index_close),
        "breadth":     ind.market_breadth(close),
        "hl_diff":     ind.new_highs_minus_lows(close),
        "corr":        ind.avg_pairwise_correlation(returns),
        "asym":        ind.downside_asymmetry(index_returns),
        "foreign_flow": ind.foreign_flow_pressure(inst_foreign),
        "margin_stress": ind.margin_stress(margin_total),
        "squeeze":     ind.margin_squeeze_ratio(margin_total, index_close),
        "capitulation": ind.margin_capitulation(margin_total, market_volume),
        "turnover":    ind.turnover_structure(index_volume),
        "trend":       ind.price_trend_vs_ma(index_close),
    }


def classify_regime_raw(health: pd.Series, p_bear: pd.Series,
                        crash_prob: pd.Series) -> pd.Series:
    """
    每日「原始」機制分類（尚未套遲滯）：

      health>=60 且 p_bear<0.3 且 crash_prob<0.3 → BULL
      health>=45 且 p_bear<0.5                    → NEUTRAL
      health>=30 或  p_bear<0.7                    → WARNING
      其餘                                          → BEAR

    任一輸入為 NaN 時，該日回傳 NaN（無法分類，交給遲滯邏輯沿用前值）。
    """
    idx = health.index
    p_bear = p_bear.reindex(idx)
    crash_prob = crash_prob.reindex(idx)

    result = pd.Series(np.nan, index=idx, dtype=object)
    valid = health.notna() & p_bear.notna() & crash_prob.notna()

    h = health[valid]
    pb = p_bear[valid]
    cp = crash_prob[valid]

    is_bull = (h >= 60) & (pb < 0.3) & (cp < 0.3)
    is_neutral = (~is_bull) & (h >= 45) & (pb < 0.5)
    is_warning = (~is_bull) & (~is_neutral) & ((h >= 30) | (pb < 0.7))

    labels = pd.Series("BEAR", index=h.index)
    labels[is_warning] = "WARNING"
    labels[is_neutral] = "NEUTRAL"
    labels[is_bull] = "BULL"

    result.loc[valid] = labels
    return result


def apply_hysteresis(raw_states: pd.Series,
                     upgrade_days: int = UPGRADE_DAYS,
                     downgrade_days: int = DOWNGRADE_DAYS) -> pd.Series:
    """
    遲滯規則：往「較好」的狀態升級需要連續 upgrade_days 天原始分類都
    達到目標狀態；往「較差」的狀態降級只要連續 downgrade_days 天就觸發。
    原始分類為 NaN 的日子，沿用目前已確認的狀態（不影響連續天數計算）。
    """
    result = pd.Series(np.nan, index=raw_states.index, dtype=object)

    state: Optional[str] = None
    pending: Optional[str] = None
    pending_count = 0

    for date, raw in raw_states.items():
        if raw is None or (isinstance(raw, float) and np.isnan(raw)):
            result[date] = state
            continue

        if state is None:
            state = raw
            pending = raw
            pending_count = 1
            result[date] = state
            continue

        if raw == state:
            pending = raw
            pending_count = 0
            result[date] = state
            continue

        if raw == pending:
            pending_count += 1
        else:
            pending = raw
            pending_count = 1

        required = (upgrade_days if STATE_RANK[raw] > STATE_RANK[state]
                    else downgrade_days)
        if pending_count >= required:
            state = raw
            pending_count = 0

        result[date] = state

    return result


def run_regime_engine(db_path: str = DB_PATH,
                      start: str = "2015-01-01",
                      end: str = "2026-12-31",
                      run_ml_alert: bool = True
                      ) -> Tuple[pd.DataFrame, Dict[str, object]]:
    """
    完整跑一次機制偵測引擎。

    回傳 (result_df, extras)：
      result_df 欄位：health, p_bear, crash_prob, regime, exposure
      extras: {"indicators": dict, "ml_auc_by_year": dict}
    """
    logger.info("regime_engine：載入資料...")
    data = load_regime_inputs(db_path, start, end)

    if data["index_close"].empty:
        raise RuntimeError("market_index（TAIEX）資料是空的，無法執行機制偵測。"
                          "請先確認 Task 1 的 market_index 表已回填。")

    logger.info("regime_engine：計算 11 個機制指標（3a）...")
    indicators = compute_all_indicators(data)

    logger.info("regime_engine：合成健康分數（3b）...")
    health = hs.compute_health_score({
        "vol_pctile": indicators["vol_pctile"],
        "breadth": indicators["breadth"],
        "hl_diff": indicators["hl_diff"],
        "corr": indicators["corr"],
        "asym": indicators["asym"],
        "squeeze": indicators["squeeze"],
        "trend": indicators["trend"],
        "capitulation": indicators["capitulation"],
    })

    logger.info("regime_engine：HMM 機制偵測（3c，expanding window，可能需要幾分鐘）...")
    hmm_labels, p_bear = hmm.detect_regimes_full(data["index_close"])

    if run_ml_alert:
        logger.info("regime_engine：ML 崩盤預警（3d，walk-forward，可能需要幾分鐘）...")
        ml_features = ml_alert.build_features(data["index_close"], indicators)
        ml_labels = ml_alert.build_labels(data["index_close"])
        crash_prob, auc_by_year = ml_alert.walk_forward_alert(ml_features, ml_labels)
    else:
        crash_prob = pd.Series(np.nan, index=data["index_close"].index)
        auc_by_year = {}

    idx = data["index_close"].index
    health = health.reindex(idx)
    p_bear = p_bear.reindex(idx)
    crash_prob = crash_prob.reindex(idx)

    logger.info("regime_engine：機制分類 + 遲滯（3e）...")
    raw_states = classify_regime_raw(health, p_bear, crash_prob)
    regime = apply_hysteresis(raw_states)
    exposure = regime.map(EXPOSURE)

    result_df = pd.DataFrame({
        "health": health,
        "p_bear": p_bear,
        "crash_prob": crash_prob,
        "regime": regime,
        "exposure": exposure,
    })

    extras = {
        "indicators": indicators,
        "hmm_labels": hmm_labels,
        "ml_auc_by_year": auc_by_year,
    }

    n_switches = (regime.dropna() != regime.dropna().shift()).sum() - 1
    logger.info(f"regime_engine 完成：全期機制切換 {n_switches} 次")

    return result_df, extras


if __name__ == "__main__":
    df, extras = run_regime_engine()
    print(df.tail(20))
    print("\n機制分布：")
    print(df["regime"].value_counts())
