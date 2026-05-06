"""
pead_module.py
─────────────────────────────────────────────────────────────
PEAD（Post-Earnings Announcement Drift）月營收事件驅動策略 — J

理論根源：
  Bernard, Thomas (1989) "Post-Earnings-Announcement Drift"
  → 盈餘超預期的公司，未來幾週會繼續上漲
  → 是金融學最穩定的異常之一（IC 衰減慢）

台股應用（你研究筆記第 6.2 節）：
  台股每月 10 日前公告月營收 → 比季 EPS 更即時
  「月營收 YoY 超預期」是台股獨有的事件訊號

設計：
  Surprise = 本月 YoY − 過去 12 個月 YoY 移動平均
            （正向 surprise = 加速成長）

  持倉邏輯：
    1. 每月公告日（用月營收日期 + 40 天延遲建模）
    2. 取 surprise 前 N 檔等權持有
    3. 持有 20 個交易日後出場
    4. 不擇時（純事件驅動）

  與 L3 多因子組合：
    PEAD 每月只持有 ~20 個交易日 + 每月換股
    與 L3 的相關性低（L3 是月度多因子，PEAD 是月度事件）
    組合預期 Sharpe 提升

注意：本模組計算 surprise 用 lookback 12 個月，
      所有 alignment 都已加 40 天公告延遲，無 look-ahead bias。

直接執行：
  python strategy/pead_module.py
  python strategy/pead_module.py --combine    # 與 L3 組合
"""
import sys
import warnings
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset

warnings.filterwarnings("ignore", category=FutureWarning)
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE, REPORT_START,
    COMMISSION, TAX, SLIPPAGE, RF_RATE,
    load_matrices, build_factors, _industry_neutral_zscore,
)

# ── PEAD 參數 ─────────────────────────────────────────────────
SURPRISE_LOOKBACK = 12     # 用過去 12 個月 YoY 估計「期望值」
HOLD_DAYS         = 20     # 公告後持有 20 個交易日
TOP_N_PEAD        = 20     # 每期持有前 20 檔
ANNOUNCE_DELAY    = 40     # 月營收實際公告延遲（防 look-ahead）

# ── 組合權重（PEAD 較不穩，給 20%）────────────────────────────
W_L3_PEAD   = 0.80
W_PEAD      = 0.20


# ══════════════════════════════════════════════════════════════
# 1. 計算每月 surprise 矩陣
# ══════════════════════════════════════════════════════════════

