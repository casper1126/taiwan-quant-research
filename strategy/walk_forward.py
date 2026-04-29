"""
walk_forward.py
─────────────────────────────────────────────────────────────
Layer 3：Walk-Forward Out-of-Sample 驗證

設計邏輯：
  In-Sample 回測永遠過擬合——你選的參數在訓練期表現好，
  不代表未來也好。Walk-Forward 驗證的核心是：
    1. 在訓練集上「學習」因子的有效性（計算 IC 作為權重）
    2. 在測試集上「應用」這些權重（不再接觸訓練資料）
    3. 把所有測試期串接 = 真實的 OOS 績效

窗口設定（Expanding Window）：
  訓練 2015–2019 → 測試 2020
  訓練 2015–2020 → 測試 2021
  訓練 2015–2021 → 測試 2022
  訓練 2015–2022 → 測試 2023
  訓練 2015–2023 → 測試 2024
  訓練 2015–2024 → 測試 2025

直接執行：
  python strategy/walk_forward.py

或透過 run.py：
  python run.py --step 3
"""

import sys
import json
import sqlite3
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, "data_pipeline")
sys.path.insert(0, "strategy")

# ── 設定 ──────────────────────────────────────────────────────
DB_PATH        = "data/taiwan_stock.db"
TRAIN_START    = "2015-01-01"    # 訓練集起點（固定）
FIRST_TEST_YR  = 2020            # 第一個測試年份
LAST_TEST_YR   = 2025            # 最後一個測試年份

TOP_N          = 30
REBAL_FREQ     = 120
BUFFER_MULT    = 1.5
COMMISSION     = 0.001425
TAX            = 0.003
SLIPPAGE       = 0.001
RF_RATE        = 0.015


# ══════════════════════════════════════════════════════════════
# 資料載入
# ══════════════════════════════════════════════════════════════

def load_all_matrices(db_path: str) -> Dict[str, pd.DataFrame]:
    """一次載入所有資料，Walk-Forward 各 fold 從這裡切片。"""
    conn = sqlite3.connect(db_path)

    price_df = pd.read_sql(
        "SELECT date, stock_id, close, volume FROM daily_price ORDER BY date",
        conn, parse_dates=["date"],
    )
    val_df = pd.read_sql(
        "SELECT date, stock_id, PER, PBR, dividend_yield FROM daily_valuation ORDER BY date",
        conn, parse_dates=["date"],
    )
    rev_df = pd.read_sql(
        "SELECT date, stock_id, revenue FROM monthly_revenue ORDER BY date",
        conn, parse_dates=["date"],
    )

    # 三大法人（有就用，沒有就空）
    try:
        inst_df = pd.read_sql(
            """SELECT date, stock_id,
                      SUM(CASE WHEN investor_type IN
                          ('Foreign_Investor','Foreign_Dealer_Self','Investment_Trust')
                          THEN net ELSE 0 END) AS inst_net
               FROM institutional_investors
               GROUP BY date, stock_id ORDER BY date""",
            conn, parse_dates=["date"],
        )
        inst_matrix = inst_df.pivot(index="date", columns="stock_id", values="inst_net")
    except Exception:
        inst_matrix = pd.DataFrame()

    conn.close()

    def wide(df: pd.DataFrame, col: str) -> pd.DataFrame:
        return df.pivot(index="date", columns="stock_id", values=col)

    return {
        "close":         wide(price_df, "close"),
        "volume":        wide(price_df, "volume"),
        "PER":           wide(val_df,   "PER"),
        "revenue":       wide(rev_df,   "revenue"),
        "institutional": inst_matrix,
    }


# ══════════════════════════════════════════════════════════════
# 因子建構（單一窗口）
# ══════════════════════════════════════════════════════════════

def _build_rev_yoy(rev_matrix: pd.DataFrame,
                   price_dates: pd.DatetimeIndex) -> pd.DataFrame:
    yoy = rev_matrix.sort_index().pct_change(12)
    yoy.index = yoy.index + DateOffset(days=40)
    return yoy.sort_index().reindex(price_dates, method="ffill")


