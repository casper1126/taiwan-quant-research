"""
quant_pure_ml.py
─────────────────────────────────────────────────────────────
台股純 ML 量化策略（M1：完全 ML-driven）

設計哲學（vs Layer 3）：
  Layer 3 用「結構限制」（風險平價、產業 cap、低波動因子）保護下行，
            但這些限制在牛市拖累報酬。
  純 ML  捨棄所有結構限制，讓 LightGBM 自由學習因子-報酬關係，
            並用「預測強度」直接決定權重。

降低換手率的 6 個機制：
  ① 預測值 5 日 rolling mean smoothing（去除單日噪聲）
  ② Rebalance 30 個交易日（v12 是 21）
  ③ 出場 buffer 1.5x（排名跌出前 45 才賣）
  ④ ML refit 90 天（v12 是 60）
  ⑤ 個股 cap 10%（v12 是 8%）
  ⑥ 預測 winsorize（極端值截斷）

特徵工程（30 個）：
  A. 價格與動能 (10)：5/20/60/252 日報酬、52w 高、MA 乖離、RSI、布林、偏度、峰度
  B. 波動率 (4)：20/60 日 vol、vol_ratio、IVOL
  C. 流動性 (4)：log_dv、dv_rank、turnover、Amihud
  D. 基本面 (5)：1/PER, 1/PBR, Rev YoY, Rev MoM, Rev_acceleration
  E. 籌碼 (3)：60 日累積 inst、5 日累積 inst、inst 強度
  F. 截面排名 (3)：ep_rank、mom_rank、size_rank
  G. 時間 régime (1)：擇時 score（4 條 MA 投票）

直接執行：
  python strategy/quant_pure_ml.py
"""
import sqlite3
import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── 路徑與時間 ─────────────────────────────────────────────
DB_PATH      = "data/taiwan_stock.db"
WARMUP_START = "2012-01-01"
REPORT_START = "2015-01-01"
END_DATE     = "2026-04-17"

# ── 投資組合參數（為降低換手而調整）────────────────────────
TOP_N            = 30
REBAL_FREQ       = 30      # ↑ 21 → 30
BUFFER_RATIO     = 1.5     # ↑ 1.3 → 1.5
MAX_WEIGHT       = 0.10    # ↑ 0.08 → 0.10
SMOOTH_PRED_DAYS = 5       # 預測平滑視窗
WINSORIZE_QUANTILE = 0.02  # 預測 winsorize 上下 2%

# ── ML 訓練參數 ────────────────────────────────────────────
ML_REFIT_FREQ        = 90  # ↑ 60 → 90
ML_TRAIN_LOOKBACK_Y  = 4
ML_FWD_DAYS          = 20
ML_N_SEEDS           = 3

# ── 交易成本 ───────────────────────────────────────────────
COMMISSION = 0.001425
TAX        = 0.003
SLIPPAGE   = 0.001
RF_RATE    = 0.015


# ══════════════════════════════════════════════════════════════════
# PART 1：資料載入
# ══════════════════════════════════════════════════════════════════

