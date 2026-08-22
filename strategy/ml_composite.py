"""
strategy/ml_composite.py
─────────────────────────────────────────────────────────────
Task 5：ML 因子合成。

用 LightGBM 迴歸取代 Task 2 的線性 IC 加權／Task 4 的機制靜態權重，
直接學習「5 個核心因子（momentum/value/rev_yoy/low_vol/margin_usage）
+ 機制 one-hot + 大盤波動分位」與「個股未來 20 日報酬橫截面分位數」
之間可能存在的非線性/條件式關係。

為什麼刻意換一批完全不同的樣本與模型（不是同一份資料重測一次）：
Task 2 的四輪 IC 排查已經證明 inst_flow／margin_usage 在「流動性前
300 檔＋線性排名 IC」的方法下沒有訊號（見 reports/factor_negative_findings.md）。
這裡刻意（1）用全市場所有股票（不限流動性前 300、不限最終選中的
30 檔），（2）換一個能抓非線性/條件式關係的模型類別（LightGBM 迴歸），
才是真正獨立的第二次檢驗——用同一份資料、同一種線性方法再測一次，
就算換了因子名稱也只是自我安慰。

訓練方式：expanding window，每年 1/1 重新訓練一次，訓練資料是該年
以前的「全部」歷史（不能看到當年或未來的資料）。訓練樣本涵蓋當時
全市場所有有效股票，不是只用最終回測選中的 30 檔。

跟 regime/、quant_layer2.py 的依賴方向：
  ml_composite.py 借用 quant_layer2.py 的資料載入（load_matrices）與
  factors/ 因子函式（跟 quant_layer2.py 自己算複合分數用的是同一套
  因子定義，只是不套用流動性遮罩），也借用 regime/ 的機制標籤與
  大盤波動分位指標。quant_layer2.py 對這個檔案的依賴僅限於「接受
  外部已經算好的 ml_scores 矩陣」（build_positions() 的新參數），
  不會 import 這個檔案本身的內部函式——維持 Task 3-4 一開始定案的
  單向依賴精神（regime/ 不依賴 quant_layer2.py；這裡反過來，
  ml_composite.py 依賴 quant_layer2.py + regime/，但兩者都不會反向
  依賴 ml_composite.py）。

直接執行：python strategy/ml_composite.py
（會跑完整 walk-forward 訓練＋輸出三份報告，需要幾分鐘到十幾分鐘。）
"""

import sys
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import quant_layer2
from factors import style as factor_style
from factors import taiwan as factor_taiwan
from regime import regime_engine

# ══════════════════════════════════════════════════════════════
# 設定
# ══════════════════════════════════════════════════════════════
DB_PATH    = quant_layer2.DB_PATH
START_DATE = "2013-01-01"          # 比 quant_layer2 早，給動能/營收/機制暖機空間
END_DATE   = quant_layer2.END_DATE  # 跟 quant_layer2/regime 一致，方便互相比較

FWD_DAYS = 20

# 五個核心因子（含 margin_usage）——Task 2 已排除 inst_flow（見上方模組說明），
# 這裡延續同一個決定，不重新把 inst_flow 加回來（那不是 Task 5 要重新驗證的
# 對象，Task 5 驗證的是「這五個因子能不能用非線性方法合成得更好」）。
FACTOR_FEATURES = ["momentum", "value", "rev_yoy", "low_vol", "margin_usage"]
REGIME_STATES = ["BULL", "NEUTRAL", "WARNING", "BEAR"]
REGIME_FEATURES = [f"regime_{s}" for s in REGIME_STATES]
MARKET_FEATURES = ["mkt_vol_pctile"]
ALL_FEATURES = FACTOR_FEATURES + REGIME_FEATURES + MARKET_FEATURES