def compute_surprise(rev_matrix: pd.DataFrame,
                     price_dates: pd.DatetimeIndex,
                     lookback: int = SURPRISE_LOOKBACK,
                     delay: int = ANNOUNCE_DELAY) -> pd.DataFrame:
    """
    計算月營收 surprise（比 raw YoY 更乾淨的訊號）。

    Surprise_t = YoY_t − mean(YoY_{t-12 ... t-1})
              = 本月 YoY 高於過去 1 年 YoY 平均多少
              （正向 = 成長加速；負向 = 成長放緩）

    返回 daily wide-format（與 close 同 index），公告延遲已套用。
    """
    # 月度 YoY
    monthly_yoy = rev_matrix.sort_index().pct_change(periods=12)
    # 過去 lookback 個月的 YoY 平均（移動視窗）
    expectation = monthly_yoy.rolling(lookback, min_periods=lookback // 2).mean()
    # surprise = 本月 YoY − 期望
    surprise = monthly_yoy - expectation

    # 公告延遲：月營收日期是「該月 1 日」，實際公告 ~40 天後
    surprise.index = surprise.index + DateOffset(days=delay)
    surprise = surprise.sort_index()

    # forward-fill 到日頻
    return surprise.reindex(price_dates, method="ffill")


# ══════════════════════════════════════════════════════════════
# 2. PEAD 部位建構
# ══════════════════════════════════════════════════════════════

def build_pead_positions(close: pd.DataFrame,
                          surprise: pd.DataFrame,
                          liquid_mask: pd.DataFrame,
                          top_n: int = TOP_N_PEAD,
                          hold_days: int = HOLD_DAYS) -> pd.DataFrame:
    """
    每月公告日（每 21 個交易日）取 surprise 最高的 top_n 檔等權持有。

    持有期 = hold_days 個交易日，到期自動平倉（再下次再進場）。
    這對應「公告後 1 個月的 PEAD drift」。

    產業中性化：在每個產業內計算 surprise 的 z-score，再合併排名
                避免某產業（電子）系統性 surprise 較高。
    """
    cols    = close.columns
    n_days  = len(close)
    rebal_idx = np.arange(0, n_days, hold_days)
    eq_w    = 1.0 / top_n

    positions = pd.DataFrame(0.0, index=close.index, columns=cols)

    # 產業中性化 surprise
    surprise_neutral = _industry_neutral_zscore(surprise.where(liquid_mask))

    for day_idx in rebal_idx:
        today = close.index[day_idx]
        scores = surprise_neutral.loc[today]
        # 取 surprise 最高的 top_n 檔
        ranked = scores.sort_values(ascending=False)
        top_stocks = ranked.dropna().head(top_n).index
        if len(top_stocks) == 0:
            continue
        # 設置該日權重
        positions.loc[today, top_stocks] = eq_w

    # ffill hold_days，shift(1) 防 look-ahead
    anchor = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor.iloc[rebal_idx] = positions.iloc[rebal_idx].values
    return anchor.ffill().fillna(0.0).shift(1).fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 3. 回測
# ══════════════════════════════════════════════════════════════

def backtest_pead(close: pd.DataFrame,
                   positions: pd.DataFrame) -> Tuple[dict, pd.Series]:
    """PEAD 回測（含台股完整交易成本）。"""
    asset_ret  = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross_ret  = (positions * asset_ret).sum(axis=1)
    turnover   = positions.diff().abs().sum(axis=1)
    cost       = turnover * (COMMISSION + SLIPPAGE + COMMISSION + TAX + SLIPPAGE) / 2.0
    net_ret    = gross_ret - cost

    mask    = net_ret.index >= pd.Timestamp(REPORT_START)
    net_ret = net_ret.loc[mask]
    equity  = (1 + net_ret).cumprod()

    if len(equity) < 2:
        return {}, equity

    total_ret = equity.iloc[-1] - 1
    years     = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr      = (1 + total_ret) ** (1 / years) - 1
    vol       = net_ret.std() * np.sqrt(252)
    sharpe    = (cagr - RF_RATE) / vol if vol > 0 else 0.0
    mdd       = (equity / equity.cummax() - 1).min()
    annual_to = turnover.loc[mask].mean() * 252

    stats = {
        "total_ret":  round(total_ret * 100, 2),
        "cagr":       round(cagr * 100, 2),
        "vol":        round(vol * 100, 2),
        "sharpe":     round(sharpe, 3),
        "max_dd":     round(mdd * 100, 2),
        "ann_to":     round(annual_to * 100, 1),
    }
    return stats, equity


# ══════════════════════════════════════════════════════════════
# 4. 主程式
# ══════════════════════════════════════════════════════════════

def run_pead(combine_with_l3: bool = False) -> None:
    print("\n" + "=" * 60)
    print("  PEAD 月營收事件驅動（J）")
    print("=" * 60)

    print("📂 載入資料...")
    data    = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    factors = build_factors(data)
    close       = factors["close"]
    liquid_mask = factors["liquid_mask"]

    print(f"\n🧮 計算月營收 surprise（lookback {SURPRISE_LOOKBACK}m，"
          f"延遲 {ANNOUNCE_DELAY}d）...")
    surprise = compute_surprise(data["revenue"], close.index)

    print(f"📐 建構 PEAD 部位（top {TOP_N_PEAD}, 持有 {HOLD_DAYS}d）...")
    positions = build_pead_positions(close, surprise, liquid_mask)

    print("📈 PEAD 回測...")
    stats, equity = backtest_pead(close, positions)

    print("\n" + "=" * 60)
    print("  PEAD 標準回測結果")
    print("=" * 60)
    for k, v in stats.items():
        print(f"    {k:<10} {v}")

    Path("reports").mkdir(exist_ok=True)
    equity.to_frame("equity").to_csv("reports/equity_curve_pead.csv")
    print(f"\n  💾 reports/equity_curve_pead.csv")

    if combine_with_l3:
        l3_path = "reports/equity_curve_L3_ml.csv"
        if not Path(l3_path).exists():
            l3_path = "reports/equity_curve_L3.csv"
        print(f"\n📂 讀取 L3 淨值：{l3_path}")
        l3_eq = pd.read_csv(l3_path, parse_dates=["date"]).set_index("date")["equity"]

        common = equity.index.intersection(l3_eq.index)
        l3_norm = (l3_eq.reindex(common) / l3_eq.reindex(common).iloc[0])
        pead_norm = (equity.reindex(common) / equity.reindex(common).iloc[0])

        ret_l3   = l3_norm.pct_change().fillna(0.0)
        ret_pead = pead_norm.pct_change().fillna(0.0)
        combined = (1 + W_L3_PEAD * ret_l3 + W_PEAD * ret_pead).cumprod()

        # 報告
        def _metrics(eq, label):
            rets = eq.pct_change().dropna()
            yrs = (eq.index[-1] - eq.index[0]).days / 365.25
            cagr = eq.iloc[-1] ** (1 / yrs) - 1
            vol  = rets.std() * np.sqrt(252)
            sharpe = (cagr - RF_RATE) / vol if vol > 0 else 0.0
            mdd  = (eq / eq.cummax() - 1).min()
            print(f"\n  ─── {label} ───")
            print(f"    CAGR     {cagr*100:+.2f}%")
            print(f"    Sharpe   {sharpe:.3f}")
            print(f"    MDD      {mdd*100:+.2f}%")

        _metrics(l3_norm,   "Layer 3（100%）")
        _metrics(pead_norm, "PEAD（100%）")
        _metrics(combined,  f"組合 ({W_L3_PEAD*100:.0f}% L3 + {W_PEAD*100:.0f}% PEAD)")

        combined.to_frame("equity").to_csv("reports/equity_curve_combined_pead.csv")
        print(f"\n  💾 reports/equity_curve_combined_pead.csv")
        # 兩個策略相關性
        corr = ret_l3.corr(ret_pead)
        print(f"\n  📐 L3 vs PEAD 日報酬相關性：{corr:.3f}")
        print(f"     (相關性越低，組合分散效益越好)")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--combine", action="store_true",
                   help="與 L3 多因子做 80/20 組合")
    args = p.parse_args()
    run_pead(combine_with_l3=args.combine)