def build_factors_slice(data: Dict[str, pd.DataFrame],
                         start: str, end: str) -> Dict[str, pd.DataFrame]:
    """在指定日期範圍內建構所有因子矩陣。"""
    close  = data["close"].replace(0.0, np.nan).loc[start:end]
    volume = data["volume"].replace(0.0, np.nan).loc[start:end]
    PER    = data["PER"].loc[start:end]

    # 投資宇宙：日均成交金額前 300
    dv_rank = (volume * close).rolling(252, min_periods=60).mean().rank(axis=1, ascending=False)
    liquid  = dv_rank <= 300

    close_l = close.where(liquid, np.nan)
    PER_l   = PER.where(liquid, np.nan)

    high_252     = close.rolling(252, min_periods=60).max()
    momentum     = (close_l / high_252).where(liquid, np.nan)
    value        = (1.0 / PER_l).replace([np.inf, -np.inf], np.nan)
    rev_yoy      = _build_rev_yoy(data["revenue"], close.index).reindex(
                       columns=close.columns).where(liquid, np.nan)
    daily_ret    = close_l.pct_change()
    low_vol      = (1.0 / daily_ret.rolling(60, min_periods=20).std()
                   ).replace([np.inf, -np.inf], np.nan)

    inst_raw = data.get("institutional")
    if inst_raw is not None and not inst_raw.empty:
        inst_aligned = inst_raw.reindex(index=close.index, columns=close.columns).fillna(0)
        inst_flow    = inst_aligned.rolling(60, min_periods=10).sum().where(liquid, np.nan)
    else:
        inst_flow = pd.DataFrame(np.nan, index=close.index, columns=close.columns)

    bias = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    return {
        "momentum":   momentum,
        "value":      value,
        "rev_yoy":    rev_yoy,
        "low_vol":    low_vol,
        "inst_flow":  inst_flow,
        "bias":       bias,
        "PER":        PER_l,
        "close":      close,
        "liquid":     liquid,
    }


# ══════════════════════════════════════════════════════════════
# IC 計算與 ICIR 加權
# ══════════════════════════════════════════════════════════════

def compute_ic_series(factor: pd.DataFrame, fwd_ret: pd.DataFrame,
                       min_stocks: int = 10) -> pd.Series:
    records = []
    for dt in factor.index:
        if dt not in fwd_ret.index:
            continue
        f = factor.loc[dt].dropna()
        r = fwd_ret.loc[dt].dropna()
        common = f.index.intersection(r.index)
        if len(common) < min_stocks:
            continue
        ic = f[common].rank().corr(r[common].rank(), method="spearman")
        records.append({"date": dt, "IC": ic})
    if not records:
        return pd.Series(dtype=float)
    return pd.DataFrame(records).set_index("date")["IC"]


def icir_weights(factors: Dict[str, pd.DataFrame],
                 close: pd.DataFrame,
                 fwd_days: int = 20) -> Dict[str, float]:
    """
    用訓練集的 ICIR 比例計算各因子權重。
    ICIR = IC均值 / IC標準差，越高代表因子越穩定。
    """
    fwd_ret = close.pct_change(fwd_days).shift(-fwd_days)
    skip    = {"bias", "PER", "close", "liquid", "inst_flow"}  # inst_flow 可能全 NaN

    icir_map: Dict[str, float] = {}
    for name, mat in factors.items():
        if name in skip or mat.isna().all().all():
            continue
        ic = compute_ic_series(mat, fwd_ret)
        if ic.empty or ic.std() == 0:
            continue
        icir_map[name] = max(ic.mean() / ic.std(), 0.0)  # 負 ICIR 不給權重

    # inst_flow 若有資料也加入
    if "inst_flow" in factors and not factors["inst_flow"].isna().all().all():
        ic = compute_ic_series(factors["inst_flow"], fwd_ret)
        if not ic.empty and ic.std() != 0:
            icir_map["inst_flow"] = max(ic.mean() / ic.std(), 0.0)

    total = sum(icir_map.values())
    if total == 0:
        # 所有 ICIR ≤ 0 時回退等權
        n = len(icir_map) or 1
        return {k: 1.0 / n for k in icir_map}

    return {k: v / total for k, v in icir_map.items()}


# ══════════════════════════════════════════════════════════════
# 組合因子 + 建立部位
# ══════════════════════════════════════════════════════════════

def build_composite(factors: Dict[str, pd.DataFrame],
                     weights: Dict[str, float]) -> pd.DataFrame:
    """用 ICIR 加權合成複合因子分數（橫截面 z-score 後加權）。"""
    close = factors["close"]

    def cross_z(m: pd.DataFrame) -> pd.DataFrame:
        mu  = m.mean(axis=1)
        std = m.std(axis=1).replace(0, np.nan)
        return m.sub(mu, axis=0).div(std, axis=0).clip(-3, 3).fillna(0)

    composite = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for name, w in weights.items():
        if name in factors and w > 0:
            composite += cross_z(factors[name]) * w

    valid = (factors["PER"] > 0) & (factors["bias"].abs() < 0.10) & factors["liquid"]
    return composite.where(valid, np.nan)


