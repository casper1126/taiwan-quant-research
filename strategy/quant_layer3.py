"""
quant_layer3.py
─────────────────────────────────────────────────────────────────────────────
台股多因子策略 Layer 3（重新設計版）

設計原則（來自研究章節 11–12）：
  - 以價值為核心，輔以品質因子（降低價值陷阱）
  - 靜態 IC 權重（移除噪聲嚴重的動態 IC 加權）
  - 風險平價權重（依個股波動反向加權，含 3%–8% 邊界）
  - 三級曝險擇時（多頭 100% / 中性 50% / 空頭 0%）
  - 月度再平衡（21 個交易日）
  - 資料從 2012 年載入（因子充分暖機），績效從 2015 年起算
  - 移除 bias_cap 反動能過濾器

因子組合（靜態權重）：
  複合因子 = 0.30 × Value      (1/PER + 1/PBR 平均)
           + 0.25 × Quality   (ROE = PBR/PER，僅正值)
           + 0.20 × LowVol    (60 日 IVOL 倒數)
           + 0.15 × RevMom    (月營收 YoY，40 天延遲)
           + 0.10 × InstFlow  (三大法人 60 日累積買超)

投資組合最佳化方法：
  ┌──────────────┬─────────────────────────────────┬──────────────┐
  │ 方法          │ 邏輯                             │ 本策略選用    │
  ├──────────────┼─────────────────────────────────┼──────────────┤
  │ 等權          │ 每檔同重，簡單但分散效果好         │              │
  │ 風險平價 ✓   │ 1/σ 加權，均衡風險貢獻            │      ✓       │
  │ 均值變異       │ 最大化 Sharpe，對估計誤差極敏感   │              │
  │ Black-Litterman│ 市場均衡 + 主觀觀點             │              │
  │ HRP           │ 層次聚類，免矩陣求逆，最穩健      │ (見 hrp_weights)│
  └──────────────┴─────────────────────────────────┴──────────────┘

直接執行：python strategy/quant_layer3.py
"""

import sqlite3
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset

warnings.filterwarnings("ignore", category=FutureWarning)

# ── 路徑 ─────────────────────────────────────────────────────────
DB_PATH       = "data/taiwan_stock.db"
WARMUP_START  = "2012-01-01"   # 資料載入起點（因子暖機用）
REPORT_START  = "2015-01-01"   # 績效計算起點
END_DATE      = "2026-04-17"

# ── 策略參數 ─────────────────────────────────────────────────────
TOP_N         = 25
REBAL_FREQ    = 21             # 月度再平衡
BUFFER_RATIO  = 1.5            # D: 1.3 → 1.5（Layer 2 同設定，降換手）
SMOOTH_WINDOW = 10             # D: 5 → 10 日（因子平滑視窗）
RP_EQ_BLEND   = 0.5            # D: 風險平價 0.5 + 等權 0.5（降防禦偏差）

# 靜態因子權重
# 變更歷程：
#   v1: value 0.30 / quality 0.25 / low_vol 0.20 / rev_mom 0.15 / inst_flow 0.10
#       → quality (PBR/PER) 在大型股宇宙差異性低，ICIR 0.017 ≈ 噪聲
#   v2: 移除 quality，redistribute → value 0.40 / low_vol 0.25 / rev_mom 0.25 / inst 0.10
#   v3: 暫移除 inst_flow → 結果反而更差（CAGR -1.5% → -2.6%）
#       原因：籌碼資料其實已下載完成（19M 筆，2015-2026），inst_flow 是有效訊號
#   v4: 還原 inst_flow
#   v5: 加入 mom_52w
#   v6 (current, N1): Sharpe-proportional weighting
#       Pairing analyzer 顯示單因子 Sharpe：
#         mom_52w   0.561  ← 主力 50%
#         inst_flow 0.422  ← 二把手 35%
#         value     0.117  ← 陪襯 10%
#         rev_mom   0.034  ← 5%
#         low_vol  -0.561  ← 砍掉（過去害我們 -3.4% 的元兇）
#   v7 (current, N1+sweep): 純 2 因子（純化 mom + inst）
#       Sweep 確認 value/rev_mom 在 5%/10% 權重下其實是雜訊，砍光更乾淨
#       60/40 是 Sharpe 最大化點
FACTOR_WEIGHTS: Dict[str, float] = {
    "mom_52w":   0.60,   # ↑ 0.50 → 0.60（純化）
    "inst_flow": 0.40,   # ↑ 0.35 → 0.40
    # "value":     0.0,  # 砍（5%/10% 試了反而拖累）
    # "rev_mom":   0.0,
    # "low_vol":   0.0,  # 砍（單因子 CAGR -3.4%）
}

# 風險平價邊界
MAX_WEIGHT    = 0.08           # 單檔上限 8%
MIN_WEIGHT    = 0.025          # 單檔下限 2.5%

# 產業分散限制（B：避免重複押注同一產業）
INDUSTRY_CAP_WEIGHT = 0.25     # 單一產業權重上限 25%
INDUSTRY_CAP_COUNT  = 6        # 單一產業最多持有 6 檔（25 × 25% = 6.25）

# 交易成本
COMMISSION    = 0.001425
TAX           = 0.003
SLIPPAGE      = 0.001
RF_RATE       = 0.015


# ══════════════════════════════════════════════════════════════════
# PART 1  資料載入
# ══════════════════════════════════════════════════════════════════

def load_matrices(db_path: str, start: str, end: str) -> Dict[str, pd.DataFrame]:
    """從 SQLite 載入資料，轉為寬格式矩陣。從 WARMUP_START 起載入以充分暖機。"""
    print(f"📂 載入資料 {start} ~ {end}（含暖機）...")
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
    # 月營收：載入比 start 早 18 個月，確保 YoY 在 start 時有效
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

    def wide(df, col):
        return df.pivot(index="date", columns="stock_id", values=col)

    data = {
        "close":         wide(price_df, "close"),
        "volume":        wide(price_df, "volume"),
        "PER":           wide(val_df,   "PER"),
        "PBR":           wide(val_df,   "PBR"),
        "revenue":       wide(rev_df,   "revenue"),
        "institutional": inst_matrix,
    }
    n_stocks = data["close"].shape[1]
    n_days   = data["close"].shape[0]
    print(f"✅ 載入完成：{n_stocks} 檔股票 × {n_days} 個交易日")
    return data


# ══════════════════════════════════════════════════════════════════
# PART 2  因子建構
# ══════════════════════════════════════════════════════════════════