# 訓練樣本抽樣頻率：全市場每天攤平成長格式的成本很高（~2000 檔 × 每年
# ~250 天），且連續交易日的橫截面特徵高度自相關（滾動窗口因子），
# 逐日全收沒有帶來等比例的資訊量，只會讓運算時間隨年份線性膨脹。
# 這裡改成每 5 個交易日取一天做訓練樣本（預測仍然逐日進行，不受影響），
# 是計算量的工程取捨，不是為了湊結果而挑資料——訓練/驗證切分、
# early stopping、最終逐日預測全部維持不變。
TRAIN_DATE_STRIDE = 5
MIN_TRAIN_DAYS = 200     # 抽樣後至少要有這麼多訓練日才開始 walk-forward
VALID_FRAC = 0.15        # 訓練集尾端切一部分（照時間序，非隨機）當 early stopping 驗證集

N_ESTIMATORS = 500
MAX_DEPTH = 5
LEARNING_RATE = 0.01
EARLY_STOPPING_ROUNDS = 50

REPORT_DIR = Path("reports")


# ══════════════════════════════════════════════════════════════
# PART 1  特徵矩陣建構（全市場，不套流動性遮罩）
# ══════════════════════════════════════════════════════════════

def build_factor_matrices(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """
    五個核心因子的寬格式矩陣（date x stock_id），全市場、不套流動性遮罩。

    刻意不呼叫 quant_layer2.build_factors()：那個函式會先做「日均成交金額
    前 300」的流動性篩選再算因子，Task 5 明確要求用全市場所有股票的歷史
    因子分數訓練，所以這裡直接呼叫 factors/ 模組的因子函式，跳過流動性
    篩選那一層。
    """
    momentum = factor_style.momentum_52w(data)
    value = factor_style.value_composite(data)
    low_vol = factor_style.low_vol_ivol(data)
    rev_yoy = quant_layer2.build_rev_yoy(data["revenue"], data["close"].index)
    margin_usage = factor_taiwan.margin_usage(data)

    return {
        "momentum": momentum,
        "value": value,
        "rev_yoy": rev_yoy,
        "low_vol": low_vol,
        "margin_usage": margin_usage,
    }


def build_target(close: pd.DataFrame, fwd_days: int = FWD_DAYS) -> pd.DataFrame:
    """標籤：個股未來 fwd_days 日報酬的橫截面分位數（0-1，全市場排名）。"""
    fwd_ret = close.pct_change(fwd_days).shift(-fwd_days)
    return fwd_ret.rank(axis=1, pct=True)


def _market_features_from_regime_output(regime_df: pd.DataFrame, extras: dict,
                                         close_index: pd.DatetimeIndex) -> pd.DataFrame:
    """把 regime_engine.run_regime_engine() 的輸出轉成機制 one-hot（4 欄）＋
    大盤波動分位（1 欄），reindex 到 close_index。抽成獨立函式是因為
    Task 6（strategy/ablation.py）需要跟這裡用「同一次」regime_engine 執行
    結果（不重跑一次——regime_engine 內部的 LightGBM 崩盤預警在不同次執行
    間有輕微不確定性，見 docs/PROJECT_STATUS.md 的環境注意事項，同一份
    market_features 才能保證 ablation 的 D 版跟其他版本用的是同一個機制
    標籤序列，不是兩份微幅不同的版本）。

    機制尚未暖機完成（regime 為 NaN，見 regime/hmm_detector.py 的 504 天
    門檻）的早期日期，one-hot 全部填 0——這代表「當下沒有機制資訊可用」，
    不是猜一個特定狀態，跟 quant_layer2.py 的 _regime_state_and_exposure()
    fallback 到 NEUTRAL 是不同的處理：這裡是特徵輸入，讓模型自己學會
    「這個情況下沒有機制資訊」，而不是餵一個可能誤導模型的假訊號。
    """
    one_hot = pd.get_dummies(regime_df["regime"]).reindex(columns=REGIME_STATES, fill_value=0.0)
    one_hot = one_hot.reindex(close_index).fillna(0.0)
    one_hot.columns = [f"regime_{c}" for c in one_hot.columns]

    mkt_vol_pctile = extras["indicators"]["vol_pctile"].reindex(close_index)

    market_features = one_hot
    market_features["mkt_vol_pctile"] = mkt_vol_pctile
    return market_features


def build_market_features(close_index: pd.DatetimeIndex,
                          start: str = "2012-05-02",
                          end: str = END_DATE) -> pd.DataFrame:
    """
    機制 one-hot（4 欄）＋大盤波動分位（1 欄），reindex 到 close_index。

    機制標籤來自完整跑一次 regime/regime_engine.py（含 HMM／health
    score／ml_alert，需要幾分鐘）。大盤波動分位直接重用
    regime/indicators.py 的 realized_vol_percentile 輸出（跟健康分數用
    的是同一個指標，避免重算兩次不一致的版本）。
    """
    logger.info("ml_composite：跑 regime_engine 取得機制標籤 + 大盤波動分位（可能需要幾分鐘）...")
    regime_df, extras = regime_engine.run_regime_engine(start=start, end=end, run_ml_alert=True)
    return _market_features_from_regime_output(regime_df, extras, close_index)


# ══════════════════════════════════════════════════════════════
# PART 2  寬格式 → 長格式（訓練/預測用的 (date, stock) 樣本表）
# ══════════════════════════════════════════════════════════════

def matrices_to_long(factor_mats: Dict[str, pd.DataFrame],
                     market_features: pd.DataFrame,
                     dates: pd.DatetimeIndex,
                     target: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    把寬格式矩陣攤平成長格式：欄位 = [date, stock_id, 因子們..., 機制 one-hot,
    大盤波動分位, (target)]。只保留 ALL_FEATURES（+ target，若有給）全部
    非 NaN 的樣本——LightGBM 雖然能吃 NaN，但這裡選擇明確丟棄不完整樣本，
    避免模型學到「缺值本身」這種跟資料完整度而非市場訊號有關的模式。
    """
    # pandas>=2.1 的 stack() 預設（future_stack）已經不會為 NaN 產生額外的列，
    # 行為等同舊版的 dropna=False，這裡不用也不能再傳 dropna 參數（pandas 3.0
    # 已移除這個關鍵字，強制用新版行為）。
    frames = {name: m.reindex(index=dates).stack() for name, m in factor_mats.items()}
    if target is not None:
        frames["target"] = target.reindex(index=dates).stack()

    long_df = pd.DataFrame(frames)
    long_df.index.names = ["date", "stock_id"]
    long_df = long_df.reset_index()

    mf = market_features.reindex(dates).reset_index().rename(columns={"index": "date"})
    long_df = long_df.merge(mf, on="date", how="left")

    required_cols = list(factor_mats.keys()) + list(market_features.columns)
    if target is not None:
        required_cols = required_cols + ["target"]
    long_df = long_df.dropna(subset=required_cols)
    return long_df


# ══════════════════════════════════════════════════════════════
# PART 3  Walk-Forward 訓練（expanding window，每年重訓）
# ══════════════════════════════════════════════════════════════

def _fit_one_fold(train_long: pd.DataFrame,
                  n_estimators: int = N_ESTIMATORS,
                  max_depth: int = MAX_DEPTH,
                  learning_rate: float = LEARNING_RATE,
                  early_stopping_rounds: int = EARLY_STOPPING_ROUNDS,
                  valid_frac: float = VALID_FRAC,
                  random_state: int = 42):
    """訓練集尾端（依時間序，非隨機）切一部分當 early stopping 驗證集，回傳訓練好的模型。"""
    import lightgbm as lgb

    uniq_dates = sorted(train_long["date"].unique())
    split_idx = max(1, int(len(uniq_dates) * (1 - valid_frac)))
    val_dates = set(uniq_dates[split_idx:])
    is_val = train_long["date"].isin(val_dates)

    X_train = train_long.loc[~is_val, ALL_FEATURES]
    y_train = train_long.loc[~is_val, "target"]
    X_val = train_long.loc[is_val, ALL_FEATURES]
    y_val = train_long.loc[is_val, "target"]

    model = lgb.LGBMRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        importance_type="gain",
        random_state=random_state,
        verbosity=-1,
    )

    if len(X_val) >= 20 and y_val.nunique() > 1:
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False)],
        )
    else:
        # 驗證集太小做不了 early stopping，直接用全部訓練資料 fit 到底
        model.fit(train_long[ALL_FEATURES], train_long["target"])

    return model, len(X_train), len(X_val)


def walk_forward_ml_composite(
    start: str = START_DATE,
    end: str = END_DATE,
    train_stride: int = TRAIN_DATE_STRIDE,
    min_train_days: int = MIN_TRAIN_DAYS,
    regime_df: Optional[pd.DataFrame] = None,
    regime_extras: Optional[dict] = None,
) -> Tuple[pd.DataFrame, List[Dict], Dict[str, pd.DataFrame]]:
    """
    完整跑一次 Task 5 的 walk-forward ML 因子合成。

    regime_df/regime_extras：可選，Task 6（strategy/ablation.py）需要
    ablation D 的 ML 特徵跟其他版本的機制曝險用「同一次」
    regime_engine.run_regime_engine() 執行結果，兩者都給時就不重跑一次
    （regime_engine 內部的 LightGBM 崩盤預警重跑會有輕微數值不確定性，
    見 _market_features_from_regime_output() 的說明）。不給就照 Task 5
    原本的行為，自己跑一次。

    回傳：
      predictions   : pd.DataFrame(date x stock_id)，OOS 預測分數
                       （只在模型暖機完成、有測試年份的區間才有值）
      fold_info     : List[dict]，每個 fold 一筆：
                       {year, n_train, n_val, best_iteration, model,
                        importances: {feature: gain}, X_test_sample}
      raw           : {"factor_mats", "target", "market_features", "close"}
                       方便呼叫端（例如額外的訓練策略比較實驗）重複利用，
                       不用重算一次因子。
    """
    logger.info(f"ml_composite：載入全市場資料 {start} ~ {end}...")
    data = quant_layer2.load_matrices(DB_PATH, start, end)
    close = data["close"]

    factor_mats = build_factor_matrices(data)
    target = build_target(close, FWD_DAYS)
    if regime_df is not None and regime_extras is not None:
        market_features = _market_features_from_regime_output(regime_df, regime_extras, close.index)
    else:
        market_features = build_market_features(close.index, start="2012-05-02", end=end)

    all_dates = close.index
    years = sorted(set(all_dates.year))

    predictions = pd.DataFrame(np.nan, index=all_dates, columns=close.columns)
    fold_info: List[Dict] = []

    for y in years:
        cutoff = pd.Timestamp(f"{y}-01-01")
        train_dates_all = all_dates[all_dates < cutoff]
        train_dates = train_dates_all[::train_stride]
        if len(train_dates) < min_train_days:
            logger.info(f"ml_composite {y} 年：訓練日數（抽樣後）只有 {len(train_dates)}，"
                       f"< {min_train_days}，暖機未完成，跳過")
            continue

        train_long = matrices_to_long(factor_mats, market_features, train_dates, target=target)
        if len(train_long) < 1000:
            logger.info(f"ml_composite {y} 年：有效訓練樣本只有 {len(train_long)} 筆，跳過")
            continue

        model, n_train, n_val = _fit_one_fold(train_long)

        test_dates = all_dates[all_dates.year == y]
        test_long = matrices_to_long(factor_mats, market_features, test_dates, target=None)

        if not test_long.empty:
            preds = model.predict(test_long[ALL_FEATURES])
            test_long = test_long.assign(pred=preds)
            pred_wide = test_long.pivot(index="date", columns="stock_id", values="pred")
            predictions.loc[pred_wide.index, pred_wide.columns] = pred_wide.values

        importances = dict(zip(ALL_FEATURES, model.feature_importances_))
        fold_info.append({
            "year": y,
            "n_train": n_train,
            "n_val": n_val,
            "best_iteration": getattr(model, "best_iteration_", None),
            "model": model,
            "importances": importances,
            "X_test_sample": test_long[ALL_FEATURES] if not test_long.empty else None,
        })
        logger.info(f"ml_composite {y} 年：訓練 {n_train} 筆／驗證 {n_val} 筆，"
                   f"best_iteration={getattr(model, 'best_iteration_', 'n/a')}，"
                   f"測試樣本 {len(test_long)} 筆")

    raw = {
        "factor_mats": factor_mats,
        "target": target,
        "market_features": market_features,
        "close": close,
    }
    return predictions, fold_info, raw


# ══════════════════════════════════════════════════════════════
# PART 4  報告輸出：SHAP summary、逐年 feature importance
# ══════════════════════════════════════════════════════════════

def write_shap_summary(fold_info: List[Dict],
                       path: str = "reports/shap_summary.png",
                       sample_n: int = 3000,
                       random_state: int = 42) -> None:
    """
    用「最後一個 fold」的模型（訓練資料最多、最貼近目前可用的最新模型）
    對它自己的測試年樣本抽樣，畫 SHAP summary plot。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import shap

    valid_folds = [f for f in fold_info if f["X_test_sample"] is not None and not f["X_test_sample"].empty]
    if not valid_folds:
        logger.warning("ml_composite：沒有可用的 fold 測試樣本，無法產出 SHAP summary")
        return

    last = valid_folds[-1]
    X_sample = last["X_test_sample"]
    if len(X_sample) > sample_n:
        X_sample = X_sample.sample(sample_n, random_state=random_state)

    explainer = shap.TreeExplainer(last["model"])
    shap_values = explainer.shap_values(X_sample)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    plt.figure()
    shap.summary_plot(shap_values, X_sample, feature_names=ALL_FEATURES, show=False)
    plt.title(f"SHAP Summary — 最後一個 walk-forward fold（{last['year']} 年測試集，n={len(X_sample)}）")
    plt.tight_layout()
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"ml_composite：SHAP summary 已存到 {path}")


def write_feature_importance_by_year(fold_info: List[Dict],
                                     path: str = "reports/feature_importance_by_year.md") -> pd.DataFrame:
    """
    每個 fold（年份）的 feature importance（LightGBM gain，正規化成當年佔比 %），
    輸出成表格（因子為列、年份為欄）。回傳這個表（給 stability 分析重複使用，
    不用重新解析 markdown）。
    """
    rows = {}
    for f in fold_info:
        raw_imp = f["importances"]
        total = sum(raw_imp.values()) or 1.0
        rows[f["year"]] = {k: v / total * 100.0 for k, v in raw_imp.items()}

    table = pd.DataFrame(rows).reindex(ALL_FEATURES)
    table = table.round(2)

    lines = []
    lines.append("# 逐年 Feature Importance（Task 5 ML 因子合成）\n")
    lines.append("每個 walk-forward fold（年份=測試年，訓練資料為該年以前全部歷史）"
                 "重新訓練一次 LightGBM，這裡列出每個 fold 訓練出的模型，"
                 "各特徵的 gain-based importance（正規化成當年所有特徵 gain 總和的百分比，"
                 "橫向加總 = 100%）。\n")
    lines.append(f"訓練樣本抽樣頻率：全市場每 {TRAIN_DATE_STRIDE} 個交易日取一天（計算量考量，"
                 "預測仍逐日進行，見 strategy/ml_composite.py 模組說明）。\n")
    lines.append("## 逐年 Importance（%）\n")

    try:
        md_table = table.to_markdown()
    except ImportError:
        md_table = table.to_string()
    lines.append(md_table + "\n")

    lines.append("\n## 每個 fold 的訓練規模\n")
    fold_rows = [{
        "年份": f["year"], "訓練樣本數": f["n_train"], "驗證樣本數": f["n_val"],
        "best_iteration": f["best_iteration"],
    } for f in fold_info]
    fold_df = pd.DataFrame(fold_rows).set_index("年份")
    try:
        lines.append(fold_df.to_markdown() + "\n")
    except ImportError:
        lines.append(fold_df.to_string() + "\n")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"ml_composite：逐年 feature importance 已存到 {path}")
    return table