def build_positions_oos(masked_composite: pd.DataFrame,
                         close: pd.DataFrame,
                         top_n: int = TOP_N,
                         rebal_freq: int = REBAL_FREQ,
                         buffer_mult: float = BUFFER_MULT) -> pd.DataFrame:
    """間化版建倉（OOS 用，不含擇時以隔離因子效果）。"""
    cols          = close.columns
    rebal_idx     = np.where(np.arange(len(close)) % rebal_freq == 0)[0]
    exit_thr      = int(top_n * buffer_mult)
    eq_w          = 1.0 / top_n
    current_holds: set = set()
    pos = pd.DataFrame(0.0, index=close.index, columns=cols)

    for day_idx in rebal_idx:
        today      = close.index[day_idx]
        rank_today = masked_composite.loc[today].rank(ascending=False)

        to_sell = {s for s in current_holds
                   if pd.isna(rank_today.get(s, np.nan)) or rank_today[s] > exit_thr}
        current_holds -= to_sell

        candidates = set(rank_today[rank_today <= top_n].index.tolist())
        current_holds |= candidates - current_holds

        if len(current_holds) > top_n:
            ranked = sorted([(s, rank_today.get(s, 9999)) for s in current_holds],
                            key=lambda x: x[1])
            current_holds = {s for s, _ in ranked[:top_n]}

        if current_holds:
            pos.loc[today, list(current_holds)] = eq_w

    anchor = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor.iloc[rebal_idx] = pos.iloc[rebal_idx].values
    return anchor.ffill().fillna(0.0).shift(1).fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 回測
# ══════════════════════════════════════════════════════════════

def backtest_slice(close: pd.DataFrame,
                    position: pd.DataFrame) -> Tuple[dict, pd.Series]:
    """輕量向量化回測，回傳績效指標 + 淨值序列。"""
    asset_ret   = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0)
    gross       = (position * asset_ret).sum(axis=1)
    turnover    = position.diff().abs().sum(axis=1)
    cost        = turnover * (COMMISSION + SLIPPAGE +
                              (COMMISSION + TAX + SLIPPAGE)) / 2
    net_ret     = gross - cost
    equity      = (1 + net_ret).cumprod()

    total_ret   = equity.iloc[-1] - 1
    n_active    = max((net_ret != 0).sum(), 1)
    ann_ret     = (1 + total_ret) ** (252 / n_active) - 1
    ann_vol     = net_ret.std() * np.sqrt(252)
    sharpe      = (ann_ret - RF_RATE) / ann_vol if ann_vol > 0 else 0.0
    mdd         = (equity / equity.cummax() - 1).min()
    ann_to      = turnover.mean() * 252 * 2

    stats = {
        "annual_return":   round(ann_ret * 100, 2),
        "annual_vol":      round(ann_vol * 100, 2),
        "sharpe":          round(sharpe, 3),
        "max_drawdown":    round(mdd * 100, 2),
        "annual_turnover": round(ann_to * 100, 1),
    }
    return stats, equity


# ══════════════════════════════════════════════════════════════
# Walk-Forward 主程式
# ══════════════════════════════════════════════════════════════