def _cross_zscore(m: pd.DataFrame) -> pd.DataFrame:
    """全宇宙 cross-sectional Z-score（不分產業）。"""
    mean = m.mean(axis=1)
    std  = m.std(axis=1).replace(0, np.nan)
    return m.sub(mean, axis=0).div(std, axis=0).clip(-3, 3)


def _industry_neutral_zscore(m: pd.DataFrame) -> pd.DataFrame:
    """
    產業中性化 Z-score（P1-2）：

    在「每個產業內」做 cross-sectional z-score，然後合併。
    意義：電子股的 PER 平均比金融股高 → 全宇宙 z-score 會結構性低估電子股的「相對便宜」。
         產業中性化後，每檔股票的分數是「在自己產業裡的相對位置」，
         金融內部最便宜 vs 電子內部最便宜，分數可比較。

    對少於 3 檔的小產業（OTHER）：保留 NaN（避免單檔 std=0）。
    """
    ind_map = pd.Series([_industry_of(s) for s in m.columns], index=m.columns)
    result  = pd.DataFrame(np.nan, index=m.index, columns=m.columns)

    for ind, sub_idx in ind_map.groupby(ind_map):
        cols = sub_idx.index
        sub  = m[cols]
        if sub.shape[1] < 3:                      # 太少，無法穩健 z-score
            continue
        sub_mean = sub.mean(axis=1)
        sub_std  = sub.std(axis=1).replace(0, np.nan)
        zs = sub.sub(sub_mean, axis=0).div(sub_std, axis=0).clip(-3, 3)
        result.loc[:, cols] = zs.values
    return result