# ══════════════════════════════════════════════════════════════
# PART 5  額外要求：因子穩定性分析
# ══════════════════════════════════════════════════════════════

def compute_factor_rank_table(importance_table: pd.DataFrame) -> pd.DataFrame:
    """
    只看 FACTOR_FEATURES（五個核心因子，不含機制 one-hot／大盤波動分位）
    在每年的相對重要性排名（1=當年最重要，5=當年最不重要）。
    """
    factor_imp = importance_table.reindex(FACTOR_FEATURES)
    # rank(ascending=False)：importance 越大排名數字越小（1 = 最重要）
    rank_table = factor_imp.rank(axis=0, ascending=False, method="min")
    return rank_table


def _plain_language_conclusion(rank_std: pd.Series) -> str:
    sorted_std = rank_std.sort_values()
    lines = []
    lines.append("排名標準差越小，代表這個因子每年重要性排名都差不多"
                 "（不管是穩定重要還是穩定不重要），可信賴；標準差越大，"
                 "代表排名逐年跳動很大，比較像雜訊或高度看市況才有效，"
                 "需要謹慎看待。\n")
    for name, std in sorted_std.items():
        lines.append(f"- **{name}**：排名標準差 {std:.2f}")
    lines.append("")
    most_stable = sorted_std.index[0]
    least_stable = sorted_std.index[-1]
    lines.append(f"最穩定的是 **{most_stable}**（標準差最小），"
                 f"最不穩定、逐年重要性跳動最大的是 **{least_stable}**。")
    return "\n".join(lines)


