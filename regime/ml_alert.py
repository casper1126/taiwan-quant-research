"""
regime/ml_alert.py
─────────────────────────────────────────────────────────────
Task 3d：LightGBM 崩盤預警分類器。

特徵：3a 的 11 個指標 + 大盤 5/20/60 日報酬
標籤：未來 20 日 TAIEX 報酬 < -5%（二元分類）
訓練：Walk-Forward，每年重訓（訓練 [起點, y-1] → 預測 y），
      class_weight='balanced'（崩盤是稀有事件，正負樣本嚴重不平衡）
輸出：crash_prob（每日崩盤機率）+ 每個 fold 的 AUC 存到
      reports/ml_alert_auc.json
"""

import json
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from loguru import logger
from sklearn.metrics import roc_auc_score

FUTURE_WINDOW = 20
CRASH_THRESHOLD = -0.05
# 2026-08-19 資料範圍延伸到 2012-05-02 後重新校準（見 docs/DATA_AVAILABILITY.md）：
# 2012（部分年，5-12月）17 個崩盤標籤、2013 年 7 個，單一年份都不到 20 筆的
# 訓練門檻，但合併 2012+2013 有 24 筆，足夠訓練後預測 2014。真正限制整體
# 機制輸出起點的是 hmm_detector.py 的 504 天最小歷史窗口（資料從 2012-05-02
# 起算，約落在 2014 年中），這裡不需要比它更保守，設在 2014 剛好貼齊。
FIRST_TEST_YEAR = 2014


def build_features(index_close: pd.Series, indicators: Dict[str, pd.Series]) -> pd.DataFrame:
    """
    組合特徵矩陣：3a 的 11 個指標 + 大盤 5/20/60 日報酬。

    indicators：{name: pd.Series}，key 至少包含 regime_engine.py 算出的
    11 個 3a 指標（缺的會被忽略，不強制要求全部存在，方便單元測試用
    合成資料只放幾個指標進來測）。
    """
    feat = pd.DataFrame(index=index_close.index)
    for name, series in indicators.items():
        # margin_capitulation 等布林指標會用 object dtype 存 True/False/NaN，
        # LightGBM 只吃 int/float/bool，這裡統一轉成 float（True→1.0, False→0.0）
        feat[name] = pd.to_numeric(series.reindex(feat.index), errors="coerce")

    feat["mkt_ret_5"] = index_close.pct_change(5)
    feat["mkt_ret_20"] = index_close.pct_change(20)
    feat["mkt_ret_60"] = index_close.pct_change(60)

    return feat


def build_labels(index_close: pd.Series, window: int = FUTURE_WINDOW,
                 threshold: float = CRASH_THRESHOLD) -> pd.Series:
    """未來 window 日 TAIEX 報酬 < threshold → 1（崩盤），否則 0。"""
    fwd_ret = index_close.pct_change(window).shift(-window)
    label = (fwd_ret < threshold).astype(float)
    label[fwd_ret.isna()] = np.nan
    return label


def walk_forward_alert(features: pd.DataFrame, labels: pd.Series,
                       first_test_year: int = FIRST_TEST_YEAR,
                       auc_report_path: str = "reports/ml_alert_auc.json"
                       ) -> Tuple[pd.Series, Dict[str, float]]:
    """
    每年重訓一次的 Walk-Forward LightGBM 崩盤預警。

    訓練集：[資料起點, 該年 1/1 之前]（expanding window）
    測試集：該年一整年
    只要訓練集裡正樣本（崩盤日）數量太少（<20）就跳過該年，不勉強訓練
    一個沒有意義的模型。

    回傳：
      crash_prob : pd.Series(index=date)，OOS 崩盤機率（只在有效測試年份有值）
      auc_by_year: {year: auc}，同時存成 JSON 到 auc_report_path
    """
    common_idx = features.index.intersection(labels.dropna().index)
    features = features.loc[common_idx]
    labels = labels.loc[common_idx]

    years = sorted(set(features.index.year))
    test_years = [y for y in years if y >= first_test_year]

    crash_prob = pd.Series(np.nan, index=features.index)
    auc_by_year: Dict[str, float] = {}

    for y in test_years:
        train_mask = features.index.year < y
        test_mask = features.index.year == y

        X_train, y_train = features.loc[train_mask], labels.loc[train_mask]
        X_test, y_test = features.loc[test_mask], labels.loc[test_mask]

        if X_train.empty or X_test.empty:
            continue
        n_pos = int(y_train.sum())
        if n_pos < 20 or n_pos == len(y_train):
            logger.warning(f"ml_alert {y} 年：訓練集正樣本只有 {n_pos} 筆，樣本不足，跳過")
            continue

        model = LGBMClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            class_weight="balanced",
            random_state=42,
            verbosity=-1,
        )
        model.fit(X_train, y_train)

        proba = model.predict_proba(X_test)[:, 1]
        crash_prob.loc[test_mask] = proba

        if y_test.nunique() >= 2:
            auc = roc_auc_score(y_test, proba)
            auc_by_year[str(y)] = round(float(auc), 4)
        else:
            auc_by_year[str(y)] = None
            logger.warning(f"ml_alert {y} 年：測試集只有單一類別，AUC 無法計算")

    Path(auc_report_path).parent.mkdir(parents=True, exist_ok=True)
    with open(auc_report_path, "w", encoding="utf-8") as f:
        json.dump(auc_by_year, f, ensure_ascii=False, indent=2)
    logger.info(f"ml_alert 各年 AUC 已存到 {auc_report_path}: {auc_by_year}")

    return crash_prob, auc_by_year