def walk_forward(db_path: str = DB_PATH) -> None:
    print("\n" + "="*60)
    print("  Walk-Forward Out-of-Sample 驗證")
    print("="*60)

    # 一次載入全部資料
    print("📂 載入全部資料...")
    all_data = load_all_matrices(db_path)

    folds      = range(FIRST_TEST_YR, LAST_TEST_YR + 1)
    fold_stats = []
    oos_equity_parts: List[pd.Series] = []

    for test_year in folds:
        train_end = f"{test_year - 1}-12-31"
        test_start = f"{test_year}-01-01"
        test_end   = f"{test_year}-12-31"

        print(f"\n── Fold {test_year} "
              f"（訓練 {TRAIN_START}~{train_end}｜測試 {test_start}~{test_end}）──")

        # ── 訓練：計算 ICIR 權重 ────────────────────────────────
        train_factors = build_factors_slice(all_data, TRAIN_START, train_end)
        weights = icir_weights(train_factors, train_factors["close"])

        weight_str = "  ".join(f"{k}={v:.2f}" for k, v in weights.items())
        print(f"  因子權重（ICIR 比例）：{weight_str}")

        # ── 測試：用訓練權重在 OOS 期間建倉回測 ─────────────────
        # 注意：OOS 因子建構時仍需要前期資料（252 天動能 / 60 天波動）
        # 因此從訓練集末尾往前拉 300 天作為 warm-up，只統計測試年績效
        warmup_start = f"{test_year - 1}-01-01"
        test_factors = build_factors_slice(all_data, warmup_start, test_end)

        # 只取測試期的複合因子
        composite_full = build_composite(test_factors, weights)
        close_full     = test_factors["close"]

        composite_oos  = composite_full.loc[test_start:test_end]
        close_oos      = close_full.loc[test_start:test_end]

        # 部位矩陣（用完整 warm-up 期建立，取 OOS 那段）
        position_full = build_positions_oos(composite_full, close_full)
        position_oos  = position_full.loc[test_start:test_end].reindex(
                            columns=close_oos.columns, fill_value=0.0)

        stats, equity = backtest_slice(close_oos, position_oos)
        stats["year"] = test_year
        fold_stats.append(stats)
        oos_equity_parts.append(equity)

        print(f"  OOS 年化報酬 : {stats['annual_return']:+.1f}%")
        print(f"  OOS Sharpe   : {stats['sharpe']:.3f}")
        print(f"  OOS 最大回撤 : {stats['max_drawdown']:.1f}%")
        print(f"  OOS 換手率   : {stats['annual_turnover']:.1f}%")

    # ── 整體 OOS 績效 ──────────────────────────────────────────
    print("\n" + "="*60)

    if oos_equity_parts:
        # 串接各年淨值（每年從前一年末尾繼續）
        oos_equity = oos_equity_parts[0].copy()
        for part in oos_equity_parts[1:]:
            scale = oos_equity.iloc[-1]
            oos_equity = pd.concat([oos_equity, part * scale])

        total_ret   = oos_equity.iloc[-1] - 1
        n_days      = len(oos_equity)
        ann_ret     = (1 + total_ret) ** (252 / n_days) - 1
        ann_vol_all = oos_equity.pct_change().dropna().std() * np.sqrt(252)
        sharpe_all  = (ann_ret - RF_RATE) / ann_vol_all if ann_vol_all > 0 else 0.0
        mdd_all     = (oos_equity / oos_equity.cummax() - 1).min()

        overall = {
            "annual_return": round(ann_ret * 100, 2),
            "sharpe":        round(sharpe_all, 3),
            "max_drawdown":  round(mdd_all * 100, 2),
        }

        print("  Walk-Forward 整體 OOS 績效")
        print(f"  年化報酬  : {overall['annual_return']:+.1f}%")
        print(f"  Sharpe    : {overall['sharpe']:.3f}")
        print(f"  最大回撤  : {overall['max_drawdown']:.1f}%")
    else:
        overall = {}

    # ── 儲存結果 ───────────────────────────────────────────────
    Path("reports").mkdir(exist_ok=True)

    result = {"folds": fold_stats, "overall": overall}
    json_path = "reports/walk_forward_results.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    md_path = "reports/walk_forward_results.md"
    _write_md_report(fold_stats, overall, md_path)

    print(f"\n  💾 結果已儲存：{json_path}")
    print(f"  💾 報告已儲存：{md_path}")
    print("="*60 + "\n")


def _write_md_report(fold_stats: list, overall: dict, path: str) -> None:
    lines = [
        "# Walk-Forward Out-of-Sample Validation",
        "",
        "**Strategy**: Taiwan Multi-Factor (Momentum / Value / Revenue YoY / Low-Vol / Institutional Flow)",
        f"**Training start**: {TRAIN_START} (expanding window)",
        f"**OOS period**: {FIRST_TEST_YR}–{LAST_TEST_YR}",
        "",
        "## Per-Year OOS Results",
        "",
        "| Year | Annual Return | Sharpe | Max Drawdown | Turnover |",
        "|------|:-------------:|:------:|:------------:|:--------:|",
    ]
    for s in fold_stats:
        lines.append(
            f"| {s['year']} "
            f"| {s['annual_return']:+.1f}% "
            f"| {s['sharpe']:.3f} "
            f"| {s['max_drawdown']:.1f}% "
            f"| {s['annual_turnover']:.0f}% |"
        )

    if overall:
        lines += [
            "",
            "## Overall OOS Performance",
            "",
            f"| Annual Return | Sharpe | Max Drawdown |",
            f"|:-------------:|:------:|:------------:|",
            f"| {overall['annual_return']:+.1f}% "
            f"| {overall['sharpe']:.3f} "
            f"| {overall['max_drawdown']:.1f}% |",
        ]

    lines += [
        "",
        "> OOS results use ICIR-proportional factor weights estimated on the training window.",
        "> Each fold's weights are recalculated independently to avoid look-ahead bias.",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    walk_forward()