def write_factor_stability_analysis(importance_table: pd.DataFrame,
                                    strategy_comparison_md: str,
                                    path: str = "reports/factor_stability_analysis.md") -> None:
    rank_table = compute_factor_rank_table(importance_table)
    rank_std = rank_table.std(axis=1).sort_values()

    lines = []
    lines.append("# 因子穩定性分析（Task 5 額外要求）\n")
    lines.append("五個因子（含 margin_usage）在每個 walk-forward 訓練年份的重要性排名"
                 "（只在這五個因子之間排名，1=當年最重要，5=當年最不重要；"
                 "機制 one-hot／大盤波動分位不算在「因子」裡，不參與這個排名）。\n")

    lines.append("## 1. 跨年度排名走勢表\n")
    try:
        lines.append(rank_table.to_markdown() + "\n")
    except ImportError:
        lines.append(rank_table.to_string() + "\n")

    lines.append("\n## 2. 排名標準差（跨年度）\n")
    std_df = rank_std.to_frame("排名標準差").round(3)
    try:
        lines.append(std_df.to_markdown() + "\n")
    except ImportError:
        lines.append(std_df.to_string() + "\n")

    lines.append("\n## 3. 白話結論\n")
    lines.append(_plain_language_conclusion(rank_std) + "\n")

    lines.append("\n## 4. 訓練策略比較：只用最近一年 vs 最近三年加權平均\n")
    lines.append(strategy_comparison_md)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"ml_composite：因子穩定性分析已存到 {path}")