def build_rev_yoy(rev_matrix: pd.DataFrame, price_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """月營收 → 日頻 YoY（40 天公告延遲，防 look-ahead bias）。"""
    monthly_yoy = rev_matrix.sort_index().pct_change(periods=12)
    monthly_yoy.index = monthly_yoy.index + DateOffset(days=40)
    monthly_yoy = monthly_yoy.sort_index()
    return monthly_yoy.reindex(price_dates, method="ffill")


def build_factors(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """
    建構五個因子矩陣（寬格式，index=date, columns=stock_id）。

    因子 1 Value（0.30）：
      (1/PER + 1/PBR) / 2  — 盈利收益率 + 資產折價的加權平均。
      只取正 PER & PBR（負值表示虧損/溢價，設為 NaN）。

    因子 2 Quality（0.25）：
      ROE = PBR / PER（當 PER > 0 時），衡量股東報酬率。
      公式推導：ROE = E/B = (E/P)×(P/B) = (1/PER)×PBR。
      注意：理想的 quality 應加入毛利率，但需財報資料。

    因子 3 Low Volatility（0.20）：
      1 / IVOL₆₀ — 60 日日報酬標準差倒數。
      台股特效因子：散戶偏好高波動股，機構偏好低波動。

    因子 4 Rev Momentum（0.15）：
      月營收 YoY（含 40 天延遲）— 台股特有的即時基本面信號。

    因子 5 Inst Flow（0.10）：
      外資 + 投信過去 60 日累積淨買超。若資料未下載則自動略過。
    """
    close  = data["close"].replace(0.0, np.nan)
    volume = data["volume"].replace(0.0, np.nan)
    PER    = data["PER"]
    PBR    = data["PBR"]

    # ── 投資宇宙：日均成交金額前 300（流動性過濾）──────────────
    dollar_volume = (volume * close).rolling(252, min_periods=60).mean()
    dv_rank       = dollar_volume.rank(axis=1, ascending=False)
    liquid_mask   = dv_rank <= 300

    # ── 漲跌停過濾（交易現實性）───────────────────────────────
    # 台股漲跌停 ±10%。容忍 0.5% 數值誤差，閾值用 9.5%。
    # 漲停（買不到）/ 跌停（賣不掉）的當日不能 enter/exit。
    daily_ret_raw   = close.pct_change()
    LIMIT_THRESHOLD = 0.095
    limit_up_mask   = daily_ret_raw >= LIMIT_THRESHOLD     # 當日漲停
    limit_down_mask = daily_ret_raw <= -LIMIT_THRESHOLD    # 當日跌停
    not_at_limit    = ~(limit_up_mask | limit_down_mask)

    # ── 處置股 hook（DB 沒有此資料，先預留介面）──────────────
    # 未來：sanction_mask = pd.read_sql("SELECT date, stock_id FROM sanction_stocks ...")
    # 目前：全 True（不過濾）
    sanction_mask = pd.DataFrame(True, index=close.index, columns=close.columns)

    # 合併所有 mask（注意：漲跌停只在「rebal 當日」過濾，不影響因子計算）
    # 因此 liquid_mask 維持原本的流動性定義；trading_mask 是「交易日的可交易性」
    trading_mask = liquid_mask & not_at_limit & sanction_mask

    close_liq = close.where(liquid_mask, np.nan)
    PER_liq   = PER.where(liquid_mask & (PER > 0), np.nan)
    PBR_liq   = PBR.where(liquid_mask & (PBR > 0), np.nan)

    # ① Value = 平均(1/PER, 1/PBR)
    ep     = (1.0 / PER_liq).replace([np.inf, -np.inf], np.nan)
    bp     = (1.0 / PBR_liq).replace([np.inf, -np.inf], np.nan)
    value  = (ep + bp) / 2.0

    # ② Quality = ROE = PBR/PER（僅正 PER 的股票）
    quality = (PBR_liq / PER_liq).replace([np.inf, -np.inf], np.nan)

    # ③ Low Volatility
    ivol_60 = close_liq.pct_change().rolling(60, min_periods=20).std()
    low_vol = (1.0 / ivol_60).replace([np.inf, -np.inf], np.nan)

    # ⑥ 52-Week High Momentum（George-Hwang 2004）
    # 公式：close(t) / max(close[t-252:t])，值域 [0, 1]
    # 1.0 = 創 52 週新高（最強動能）
    # 與傳統 12-1 動能相比：有界、抗動能崩盤、台股實證更穩
    high_252 = close_liq.rolling(252, min_periods=120).max()
    mom_52w  = (close_liq / high_252).clip(0, 1)

    # ④ Revenue Momentum（YoY）
    rev_yoy = build_rev_yoy(data["revenue"], close.index)
    rev_yoy = rev_yoy.where(liquid_mask, np.nan)

    # ⑤ Institutional Flow（60 日累積）
    inst_raw = data.get("institutional")
    if inst_raw is not None and not inst_raw.empty:
        inst_aligned = inst_raw.reindex(index=close.index, columns=close.columns).fillna(0.0)
        inst_flow = inst_aligned.rolling(60, min_periods=10).sum().where(liquid_mask, np.nan)
    else:
        inst_flow = pd.DataFrame(np.nan, index=close.index, columns=close.columns)

    return {
        "value":        value,
        "quality":      quality,
        "low_vol":      low_vol,
        "rev_mom":      rev_yoy,
        "inst_flow":    inst_flow,
        "mom_52w":      mom_52w,
        "dollar_volume": dollar_volume,
        "liquid_mask":  liquid_mask,
        "trading_mask": trading_mask,   # 含漲跌停 + 處置股過濾
        "close":        close,
    }


def build_composite(factors: Dict[str, pd.DataFrame],
                    weights: Dict[str, float] = FACTOR_WEIGHTS,
                    smooth_window: int = SMOOTH_WINDOW,
                    industry_neutral: bool = True) -> pd.DataFrame:
    """
    靜態權重合成因子（v8/v9：含 P1-1 平滑 + P1-2 產業中性化）。

    流程：
      raw factor → [5 日 rolling mean]      ← P1-1 平滑（降噪）
                 → [產業內 z-score]          ← P1-2 產業中性化
                 → 加權合成
                 → 至少 1 個因子有效才算分數

    P1-1 平滑（smooth_window=5）：
      raw 因子值有噪聲，平滑後 IC 預期上升 0.005~0.015、換手率下降 ~30%。
      對 mom_52w / low_vol 這類已是 rolling 統計量的因子，多 5 日平滑無害。

    P1-2 產業中性化：
      在每個產業內部做 z-score，再合併。
      不再讓「金融 PER 結構性低」自動把金融股推到價值因子前段。
      改為「金融內部最便宜 vs 電子內部最便宜」公平競爭。

    composite NaN 處理：
      用 fill_value=0 把缺失因子當「中性 z-score = 0」，避免因 NaN 交集 = 0
      導致整個 rebal 日空手（v7 修復的 critical bug）。
    """
    active = {k: v for k, v in factors.items()
              if k in weights and not v.isna().all().all()}

    total_w = sum(weights[k] for k in active)
    if total_w == 0:
        return pd.DataFrame(0.0, index=factors["close"].index,
                            columns=factors["close"].columns)

    zscore_fn = _industry_neutral_zscore if industry_neutral else _cross_zscore

    composite = None
    for name, df in active.items():
        w = weights[name] / total_w

        # ── P1-1：5 日平滑（min_periods 容忍早期）────────────────
        smoothed = df.rolling(smooth_window,
                              min_periods=max(1, smooth_window // 2)).mean()

        # ── P1-2：產業內 z-score（如果開啟）────────────────────
        zs = zscore_fn(smoothed).fillna(0.0)

        if composite is None:
            composite = zs * w
        else:
            composite = composite.add(zs * w, fill_value=0.0)

    # 至少一個因子在 cross-section 有真實值
    any_valid = None
    for name, df in active.items():
        valid = ~df.isna()
        any_valid = valid if any_valid is None else (any_valid | valid)
    return composite.where(any_valid, np.nan)


def print_factor_diagnostics(factors: Dict[str, pd.DataFrame],
                              report_start: str = REPORT_START) -> None:
    """印出每個因子在 report_start 之後的 IC 摘要（20 日預測窗）。"""
    close = factors["close"]
    fwd_ret = close.pct_change(20).shift(-20)
    start_ts = pd.Timestamp(report_start)

    print("\n" + "═" * 58)
    print("  📐 因子診斷（IC Analysis，預測窗 20 天，2015 起）")
    print("═" * 58)

    rows = []
    for name, wt in FACTOR_WEIGHTS.items():
        m = factors.get(name)
        if m is None or m.isna().all().all():
            continue
        m_sub = m.loc[m.index >= start_ts]
        f_sub = fwd_ret.loc[fwd_ret.index >= start_ts]
        ic_list = []
        for dt in m_sub.index:
            if dt not in f_sub.index:
                continue
            f = m_sub.loc[dt].dropna()
            r = f_sub.loc[dt].dropna()
            common = f.index.intersection(r.index)
            if len(common) < 10:
                continue
            ic_list.append(f[common].rank().corr(r[common].rank(), method="spearman"))
        if not ic_list:
            continue
        ic = pd.Series(ic_list)
        icir = ic.mean() / ic.std() if ic.std() > 0 else 0.0
        rows.append({
            "因子":    f"{name}({wt:.0%})",
            "IC 均值": round(ic.mean(), 4),
            "IC 標準差": round(ic.std(), 4),
            "ICIR":    round(icir, 3),
            "IC>0%":   f"{(ic > 0).mean():.1%}",
        })

    if rows:
        print(pd.DataFrame(rows).set_index("因子").to_string())
        print("\n  💡 ICIR > 0.5 且 IC>0% > 55%：值得使用")
    else:
        print("  （資料不足，跳過）")
    print("═" * 58 + "\n")


# ══════════════════════════════════════════════════════════════════
# PART 2.5  LightGBM 因子合成（C-1 ML composite）
# ══════════════════════════════════════════════════════════════════

def build_ml_composite_advanced(factors: Dict[str, pd.DataFrame],
                                  refit_freq: int = 60,
                                  train_lookback_years: int = 4,
                                  fwd_horizons: tuple = (5, 20, 60),
                                  n_estimators: int = 100,
                                  max_depth: int = 3,
                                  min_child_samples: int = 500,
                                  n_seeds: int = 3) -> pd.DataFrame:
    """
    進階 ML 合成（L）：多 horizon target + 互動特徵 + 多 seed ensemble。

    改動 vs v2:
      ① 特徵擴充：5 因子 + 5 個 60d lag + 5 個 (mom × low_vol) 互動 = 15 維
      ② Multi-horizon target：5/20/60 日 forward rank 平均（捕捉不同時間尺度）
      ③ Multi-seed ensemble：每次 refit 訓 3 個模型用不同 random_state，平均預測
                           → 降低單一模型的 variance

    為什麼這 3 個一起做：
      ① 互動特徵讓 ML 學「動能在低波動環境下更可靠」這類條件式
      ② Multi-horizon 防止只擬合單一時間尺度的雜訊
      ③ Ensemble 是 ML 標準做法，10 行 code 平均換來 10% variance reduction
    """
    import lightgbm as lgb

    close = factors["close"]

    # ── Multi-horizon target（5/20/60 日 forward rank 平均）──
    target_mat = None
    for h in fwd_horizons:
        ret_h  = close.pct_change(h).shift(-h)
        rank_h = ret_h.rank(axis=1, pct=True) - 0.5
        target_mat = rank_h if target_mat is None else target_mat + rank_h
    target_mat = target_mat / len(fwd_horizons)

    # ── 預先建構基礎 + 進階 feature 矩陣 ────────────────────
    base_names = ["value", "mom_52w", "low_vol", "rev_mom", "inst_flow"]
    feature_dfs: Dict[str, pd.DataFrame] = {}

    for name in base_names:
        if name in factors and not factors[name].isna().all().all():
            smoothed = factors[name].rolling(
                SMOOTH_WINDOW,
                min_periods=max(1, SMOOTH_WINDOW // 2),
            ).mean()
            zs = _industry_neutral_zscore(smoothed).fillna(0.0)
            feature_dfs[name] = zs
            # Lag 60 日（捕捉因子的延續性）
            feature_dfs[f"{name}_lag60"] = zs.shift(60).fillna(0.0)

    # 互動特徵：mom × low_vol、mom × value 等
    if "mom_52w" in feature_dfs and "low_vol" in feature_dfs:
        feature_dfs["mom_x_lowvol"] = feature_dfs["mom_52w"] * feature_dfs["low_vol"]
    if "mom_52w" in feature_dfs and "value" in feature_dfs:
        feature_dfs["mom_x_value"] = feature_dfs["mom_52w"] * feature_dfs["value"]
    if "rev_mom" in feature_dfs and "value" in feature_dfs:
        feature_dfs["revmom_x_value"] = feature_dfs["rev_mom"] * feature_dfs["value"]

    if len(feature_dfs) < 5:
        print("  ⚠️  L ML 模式：可用特徵不足，回退標準 ML")
        return build_ml_composite(factors)

    feature_names = list(feature_dfs.keys())
    print(f"  🧠 進階 ML 特徵數：{len(feature_names)}")

    # ── Walk-forward training (with multi-seed ensemble) ──
    predictions = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    refit_idx = list(range(0, len(close.index), refit_freq))
    refit_dates = [close.index[i] for i in refit_idx]
    n_refits = len(refit_dates)

    print(f"  🤖 LightGBM 進階 walk-forward：{n_refits} refits × "
          f"{n_seeds} seeds = {n_refits * n_seeds} 次訓練")

    fitted_count = 0
    for i, refit_date in enumerate(refit_dates):
        train_cutoff = refit_date - pd.Timedelta(days=max(fwd_horizons) + 10)
        train_start  = train_cutoff - pd.Timedelta(days=train_lookback_years * 365)
        train_dates_all = close.loc[train_start:train_cutoff].index
        if len(train_dates_all) < 60:
            continue

        X_chunks, y_chunks = [], []
        for d in train_dates_all:
            if d not in target_mat.index:
                continue
            row_features = pd.DataFrame(
                {name: zs.loc[d] for name, zs in feature_dfs.items()}
            )
            row_target = target_mat.loc[d]
            valid = row_features.notna().all(axis=1) & row_target.notna()
            if valid.sum() < 10:
                continue
            X_chunks.append(row_features.loc[valid].values)
            y_chunks.append(row_target.loc[valid].values)

        if len(X_chunks) < 10:
            continue

        X_train = np.vstack(X_chunks)
        y_train = np.concatenate(y_chunks)

        # ── Multi-seed ensemble ──────────────────────────
        seed_preds_list = []
        for seed in range(n_seeds):
            model = lgb.LGBMRegressor(
                n_estimators=n_estimators,
                max_depth=max_depth,
                num_leaves=2 ** max_depth - 1,
                learning_rate=0.05,
                subsample=0.7,
                colsample_bytree=0.7,
                min_child_samples=min_child_samples,
                reg_alpha=0.1,
                reg_lambda=0.1,
                random_state=42 + seed,
                verbose=-1,
            )
            model.fit(X_train, y_train)
            seed_preds_list.append(model)
        fitted_count += n_seeds

        # ── 預測 [refit_date, next_refit_date) ─────────
        pred_end = refit_dates[i + 1] if i + 1 < n_refits else close.index[-1]
        pred_dates = close.loc[refit_date:pred_end].index

        for d in pred_dates:
            row_features = pd.DataFrame(
                {name: zs.loc[d] for name, zs in feature_dfs.items()}
            )
            valid = row_features.notna().all(axis=1)
            if valid.sum() == 0:
                continue
            X_pred = row_features.loc[valid].values
            seed_preds = np.column_stack([m.predict(X_pred) for m in seed_preds_list])
            y_pred = seed_preds.mean(axis=1)
            predictions.loc[d, valid[valid].index] = y_pred

    print(f"  ✅ 進階 ML 訓練完成：{fitted_count} 次有效訓練")
    return predictions


def build_ml_composite(factors: Dict[str, pd.DataFrame],
                        refit_freq: int = 60,
                        train_lookback_years: int = 4,
                        fwd_days: int = 20,
                        n_estimators: int = 100,
                        max_depth: int = 3,
                        min_child_samples: int = 500,
                        target_type: str = "rank",
                        extra_features: dict = None) -> pd.DataFrame:
    """
    用 LightGBM 預測 forward return（C-1，已馴服版 v2）。

    為什麼用 LightGBM 而不是線性合成：
      線性合成假設「IC 在所有股票/時期都一樣」。但實證上：
        - 高 ROE 公司在多頭表現好，空頭表現差（條件式）
        - 動能在牛市強，熊市會崩盤（Daniel-Moskowitz 2016）
      LightGBM 自動學這些非線性條件式關係。

    馴服改動（v2 vs v1，回應原版過擬合）：
      ① max_depth 6 → 3：每棵樹只能切 3 層，強迫學廣泛模式
      ② n_estimators 200 → 100：減少樹數，避免長尾擬合
      ③ min_child_samples 200 → 500：每個葉節點至少 500 樣本，避免局部噪聲
      ④ target_type "demean" → "rank"：用截面排名（[-0.5, +0.5]）
         理由：return 有極端值（漲停板），ML 會被噪聲拉走；rank 抗 outlier

    防止 look-ahead bias：
      training 截止 = refit_date - 30 天（給 fwd_days 留 buffer）
      預測期 [refit_date, next_refit_date) 完全 OOS

    回傳值：
      raw predictions (date × stock_id)，**未做 z-score**
      由 run_pipeline 的 ensemble 階段做 z-score + 與 linear 組合。
    """
    import lightgbm as lgb

    close = factors["close"]
    fwd_ret = close.pct_change(fwd_days).shift(-fwd_days)

    # ── Target 選擇（rank 抗 outlier）─────────────────────
    if target_type == "rank":
        # 截面 percentile rank，置中為 [-0.5, +0.5]
        target_mat = fwd_ret.rank(axis=1, pct=True) - 0.5
    else:  # "demean"
        target_mat = fwd_ret.sub(fwd_ret.mean(axis=1), axis=0)

    # ── 預先建構 feature 矩陣（10 日平滑 + 產業中性化）─────
    base_names = ["value", "mom_52w", "low_vol", "rev_mom", "inst_flow"]
    feature_dfs: Dict[str, pd.DataFrame] = {}
    for name in base_names:
        if name in factors and not factors[name].isna().all().all():
            smoothed = factors[name].rolling(
                SMOOTH_WINDOW,
                min_periods=max(1, SMOOTH_WINDOW // 2),
            ).mean()
            zs = _industry_neutral_zscore(smoothed).fillna(0.0)
            feature_dfs[name] = zs

    # 加入額外 features（O2 ablation 測試用）
    if extra_features:
        for name, df in extra_features.items():
            feature_dfs[name] = df

    if len(feature_dfs) < 2:
        print("  ⚠️  ML 模式：可用因子不足，回退到線性合成")
        return build_composite(factors)

    active_feature_names = list(feature_dfs.keys())
    if extra_features:
        print(f"  🧪 ablation：base 5 + extras {list(extra_features.keys())}")

    # ── Walk-forward training & prediction ────────────────
    predictions = pd.DataFrame(np.nan, index=close.index, columns=close.columns)

    refit_idx = list(range(0, len(close.index), refit_freq))
    refit_dates = [close.index[i] for i in refit_idx]
    n_refits = len(refit_dates)
    print(f"  🤖 LightGBM walk-forward：{n_refits} 個 refit 週期，"
          f"每次 lookback {train_lookback_years} 年")

    fitted_count = 0
    for i, refit_date in enumerate(refit_dates):
        # 訓練集截止日：refit_date - 30 天（buffer 防 fwd_ret 洩漏）
        train_cutoff = refit_date - pd.Timedelta(days=30)
        train_start = train_cutoff - pd.Timedelta(days=train_lookback_years * 365)

        # ── Stack features for training period ────────────
        train_dates_all = close.loc[train_start:train_cutoff].index
        if len(train_dates_all) < 60:
            continue

        X_chunks, y_chunks = [], []
        for d in train_dates_all:
            if d not in target_mat.index:
                continue
            row_features = pd.DataFrame(
                {name: zs.loc[d] for name, zs in feature_dfs.items()}
            )
            row_target = target_mat.loc[d]
            valid = row_features.notna().all(axis=1) & row_target.notna()
            if valid.sum() < 10:
                continue
            X_chunks.append(row_features.loc[valid].values)
            y_chunks.append(row_target.loc[valid].values)

        if len(X_chunks) < 10:
            continue

        X_train = np.vstack(X_chunks)
        y_train = np.concatenate(y_chunks)

        # ── 訓練 LightGBM（馴服參數：強正則化）─────────────────
        model = lgb.LGBMRegressor(
            n_estimators=n_estimators,            # 100（v2，原 200）
            max_depth=max_depth,                  # 3（v2，原 6）
            num_leaves=2 ** max_depth - 1,        # 配合 max_depth=3 → 7
            learning_rate=0.05,
            subsample=0.7,                        # 0.8 → 0.7
            colsample_bytree=0.7,                 # 0.8 → 0.7
            min_child_samples=min_child_samples,  # 500（v2，原 200）
            reg_alpha=0.1,                        # L1 正則
            reg_lambda=0.1,                       # L2 正則
            random_state=42,
            verbose=-1,
        )
        model.fit(X_train, y_train)
        fitted_count += 1

        # ── 預測 [refit_date, next_refit_date) ────────────
        pred_end = refit_dates[i + 1] if i + 1 < n_refits else close.index[-1]
        pred_dates = close.loc[refit_date:pred_end].index

        for d in pred_dates:
            row_features = pd.DataFrame(
                {name: zs.loc[d] for name, zs in feature_dfs.items()}
            )
            valid = row_features.notna().all(axis=1)
            if valid.sum() == 0:
                continue
            X_pred = row_features.loc[valid].values
            y_pred = model.predict(X_pred)
            predictions.loc[d, valid[valid].index] = y_pred

    print(f"  ✅ ML 訓練完成：{fitted_count} 次有效 refit")
    return predictions


# ══════════════════════════════════════════════════════════════════
# PART 3  投資組合建構
# ══════════════════════════════════════════════════════════════════

def market_timing_score(close: pd.DataFrame,
                        liquid_mask: pd.DataFrame,
                        ma_weight: float = 0.6,
                        tsmom_weight: float = 0.4) -> pd.Series:
    """
    擇時分數 v3 — 4-MA + TSMOM 結合版（C-2 CTA 加成）。

    結構：
      proxy = 流動宇宙等權報酬累積指數（returns-based）
      MA_score    = (proxy > MA10) + (proxy > MA30) + (proxy > MA60) + (proxy > MA120)，÷ 4
      TSMOM_score = 過去 252 日（1 年）累積報酬正負（>0 = 1，否則 0）

      final_score = 0.6 × MA_score + 0.4 × TSMOM_score

    為什麼加 TSMOM（Time-Series Momentum，Moskowitz et al. 2012）：
      - 4-MA 是短中期趨勢，TSMOM 是長期趨勢
      - 兩者結合：必須短中長期都向上才滿倉，避免單一視角誤判
      - CTA 學派核心：「Crisis Alpha」— 大盤跌破年線時保護資產

    四級曝險不變：
      score >= 0.75  → 多頭（100%）
      score >= 0.50  → 偏多（70%）
      score >= 0.25  → 震盪（50%）
      score <  0.25  → 空頭（30% 底倉）
    """
    daily_ret = close.where(liquid_mask, np.nan).pct_change()
    proxy_ret = daily_ret.mean(axis=1).fillna(0.0)
    proxy = (1.0 + proxy_ret).cumprod()

    # ── 4-MA score（短中期趨勢）─────────────────────────────
    ma_score = pd.Series(0.0, index=proxy.index)
    for w in [10, 30, 60, 120]:
        ma_score += (proxy > proxy.rolling(w, min_periods=w // 2).mean()).astype(float)
    ma_score /= 4.0

    # ── TSMOM score（長期趨勢，CTA 加成）────────────────────
    tsmom_252 = proxy.pct_change(252)
    tsmom_score = (tsmom_252 > 0).astype(float).fillna(0.5)

    return ma_weight * ma_score + tsmom_weight * tsmom_score


def _industry_of(stock_id: str) -> str:
    """
    根據台股代號前兩碼粗略分類產業（TWSE 編碼慣例）。

    DB 中沒有產業欄位，但台股代號有官方分類規則：
      11–22  傳產（水泥/食品/塑膠/紡織/電機/化學/玻璃/紙/鋼鐵/橡膠/汽車）
      23–27  電子零組件（半導體/光電/被動元件/光學）
      28     金融保險（銀行/壽險/控股）
      30–49  電子上中游（IC設計/封測/PCB/網通）
      50–69  電子下游（電腦周邊/通路/光電下游）
      80–89  觀光/食品/電子商務
      90–99  其他服務/航運/百貨
    """
    if not stock_id or len(stock_id) < 2 or not stock_id[:2].isdigit():
        return "OTHER"
    code = int(stock_id[:2])
    if code == 28:                  return "金融"
    if 11 <= code <= 22:            return "傳產"
    if 23 <= code <= 27:            return "電子零組件"
    if 30 <= code <= 49:            return "電子中上游"
    if 50 <= code <= 69:            return "電子下游"
    if 80 <= code <= 89:            return "觀光食品"
    if 90 <= code <= 99:            return "其他服務"
    return "OTHER"


def select_with_industry_cap(rank_today: pd.Series,
                              top_n: int = TOP_N,
                              max_per_industry: int = INDUSTRY_CAP_COUNT) -> List[str]:
    """
    依 composite score 排名，貪婪選股但限制單一產業最多 max_per_industry 檔。

    範例：原本前 25 名有 12 檔金融股，會被限縮至 6 檔，
         其餘 6 個位置給排名 26–40 的非金融股。
    """
    ranked = rank_today.dropna().sort_values()
    selected: List[str] = []
    ind_count: Dict[str, int] = {}
    for stock_id in ranked.index:
        ind = _industry_of(stock_id)
        if ind_count.get(ind, 0) >= max_per_industry:
            continue
        selected.append(stock_id)
        ind_count[ind] = ind_count.get(ind, 0) + 1
        if len(selected) >= top_n:
            break
    return selected


def apply_industry_weight_cap(weights: pd.Series,
                               cap: float = INDUSTRY_CAP_WEIGHT) -> pd.Series:
    """
    將產業權重總和限制在 cap 以內。
    超出部分按比例縮減該產業所有股票，再把釋出的權重重分配給未超額產業。
    """
    if weights.empty or weights.sum() == 0:
        return weights
    weights = weights.copy()
    industries = pd.Series([_industry_of(s) for s in weights.index],
                           index=weights.index)

    for _ in range(20):
        ind_weight = weights.groupby(industries).sum()
        excess = ind_weight[ind_weight > cap]
        if excess.empty:
            break
        released = 0.0
        for ind, total in excess.items():
            mask = industries == ind
            scale = cap / total
            released += weights[mask].sum() * (1 - scale)
            weights.loc[mask] *= scale
        non_excess = ~industries.isin(excess.index)
        host_sum = weights[non_excess].sum()
        if host_sum > 0:
            weights.loc[non_excess] *= 1 + released / host_sum
        else:
            break
    return weights


def risk_parity_weights(daily_ret: pd.DataFrame,
                        holdings: List[str],
                        max_w: float = MAX_WEIGHT,
                        min_w: float = MIN_WEIGHT,
                        lookback: int = 60,
                        eq_blend: float = RP_EQ_BLEND) -> pd.Series:
    """
    風險平價 + 等權混合加權（D：降低過度防禦偏差）。

    新公式：
      w = eq_blend × (1/N) + (1 - eq_blend) × (1/σ_i 標準化)

    為什麼混合：
      純風險平價（w ∝ 1/σ）會把超大權重給金融、公用事業等低波動股，
      在牛市時嚴重落後（v9 在 2021/2023/2024 牛市落後 TAIEX 20%+）。
      混入 50% 等權後，仍享受 RP 的風險分散，但削減一半的防禦偏差。

    eq_blend = 0.5 預設：
      eq_blend = 0.0 → 純風險平價（最防禦）
      eq_blend = 0.5 → 平衡（D 預設）
      eq_blend = 1.0 → 純等權（無風險平價）

    上下界約束 [min_w, max_w] 仍套用，迭代收斂。
    """
    if not holdings:
        return pd.Series(dtype=float)

    n = len(holdings)
    recent = daily_ret[holdings].iloc[-lookback:]
    vols   = recent.std().replace(0, np.nan).dropna()

    if vols.empty:
        return pd.Series(1.0 / n, index=holdings)

    missing = [s for s in holdings if s not in vols.index]
    if missing:
        for s in missing:
            vols[s] = vols.mean()

    # ── 純風險平價權重 ─────────────────────────────────────
    rp_w = (1.0 / vols).reindex(holdings)
    rp_w = rp_w / rp_w.sum()

    # ── 等權 ───────────────────────────────────────────────
    eq_w = pd.Series(1.0 / n, index=holdings)

    # ── 混合 ───────────────────────────────────────────────
    weights = eq_blend * eq_w + (1 - eq_blend) * rp_w
    weights /= weights.sum()

    # 迭代套用上下界
    for _ in range(50):
        clipped = weights.clip(min_w, max_w)
        if (clipped - weights).abs().max() < 1e-9:
            break
        weights = clipped / clipped.sum()

    return weights


def hrp_weights(daily_ret: pd.DataFrame,
                holdings: List[str],
                lookback: int = 120) -> pd.Series:
    """
    層次風險平價（HRP, López de Prado 2016）。

    適合用於：
      - 股票數量 > 50 且相關性結構複雜
      - 需要比風險平價更強的分散化
    本策略的 25 檔宇宙中效果與風險平價相近，僅供比較研究。

    步驟：
      1. 計算相關矩陣 → 距離矩陣 d = sqrt(0.5(1-ρ))
      2. 層次聚類（ward 連結）
      3. 準對角化（quasi-diagonalization）
      4. 遞迴二分（recursive bisection）
    """
    from scipy.cluster.hierarchy import linkage, leaves_list
    from scipy.spatial.distance import squareform

    rets = daily_ret[holdings].iloc[-lookback:].dropna(axis=1, how="all")
    valid = [s for s in holdings if s in rets.columns]
    if len(valid) < 2:
        return pd.Series(1.0 / len(holdings), index=holdings)

    rets = rets[valid].fillna(0.0)
    corr = rets.corr().values
    np.fill_diagonal(corr, 1.0)
    dist = np.sqrt(0.5 * (1 - corr))
    dist = np.clip(dist, 0, None)

    # 層次聚類
    link = linkage(squareform(dist), method="ward")
    order = leaves_list(link)

    # 遞迴二分
    cov = rets.cov().values

    def _bisect(items):
        if len(items) == 1:
            return {items[0]: 1.0}
        half = len(items) // 2
        left, right = items[:half], items[half:]

        def _cluster_var(subset):
            idx = [valid.index(s) for s in subset]
            sub_cov = cov[np.ix_(idx, idx)]
            inv_diag = 1.0 / np.diag(sub_cov)
            w = inv_diag / inv_diag.sum()
            return float(w @ sub_cov @ w)

        v_l, v_r = _cluster_var(left), _cluster_var(right)
        total = v_l + v_r
        alpha = 1 - v_l / total

        w_l = _bisect(left)
        w_r = _bisect(right)
        return {s: w * (1 - alpha) for s, w in w_l.items()} | \
               {s: w * alpha for s, w in w_r.items()}

    sorted_stocks = [valid[i] for i in order]
    raw = _bisect(sorted_stocks)
    result = pd.Series(raw).reindex(holdings).fillna(0.0)
    return result / result.sum()


def build_positions(factors: Dict[str, pd.DataFrame],
                    composite: pd.DataFrame,
                    top_n: int = TOP_N,
                    rebal_freq: int = REBAL_FREQ,
                    use_hrp: bool = False) -> pd.DataFrame:
    """
    建構每日持倉矩陣（index=date, columns=stock_id）。

    三級曝險機制：
      在再平衡日評估擇時信號，決定本期全倉/半倉/空倉。
      個股權重（風險平價）保持不變，只改變總曝險比例。
      shift(1) 確保今日信號明日執行，防止未來偏差。
    """
    close        = factors["close"]
    liquid_mask  = factors["liquid_mask"]
    # trading_mask 含漲跌停 + 處置股過濾（rebal 當日不交易此類）
    trading_mask = factors.get("trading_mask", liquid_mask)
    cols         = close.columns

    market_s  = market_timing_score(close, liquid_mask)
    daily_ret = close.pct_change()
    rebal_idx = np.where(np.arange(len(close)) % rebal_freq == 0)[0]
    exit_thresh = int(top_n * BUFFER_RATIO)

    current_holds: set = set()
    pos_matrix = pd.DataFrame(0.0, index=close.index, columns=cols)

    for day_idx in rebal_idx:
        today     = close.index[day_idx]
        scores    = composite.loc[today]
        rank_today = scores.rank(ascending=False)

        # ── 漲跌停 / 處置股當天不可交易 ─────────────────────────
        # 取得當日可交易股票集合
        if today in trading_mask.index:
            tradable_today = set(trading_mask.loc[today][trading_mask.loc[today]].index)
        else:
            tradable_today = set(cols)

        # ── 無條件：跌出緩衝區就賣（但跌停日除外，否則賣不掉）───
        # 已持有的股票若今日跌停 → 強制留倉（無法執行賣單）
        current_holds -= {
            s for s in current_holds
            if (pd.isna(rank_today.get(s, np.nan))
                or rank_today.get(s, np.nan) > exit_thresh)
            and s in tradable_today          # 跌停的股票本期保留（無法賣）
        }

        # ── 四級曝險（保留 30% 底倉，避免完全踏空）────────────────
        score_today = float(market_s.loc[today]) if today in market_s.index else 1.0
        if score_today >= 0.75:
            exposure = 1.0
        elif score_today >= 0.50:
            exposure = 0.7
        elif score_today >= 0.25:
            exposure = 0.5
        else:
            exposure = 0.3

        # ── 買入新股（含產業 count cap：每產業最多 6 檔）──────────
        # 漲停的股票今天買不到 → 從候選名單剔除
        if exposure > 0:
            tradable_rank = rank_today[rank_today.index.isin(tradable_today)]
            entry_cands = set(select_with_industry_cap(tradable_rank, top_n=top_n))
            current_holds |= (entry_cands - current_holds)
            # 已持有的也要套用產業 count cap，避免歷史上 6 檔金融疊加
            if len(current_holds) > top_n:
                trimmed = select_with_industry_cap(
                    rank_today.reindex(list(current_holds)).dropna(),
                    top_n=top_n,
                )
                current_holds = set(trimmed)

        # ── 計算本期權重（風險平價 + 產業 weight cap 25%）──────────
        if current_holds and exposure > 0:
            holds_list = list(current_holds)
            ret_slice  = daily_ret.loc[:today]
            w = (hrp_weights(ret_slice, holds_list)
                 if use_hrp else
                 risk_parity_weights(ret_slice, holds_list))
            w = apply_industry_weight_cap(w)            # B：產業權重上限
            w_scaled = w * exposure                      # 曝險縮放
            pos_matrix.loc[today, w_scaled.index] = w_scaled.values
        # else: all zeros (空倉)

    # ffill 再平衡間的持倉，shift(1) 今日信號明日執行
    anchor = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor.iloc[rebal_idx] = pos_matrix.iloc[rebal_idx].values
    final_pos = anchor.ffill().fillna(0.0).shift(1).fillna(0.0)

    avg_hold = (final_pos > 0).sum(axis=1).replace(0, np.nan).mean()
    print(f"  平均持倉：{avg_hold:.1f} 檔（目標 {top_n} 檔）")
    return final_pos


# ══════════════════════════════════════════════════════════════════
# PART 4  回測引擎（含報告起始日過濾）
# ══════════════════════════════════════════════════════════════════

def run_backtest(close: pd.DataFrame,
                 position: pd.DataFrame,
                 report_start: str = REPORT_START,
                 commission: float = COMMISSION,
                 tax: float = TAX,
                 slippage: float = SLIPPAGE) -> Tuple[dict, pd.Series]:
    """
    向量化回測。只計算 report_start 之後的績效，
    report_start 之前的日期僅用於因子暖機。
    """
    print("📈 執行回測...")

    asset_ret  = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross_ret  = (position * asset_ret).sum(axis=1)
    turnover   = position.diff().abs().sum(axis=1)
    total_cost = turnover * (commission + slippage + commission + tax + slippage) / 2.0
    net_ret    = gross_ret - total_cost

    # 只從 report_start 開始計算淨值
    mask       = net_ret.index >= pd.Timestamp(report_start)
    net_ret_r  = net_ret.loc[mask]
    equity     = (1 + net_ret_r).cumprod()

    total_ret  = equity.iloc[-1] - 1
    n_days     = max((net_ret_r != 0).sum(), 1)
    annual_ret = (1 + total_ret) ** (252 / n_days) - 1
    annual_vol = net_ret_r.std() * np.sqrt(252)
    sharpe     = (annual_ret - RF_RATE) / annual_vol if annual_vol > 0 else 0.0
    mdd        = (equity / equity.cummax() - 1).min()
    calmar     = annual_ret / abs(mdd) if mdd != 0 else 0.0
    annual_to  = turnover.loc[mask].mean() * 252 * 2   # 雙邊換手

    # 年度報酬一覽
    annual_perf = net_ret_r.resample("YE").apply(lambda x: (1 + x).prod() - 1)
    annual_perf.index = annual_perf.index.year

    stats = {
        "回測區間":     f"{equity.index[0].date()} ~ {equity.index[-1].date()}",
        "總報酬":       f"{total_ret * 100:.1f}%",
        "年化報酬":     f"{annual_ret * 100:.1f}%",
        "年化波動度":   f"{annual_vol * 100:.1f}%",
        "Sharpe Ratio": f"{sharpe:.2f}",
        "最大回撤":     f"{mdd * 100:.1f}%",
        "Calmar Ratio": f"{calmar:.2f}",
        "年化換手率":   f"{annual_to:.1%}",
    }
    return stats, equity, annual_perf


# ══════════════════════════════════════════════════════════════════
# PART 5  輸出
# ══════════════════════════════════════════════════════════════════

def print_report(stats: dict, annual_perf: pd.Series) -> None:
    w = 48
    print("\n" + "═" * w)
    print("  📊 Layer 3 回測績效報告")
    print("═" * w)
    for k, v in stats.items():
        print(f"  {k:<16} {v}")
    print("─" * w)
    print("  年度報酬：")
    for yr, r in annual_perf.items():
        bar  = "█" * int(abs(r) * 100)
        sign = "+" if r > 0 else ""
        print(f"  {yr}  {sign}{r * 100:6.2f}%  {bar}")
    print("═" * w)

    try:
        sharpe  = float(stats["Sharpe Ratio"])
        mdd_val = float(stats["最大回撤"].replace("%", ""))
        ann_r   = float(stats["年化報酬"].replace("%", ""))
        print("\n  🩺 健康診斷：")
        print(f"    Sharpe > 1.0 : {'✅' if sharpe > 1.0 else '⚠️ '} {sharpe:.2f}")
        print(f"    MDD < 20%    : {'✅' if abs(mdd_val) < 20 else '⚠️ '} {mdd_val:.1f}%")
        print(f"    年化 > 15%   : {'✅' if ann_r > 15 else '⚠️ '} {ann_r:.1f}%")
    except Exception:
        pass


def save_equity(equity: pd.Series,
                path: str = "reports/equity_curve_L3.csv") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    equity.to_frame("equity").to_csv(path)
    print(f"  💾 淨值曲線：{path}")


# ══════════════════════════════════════════════════════════════════
# PART 6  主程式
# ══════════════════════════════════════════════════════════════════

def run_pipeline(use_hrp: bool = False,
                 use_ml: bool = False,
                 ml_advanced: bool = False,
                 save_path: str = "reports/equity_curve_L3.csv") -> Tuple[dict, pd.Series]:
    """執行完整 Layer 3 流程，回傳 (stats, equity)。

    Args:
        use_hrp:     用 HRP 加權取代風險平價
        use_ml:      用 LightGBM ML 合成（C-1）取代線性加權合成
        ml_advanced: ML 進階版（L）— 多 horizon + 互動特徵 + 多 seed
    """
    # Step 1：載入
    data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    if data["close"].shape[1] < 5:
        raise RuntimeError("股票數不足 5 檔，請先執行 run.py --step 1")

    # Step 2：建構因子
    print("\n🧠 建構因子矩陣...")
    factors = build_factors(data)

    # Step 3：診斷（基於 raw factors）
    print_factor_diagnostics(factors)

    # Step 4：合成（線性 / ML / 進階 ML）
    if use_ml or ml_advanced:
        ml_label = "進階 ML" if ml_advanced else "標準 ML"
        print(f"\n🤖 {ml_label} Ensemble：linear 50% + LightGBM 50%")
        linear_comp = build_composite(factors)
        linear_z    = _cross_zscore(linear_comp).fillna(0.0)

        if ml_advanced:
            ml_pred = build_ml_composite_advanced(factors)
        else:
            ml_pred = build_ml_composite(factors)
        ml_z = _cross_zscore(ml_pred).fillna(0.0)

        composite = 0.5 * linear_z + 0.5 * ml_z
        any_valid = (~linear_comp.isna()) | (~ml_pred.isna())
        composite = composite.where(any_valid, np.nan)
        print(f"  ✅ Ensemble 完成（linear z + {ml_label.lower()} z 各半）")
    else:
        composite = build_composite(factors)

    # Step 5：建構部位
    method = "HRP" if use_hrp else "風險平價+等權混合"
    print(f"📐 建構投資組合部位（{method}）...")
    positions = build_positions(factors, composite, use_hrp=use_hrp)

    # Step 6：回測
    stats, equity, annual_perf = run_backtest(data["close"], positions)

    # Step 7：輸出
    print_report(stats, annual_perf)
    save_equity(equity, save_path)

    return stats, equity


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Layer 3 台股多因子策略")
    parser.add_argument("--hrp", action="store_true",
                        help="使用 HRP 加權（預設：風險平價+等權混合）")
    parser.add_argument("--ml", action="store_true",
                        help="使用標準 ML 合成因子（C-1）")
    parser.add_argument("--ml-adv", action="store_true",
                        help="使用進階 ML（L：多 horizon + 互動特徵 + 多 seed）")
    args = parser.parse_args()

    if args.ml_adv:
        save_path = "reports/equity_curve_L3_ml_adv.csv"
    elif args.ml:
        save_path = "reports/equity_curve_L3_ml.csv"
    else:
        save_path = "reports/equity_curve_L3.csv"

    stats, equity = run_pipeline(use_hrp=args.hrp, use_ml=args.ml,
                                 ml_advanced=args.ml_adv,
                                 save_path=save_path)
    print("\n✅ Layer 3 完成！\n")