def load_data(db_path: str = DB_PATH,
              start: str = WARMUP_START,
              end: str = END_DATE) -> Dict[str, pd.DataFrame]:
    """載入所有資料並轉成 wide format 矩陣。"""
    print(f"📂 載入 {start} ~ {end}...")
    conn = sqlite3.connect(db_path)

    price_df = pd.read_sql(
        "SELECT date, stock_id, close, volume FROM daily_price "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    val_df = pd.read_sql(
        "SELECT date, stock_id, PER, PBR FROM daily_valuation "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    rev_start = (pd.Timestamp(start) - pd.DateOffset(months=18)).strftime("%Y-%m-%d")
    rev_df = pd.read_sql(
        "SELECT date, stock_id, revenue FROM monthly_revenue "
        "WHERE date >= ? ORDER BY date",
        conn, params=(rev_start,), parse_dates=["date"],
    )
    try:
        inst_df = pd.read_sql(
            """SELECT date, stock_id,
                      SUM(CASE WHEN investor_type IN
                          ('Foreign_Investor','Foreign_Dealer_Self','Investment_Trust')
                          THEN net ELSE 0 END) AS inst_net
               FROM institutional_investors
               WHERE date BETWEEN ? AND ?
               GROUP BY date, stock_id ORDER BY date""",
            conn, params=(start, end), parse_dates=["date"],
        )
        inst_matrix = (
            inst_df.pivot(index="date", columns="stock_id", values="inst_net")
            if not inst_df.empty else pd.DataFrame()
        )
    except Exception:
        inst_matrix = pd.DataFrame()
    conn.close()

    def w(df, c):
        return df.pivot(index="date", columns="stock_id", values=c)

    out = {
        "close":   w(price_df, "close").replace(0.0, np.nan),
        "volume":  w(price_df, "volume").replace(0.0, np.nan),
        "PER":     w(val_df, "PER"),
        "PBR":     w(val_df, "PBR"),
        "revenue": w(rev_df, "revenue"),
    }
    if not inst_matrix.empty:
        out["inst"] = inst_matrix.reindex(index=out["close"].index,
                                          columns=out["close"].columns).fillna(0.0)
    else:
        out["inst"] = pd.DataFrame(0.0,
                                   index=out["close"].index,
                                   columns=out["close"].columns)
    print(f"✅ 完成：{out['close'].shape[1]} 檔 × {out['close'].shape[0]} 天")
    return out


# ══════════════════════════════════════════════════════════════════
# PART 2：30+ 特徵工程
# ══════════════════════════════════════════════════════════════════

def _rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """向量化 RSI。"""
    delta = close.diff()
    gain  = delta.where(delta > 0, 0).rolling(n, min_periods=n // 2).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(n, min_periods=n // 2).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _build_rev_yoy(rev: pd.DataFrame, dates: pd.DatetimeIndex,
                   delay_days: int = 40) -> pd.DataFrame:
    """月營收 YoY → 日頻（40 天公告延遲，防 look-ahead）。"""
    yoy = rev.sort_index().pct_change(periods=12)
    yoy.index = yoy.index + DateOffset(days=delay_days)
    return yoy.sort_index().reindex(dates, method="ffill")


def _build_rev_mom(rev: pd.DataFrame, dates: pd.DatetimeIndex,
                   delay_days: int = 40) -> pd.DataFrame:
    """月營收 MoM 加 40 天延遲。"""
    mom = rev.sort_index().pct_change(periods=1)
    mom.index = mom.index + DateOffset(days=delay_days)
    return mom.sort_index().reindex(dates, method="ffill")


def build_features(data: Dict[str, pd.DataFrame]) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
    """建構 30+ 特徵 + 流動性 mask（top 300 by 252-day dollar volume）。"""
    print("🧠 建構 30 個特徵...")
    close   = data["close"]
    volume  = data["volume"]
    PER     = data["PER"]
    PBR     = data["PBR"]
    revenue = data["revenue"]
    inst    = data["inst"]

    daily_ret = close.pct_change()

    # 流動性宇宙
    dv         = close * volume
    dv_60      = dv.rolling(60, min_periods=20).mean()
    dv_252     = dv.rolling(252, min_periods=60).mean()
    dv_rank_d  = dv_252.rank(axis=1, ascending=False)
    liquid     = dv_rank_d <= 300

    feats: Dict[str, pd.DataFrame] = {}

    # ── A. 價格與動能（10）─────────────────────────────────
    feats["ret_5d"]    = close.pct_change(5)
    feats["ret_20d"]   = close.pct_change(20)
    feats["ret_60d"]   = close.pct_change(60)
    feats["ret_252d"]  = close.pct_change(252)
    feats["mom_52w"]   = (close / close.rolling(252, min_periods=120).max()).clip(0, 1)
    ma60               = close.rolling(60, min_periods=20).mean()
    feats["ma_dist_60"] = (close - ma60) / ma60.replace(0, np.nan)
    feats["rsi_14"]    = _rsi(close, 14) / 100.0
    ma20               = close.rolling(20, min_periods=5).mean()
    std20              = close.rolling(20, min_periods=5).std()
    feats["bb_pos_20"] = (close - ma20) / (2 * std20.replace(0, np.nan))
    feats["ret_skew_60"] = daily_ret.rolling(60, min_periods=20).skew()
    feats["ret_kurt_60"] = daily_ret.rolling(60, min_periods=20).kurt()

    # ── B. 波動率（4）─────────────────────────────────────
    vol_20d  = daily_ret.rolling(20, min_periods=5).std()
    vol_60d  = daily_ret.rolling(60, min_periods=20).std()
    feats["vol_20d"]   = vol_20d
    feats["vol_60d"]   = vol_60d
    feats["vol_ratio"] = vol_20d / vol_60d.replace(0, np.nan)

    # IVOL：簡化用「日報酬 - 截面均值」的標準差（剝除大盤共同變動）
    market_ret  = daily_ret.where(liquid).mean(axis=1)
    excess_ret  = daily_ret.sub(market_ret, axis=0)
    feats["ivol_60d"]  = excess_ret.rolling(60, min_periods=20).std()

    # ── C. 流動性（4）─────────────────────────────────────
    feats["log_dv_60d"]   = np.log(dv_60.clip(lower=1))
    feats["dv_rank_pct"]  = dv_252.rank(axis=1, pct=True, ascending=False)
    turnover              = volume / volume.rolling(252, min_periods=60).mean()
    feats["turnover_20d"] = turnover.rolling(20, min_periods=5).mean()
    feats["amihud"]       = (daily_ret.abs() / dv.clip(lower=1)).rolling(60, min_periods=20).mean()

    # ── D. 基本面（5）─────────────────────────────────────
    feats["ep"]        = (1.0 / PER.where(PER > 0, np.nan)).replace([np.inf, -np.inf], np.nan)
    feats["bp"]        = (1.0 / PBR.where(PBR > 0, np.nan)).replace([np.inf, -np.inf], np.nan)
    feats["rev_yoy"]   = _build_rev_yoy(revenue, close.index)
    feats["rev_mom"]   = _build_rev_mom(revenue, close.index)
    feats["rev_accel"] = feats["rev_yoy"] - feats["rev_yoy"].shift(21)

    # ── E. 籌碼（3）───────────────────────────────────────
    inst_60         = inst.rolling(60, min_periods=10).sum()
    feats["inst_60d"] = inst_60
    feats["inst_5d"]  = inst.rolling(5, min_periods=2).sum()
    inst_dv60         = (close * volume).rolling(60, min_periods=10).sum()
    feats["inst_pct"] = inst_60 / inst_dv60.clip(lower=1)

    # ── F. 截面排名（3）──────────────────────────────────
    feats["ep_rank"]   = feats["ep"].rank(axis=1, pct=True)
    feats["mom_rank"]  = feats["mom_52w"].rank(axis=1, pct=True)
    feats["size_rank"] = dv_252.rank(axis=1, pct=True, ascending=False)

    # 套用 liquid mask 到所有特徵（OOU 不交易非流動股）
    for k in feats:
        feats[k] = feats[k].where(liquid, np.nan)

    print(f"  ✅ 完成 {len(feats)} 個特徵")
    return feats, liquid


# ══════════════════════════════════════════════════════════════════
# PART 3：截面標準化（cross-sectional z-score）
# ══════════════════════════════════════════════════════════════════

def cross_zscore(m: pd.DataFrame) -> pd.DataFrame:
    mean = m.mean(axis=1)
    std  = m.std(axis=1).replace(0, np.nan)
    return m.sub(mean, axis=0).div(std, axis=0).clip(-3, 3)


def standardize_features(feats: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """每個 feature 做截面 z-score（讓 ML 看相對而非絕對值）。"""
    print("📐 截面標準化...")
    return {k: cross_zscore(v) for k, v in feats.items()}


# ══════════════════════════════════════════════════════════════════
# PART 4：LightGBM Walk-Forward
# ══════════════════════════════════════════════════════════════════

def train_predict_walkforward(features: Dict[str, pd.DataFrame],
                                close: pd.DataFrame,
                                refit_freq: int = ML_REFIT_FREQ,
                                lookback_years: int = ML_TRAIN_LOOKBACK_Y,
                                fwd_days: int = ML_FWD_DAYS,
                                n_seeds: int = ML_N_SEEDS) -> pd.DataFrame:
    """
    Walk-forward LightGBM 訓練 + 預測，回傳 (date × stock) 預測矩陣。

    Target: forward return rank（截面 percentile - 0.5），抗 outlier。
    """
    import lightgbm as lgb

    # Target
    fwd_ret = close.pct_change(fwd_days).shift(-fwd_days)
    target  = fwd_ret.rank(axis=1, pct=True) - 0.5

    feature_names = list(features.keys())
    predictions = pd.DataFrame(np.nan, index=close.index, columns=close.columns)

    refit_idx   = list(range(0, len(close.index), refit_freq))
    refit_dates = [close.index[i] for i in refit_idx]
    n_refits    = len(refit_dates)
    print(f"🤖 Walk-forward：{n_refits} refits × {n_seeds} seeds = "
          f"{n_refits * n_seeds} 次訓練（lookback {lookback_years}y）")

    fitted_count = 0
    for i, refit_date in enumerate(refit_dates):
        train_cutoff = refit_date - pd.Timedelta(days=fwd_days + 10)
        train_start  = train_cutoff - pd.Timedelta(days=lookback_years * 365)
        train_dates  = close.loc[train_start:train_cutoff].index
        if len(train_dates) < 60:
            continue

        # 收集 (date, stock) 訓練樣本
        X_chunks, y_chunks = [], []
        for d in train_dates:
            if d not in target.index:
                continue
            row = pd.DataFrame({n: f.loc[d] for n, f in features.items()})
            tgt = target.loc[d]
            valid = row.notna().all(axis=1) & tgt.notna()
            if valid.sum() < 10:
                continue
            X_chunks.append(row.loc[valid].values)
            y_chunks.append(tgt.loc[valid].values)
        if not X_chunks:
            continue

        X_train = np.vstack(X_chunks)
        y_train = np.concatenate(y_chunks)

        # Multi-seed ensemble
        seed_models = []
        for seed in range(n_seeds):
            model = lgb.LGBMRegressor(
                n_estimators=200,
                max_depth=4,
                num_leaves=15,
                learning_rate=0.03,
                min_child_samples=300,
                subsample=0.7,
                colsample_bytree=0.7,
                reg_alpha=0.1,
                reg_lambda=0.1,
                random_state=42 + seed,
                verbose=-1,
            )
            model.fit(X_train, y_train)
            seed_models.append(model)
        fitted_count += n_seeds

        # 預測 [refit_date, next_refit_date)
        pred_end = refit_dates[i + 1] if i + 1 < n_refits else close.index[-1]
        pred_dates = close.loc[refit_date:pred_end].index

        for d in pred_dates:
            row = pd.DataFrame({n: f.loc[d] for n, f in features.items()})
            valid = row.notna().all(axis=1)
            if valid.sum() == 0:
                continue
            X_pred = row.loc[valid].values
            preds = np.column_stack([m.predict(X_pred) for m in seed_models])
            predictions.loc[d, valid[valid].index] = preds.mean(axis=1)

        if (i + 1) % 10 == 0:
            print(f"  Progress: {i + 1}/{n_refits} refits done...")

    print(f"  ✅ {fitted_count} 次有效訓練")
    return predictions


# ══════════════════════════════════════════════════════════════════
# PART 5：投組建構（ML 強度直接加權，無風險平價、無產業 cap）
# ══════════════════════════════════════════════════════════════════

def smooth_predictions(predictions: pd.DataFrame,
                        smooth_days: int = SMOOTH_PRED_DAYS,
                        winsorize_q: float = WINSORIZE_QUANTILE) -> pd.DataFrame:
    """
    預測值平滑 + winsorize（降換手）。
      smooth: 5 日 rolling mean → 去除單日噪聲
      winsorize: 每日截斷上下 winsorize_q quantile → 防極端值主導權重
    """
    smoothed = predictions.rolling(smooth_days, min_periods=2).mean()
    # 截面 winsorize
    lower = smoothed.quantile(winsorize_q,     axis=1)
    upper = smoothed.quantile(1 - winsorize_q, axis=1)
    return smoothed.clip(lower=lower, upper=upper, axis=0)


def build_positions(predictions: pd.DataFrame,
                     close: pd.DataFrame,
                     top_n: int = TOP_N,
                     rebal_freq: int = REBAL_FREQ,
                     buffer_ratio: float = BUFFER_RATIO,
                     max_w: float = MAX_WEIGHT) -> pd.DataFrame:
    """
    純 ML 投組建構：

    1. 預測排名前 top_n 進場（buffer 1.5x，跌出前 1.5n 才賣）
    2. 權重 = 預測強度 max(0, pred) 標準化
       → 預測越正權重越大（對「相信度」高的押重）
    3. 個股 cap max_w（防集中）
    4. 不做擇時（簡化，由 ML 內部捕捉）
    """
    cols   = close.columns
    n_days = len(close)
    rebal_idx = np.arange(0, n_days, rebal_freq)
    exit_thr  = int(top_n * buffer_ratio)

    current_holds: set = set()
    pos_matrix = pd.DataFrame(0.0, index=close.index, columns=cols)

    for day_idx in rebal_idx:
        today = close.index[day_idx]
        scores = predictions.loc[today]
        rank   = scores.rank(ascending=False)

        # 1. 賣出（跌出 buffer 區間或無評分）
        current_holds -= {
            s for s in current_holds
            if pd.isna(rank.get(s, np.nan)) or rank.get(s, np.nan) > exit_thr
        }

        # 2. 補進新股到 top_n
        cands = set(rank[rank <= top_n].dropna().index)
        current_holds |= (cands - current_holds)
        if len(current_holds) > top_n:
            ranked = sorted(current_holds, key=lambda s: rank.get(s, 9999))
            current_holds = set(ranked[:top_n])

        # 3. 算權重：max(0, pred) 標準化
        if current_holds:
            holds_list = list(current_holds)
            held_scores = scores.reindex(holds_list)
            # 將最低分 shift 到 0 之上（保證所有持股都有 baseline）
            shifted = (held_scores - held_scores.min() + 0.01).clip(lower=0.0)
            if shifted.sum() <= 0:
                w = pd.Series(1.0 / len(holds_list), index=holds_list)
            else:
                w = shifted / shifted.sum()

            # 個股 cap 迭代
            for _ in range(20):
                clipped = w.clip(upper=max_w)
                if (clipped - w).abs().max() < 1e-9:
                    break
                w = clipped / clipped.sum()
            pos_matrix.loc[today, w.index] = w.values

    # ffill rebal 之間的權重，shift(1) 防 look-ahead
    anchor = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor.iloc[rebal_idx] = pos_matrix.iloc[rebal_idx].values
    final = anchor.ffill().fillna(0.0).shift(1).fillna(0.0)

    avg_hold = (final > 0).sum(axis=1).replace(0, np.nan).mean()
    print(f"  📊 平均持倉：{avg_hold:.1f} 檔")
    return final


# ══════════════════════════════════════════════════════════════════
# PART 6：回測
# ══════════════════════════════════════════════════════════════════

def run_backtest(close: pd.DataFrame,
                  positions: pd.DataFrame,
                  report_start: str = REPORT_START) -> Tuple[dict, pd.Series, pd.Series]:
    print("📈 回測...")
    asset_ret  = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross_ret  = (positions * asset_ret).sum(axis=1)
    turnover   = positions.diff().abs().sum(axis=1)
    cost       = turnover * (COMMISSION + SLIPPAGE + COMMISSION + TAX + SLIPPAGE) / 2.0
    net_ret    = gross_ret - cost

    mask = net_ret.index >= pd.Timestamp(report_start)
    net_ret_r = net_ret.loc[mask]
    equity    = (1 + net_ret_r).cumprod()

    total_ret = equity.iloc[-1] - 1
    n_days    = max((net_ret_r != 0).sum(), 1)
    cagr      = (1 + total_ret) ** (252 / n_days) - 1
    vol       = net_ret_r.std() * np.sqrt(252)
    sharpe    = (cagr - RF_RATE) / vol if vol > 0 else 0.0
    mdd       = (equity / equity.cummax() - 1).min()
    calmar    = cagr / abs(mdd) if mdd != 0 else 0.0
    annual_to = turnover.loc[mask].mean() * 252

    yr_ret = net_ret_r.resample("YE").apply(lambda x: (1 + x).prod() - 1)
    yr_ret.index = yr_ret.index.year

    stats = {
        "回測區間":     f"{equity.index[0].date()} ~ {equity.index[-1].date()}",
        "總報酬":       f"{total_ret * 100:+.1f}%",
        "年化報酬":     f"{cagr * 100:+.2f}%",
        "年化波動度":   f"{vol * 100:.2f}%",
        "Sharpe":       f"{sharpe:.3f}",
        "最大回撤":     f"{mdd * 100:+.2f}%",
        "Calmar":       f"{calmar:.3f}",
        "年化換手率":   f"{annual_to * 100:.1f}%",
    }
    return stats, equity, yr_ret


def print_report(stats: dict, yr_ret: pd.Series) -> None:
    print("\n" + "=" * 50)
    print("  📊 純 ML 策略回測報告")
    print("=" * 50)
    for k, v in stats.items():
        print(f"  {k:<14} {v}")
    print("-" * 50)
    print("  年度報酬：")
    for yr, r in yr_ret.items():
        bar = "█" * int(abs(r) * 100)
        sign = "+" if r > 0 else ""
        print(f"  {yr}  {sign}{r * 100:6.2f}%  {bar}")
    print("=" * 50)


# ══════════════════════════════════════════════════════════════════
# PART 7：主程式
# ══════════════════════════════════════════════════════════════════

def run_pipeline(save_path: str = "reports/equity_curve_pure_ml.csv") -> Tuple[dict, pd.Series]:
    data = load_data()
    feats, liquid = build_features(data)
    feats_z = standardize_features(feats)
    predictions = train_predict_walkforward(feats_z, data["close"])
    predictions_smooth = smooth_predictions(predictions)
    positions = build_positions(predictions_smooth, data["close"])
    stats, equity, yr_ret = run_backtest(data["close"], positions)
    print_report(stats, yr_ret)

    Path("reports").mkdir(exist_ok=True)
    equity.to_frame("equity").to_csv(save_path)
    print(f"  💾 {save_path}")
    return stats, equity


if __name__ == "__main__":
    stats, equity = run_pipeline()
    print("\n✅ 純 ML 完成！\n")