# ══════════════════════════════════════════════════════════════
# PART 6  額外實驗：只用最近一年 vs 最近三年加權平均
# ══════════════════════════════════════════════════════════════
#
# 這是給「之後決定 Task 5 最終採用哪種訓練策略」用的簡化版比較實驗，
# 不是要取代上面 expanding-window（用全部歷史）的主模型——任務書
# Task 5 規格明講「訓練資料為該年以前的全部歷史」，這裡的比較實驗
# 是額外要求，結果只放進 factor_stability_analysis.md 供參考，不會
# 拿來覆蓋主模型的訓練方式（不然就是看到不喜歡的結果就換做法，
# 是本專案一直避免的行為）。
#
# 簡化之處（誠實列出，不是為了湊結果）：
#   - n_estimators 縮小到 150（兩種策略用同一個縮小值，公平比較，
#     只是為了控制這個額外實驗的計算時間，不影響相對比較的有效性）
#   - 只跑最近幾個可用年份（不是全部 walk-forward 年份），一樣是
#     計算時間考量
#   - 評估指標用「預測值 vs 實際未來報酬」的橫截面 Spearman IC
#     （逐日計算後取年度均值/標準差），不是完整回測——這是任務書
#     用詞「簡化版 walk-forward」的意思：比較訓練策略的相對優劣，
#     不需要重新跑一次完整交易成本/換手率模擬
SIMPLE_N_ESTIMATORS = 150
SIMPLE_MAX_YEARS_COMPARED = 4


def _simple_fit(train_long: pd.DataFrame, n_estimators: int = SIMPLE_N_ESTIMATORS):
    import lightgbm as lgb
    model = lgb.LGBMRegressor(
        n_estimators=n_estimators,
        max_depth=MAX_DEPTH,
        learning_rate=LEARNING_RATE,
        importance_type="gain",
        random_state=42,
        verbosity=-1,
    )
    model.fit(train_long[ALL_FEATURES], train_long["target"])
    return model


def _daily_ic(pred_wide: pd.DataFrame, target: pd.DataFrame) -> pd.Series:
    """逐日橫截面 Spearman IC（預測分數 vs 實際未來報酬排名）。"""
    common_idx = pred_wide.index.intersection(target.index)
    records = {}
    for d in common_idx:
        p = pred_wide.loc[d].dropna()
        t = target.loc[d].dropna()
        common_cols = p.index.intersection(t.index)
        if len(common_cols) < 10:
            continue
        ic = p[common_cols].rank().corr(t[common_cols].rank(), method="spearman")
        records[d] = ic
    return pd.Series(records)


def compare_training_strategies(raw: Dict[str, pd.DataFrame],
                                max_years: int = SIMPLE_MAX_YEARS_COMPARED,
                                stride: int = TRAIN_DATE_STRIDE) -> Tuple[pd.DataFrame, str]:
    """
    策略 1（只用最近一年）：測試年 y，只用 y-1 那一年的資料訓練一個模型。
    策略 2（最近三年加權平均）：測試年 y，分別用 y-1／y-2／y-3 各自單獨
    訓練一個模型，預測值用 0.5/0.3/0.2（越近年份權重越高）加權平均。

    回傳 (逐年 IC 比較表, markdown 文字)。
    """
    factor_mats = raw["factor_mats"]
    target = raw["target"]
    market_features = raw["market_features"]
    close = raw["close"]

    all_dates = close.index
    years = sorted(set(all_dates.year))

    # 需要 y-3 也有資料才能公平比較兩種策略，所以起始年至少是資料起點+3
    candidate_years = [y for y in years if (all_dates[all_dates.year == y - 1]).size >= MIN_TRAIN_DAYS
                       and (all_dates[all_dates.year == y - 3]).size > 0]
    test_years = candidate_years[-max_years:] if len(candidate_years) > max_years else candidate_years

    logger.info(f"ml_composite：簡化版訓練策略比較，測試年份 = {test_years}")

    results = []
    for y in test_years:
        test_dates = all_dates[all_dates.year == y]
        test_long_feat = matrices_to_long(factor_mats, market_features, test_dates, target=None)
        if test_long_feat.empty:
            continue

        # 策略 1：只用 y-1 一年
        y1_dates = all_dates[(all_dates.year == y - 1)][::stride]
        train_y1 = matrices_to_long(factor_mats, market_features, y1_dates, target=target)
        if len(train_y1) < 500:
            continue
        model_1y = _simple_fit(train_y1)
        pred_1y = pd.Series(model_1y.predict(test_long_feat[ALL_FEATURES]), index=test_long_feat.index)

        # 策略 2：y-1／y-2／y-3 各自訓練，加權平均（越近權重越高）
        weights = {1: 0.5, 2: 0.3, 3: 0.2}
        weighted_pred = pd.Series(0.0, index=test_long_feat.index)
        total_w = 0.0
        for lag, w in weights.items():
            yr_dates = all_dates[(all_dates.year == y - lag)][::stride]
            train_yr = matrices_to_long(factor_mats, market_features, yr_dates, target=target)
            if len(train_yr) < 500:
                continue
            model_yr = _simple_fit(train_yr)
            weighted_pred = weighted_pred.add(
                pd.Series(model_yr.predict(test_long_feat[ALL_FEATURES]), index=test_long_feat.index) * w,
                fill_value=0.0,
            )
            total_w += w
        if total_w == 0:
            continue
        pred_3y = weighted_pred / total_w

        test_long_1y = test_long_feat.assign(pred=pred_1y.values)
        test_long_3y = test_long_feat.assign(pred=pred_3y.values)

        pred_wide_1y = test_long_1y.pivot(index="date", columns="stock_id", values="pred")
        pred_wide_3y = test_long_3y.pivot(index="date", columns="stock_id", values="pred")

        ic_1y = _daily_ic(pred_wide_1y, target)
        ic_3y = _daily_ic(pred_wide_3y, target)

        results.append({
            "年份": y,
            "策略1_只用最近一年_IC均值": ic_1y.mean(),
            "策略1_IC標準差": ic_1y.std(),
            "策略2_最近三年加權平均_IC均值": ic_3y.mean(),
            "策略2_IC標準差": ic_3y.std(),
        })
        logger.info(f"ml_composite 策略比較 {y} 年：策略1 IC均值={ic_1y.mean():.4f}／"
                   f"策略2 IC均值={ic_3y.mean():.4f}")

    comparison_df = pd.DataFrame(results).set_index("年份").round(4) if results else pd.DataFrame()

    lines = []
    if comparison_df.empty:
        lines.append("資料不足，無法執行這項比較實驗（可用年份太少）。\n")
    else:
        lines.append(f"比較年份：{test_years}（受限於運算時間，只跑最近 "
                     f"{max_years} 個可用年份，用 n_estimators={SIMPLE_N_ESTIMATORS} 的"
                     "簡化版模型，兩種策略同一設定，公平比較）。\n")
        lines.append("評估方式：逐日橫截面 Spearman IC（預測分數 vs 個股實際未來 20 日報酬排名），"
                     "取年度均值與標準差——均值越高代表預測力越強，標準差越小代表年度內越穩定。\n")
        try:
            lines.append(comparison_df.to_markdown() + "\n")
        except ImportError:
            lines.append(comparison_df.to_string() + "\n")

        mean_1y = comparison_df["策略1_只用最近一年_IC均值"].mean()
        mean_3y = comparison_df["策略2_最近三年加權平均_IC均值"].mean()
        std_1y = comparison_df["策略1_只用最近一年_IC均值"].std()
        std_3y = comparison_df["策略2_最近三年加權平均_IC均值"].std()

        lines.append(f"\n全部比較年份加總：策略1（只用最近一年）平均 IC = {mean_1y:.4f}，"
                     f"年度間標準差 = {std_1y:.4f}；策略2（最近三年加權平均）平均 IC = "
                     f"{mean_3y:.4f}，年度間標準差 = {std_3y:.4f}。\n")

        if mean_3y > mean_1y and std_3y <= std_1y:
            verdict = "策略2（最近三年加權平均）在這次比較中平均 IC 較高、年度間也較穩定，兩項指標都更好。"
        elif mean_3y > mean_1y:
            verdict = "策略2（最近三年加權平均）平均 IC 較高，但年度間標準差沒有比較小——報酬與穩定性的取捨，不是單方面完勝。"
        elif std_3y < std_1y:
            verdict = "策略2（最近三年加權平均）年度間比較穩定，但平均 IC 沒有比較高——犧牲一點預測力換穩定性。"
        else:
            verdict = "這次比較裡，策略1（只用最近一年）在平均 IC 與穩定性上都不比策略2差，沒有看到加權平均帶來明顯好處。"
        lines.append(f"**結論（僅供之後參考，不代表現在要切換主模型的訓練方式）**：{verdict}")

    return comparison_df, "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# PART 7  主程式
# ══════════════════════════════════════════════════════════════

def run_full_task5(start: str = START_DATE, end: str = END_DATE) -> Tuple[pd.DataFrame, List[Dict]]:
    predictions, fold_info, raw = walk_forward_ml_composite(start, end)

    if not fold_info:
        raise RuntimeError("ml_composite：所有年份的訓練都被跳過（暖機不足或樣本太少），"
                          "無法產出任何報告，請確認資料範圍/DB 內容。")

    write_shap_summary(fold_info)
    importance_table = write_feature_importance_by_year(fold_info)
    _, strategy_comparison_md = compare_training_strategies(raw)
    write_factor_stability_analysis(importance_table, strategy_comparison_md)

    return predictions, fold_info


if __name__ == "__main__":
    predictions, fold_info = run_full_task5()
    print(f"\n✅ Task 5 ML 因子合成完成，共 {len(fold_info)} 個 walk-forward fold。")
    print(f"   預測矩陣：{predictions.shape[0]} 個交易日 × {predictions.shape[1]} 檔股票")
    print("   輸出：reports/shap_summary.png、reports/feature_importance_by_year.md、"
         "reports/factor_stability_analysis.md")
