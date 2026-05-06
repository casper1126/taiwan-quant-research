"""
cta_module.py
─────────────────────────────────────────────────────────────
CTA 趨勢追蹤模組（I：獨立 CTA 策略）

設計依據（你的 strategy_RD.txt 第 5 章）：
  CTA = Commodity Trading Advisor，現指「跨資產的趨勢追蹤策略」。
  核心：價格趨勢一旦形成會持續一段時間（行為偏誤 + 機構大單慢慢建倉）。
  代表論文：Moskowitz, Ooi, Pedersen (2012) "Time Series Momentum"。

訊號設計（混合 TSMOM + 雙均線）：
  ──────────────────────────────────────────
  signal_TSMOM    = sign(past 252-day return)        ← 1 年趨勢
  signal_FAST     = (price > MA50) AND (MA50 > MA200) ← 經典金叉
  signal_combined = (TSMOM == 1) AND (FAST == True)
  ──────────────────────────────────────────

  signal = 1 → 多頭（long 等權台股代理）
  signal = 0 → 空手（cash，年化 1.5% 假設）

部位邏輯（純多頭，台股放空成本高）：
  - 多頭時：滿倉持有「等權流動宇宙報酬指數」（同 quant_layer3 的 proxy）
  - 空手時：cash，賺無風險利率

Crisis Alpha（你研究筆記第 5.3 節）：
  CTA 在 2008 / 2020 系統性危機都能獲利，因為跌破年線時自動空手。
  這是它跟多因子策略最大的互補價值。

直接執行：
  python strategy/cta_module.py             — 跑 CTA 標準回測
  python strategy/cta_module.py --combine   — 跑 70% L3 + 30% CTA 組合
"""
import sys
import warnings
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE, REPORT_START,
    COMMISSION, SLIPPAGE, RF_RATE,
    load_matrices, build_factors,
)

# ── CTA 參數 ──────────────────────────────────────────────────
TSMOM_LOOKBACK = 252        # 1 年趨勢
FAST_MA        = 50         # 短期均線
SLOW_MA        = 200        # 長期均線
CTA_COST       = COMMISSION + SLIPPAGE   # 進出 ETF 成本（無證交稅）

# ── 組合權重 ──────────────────────────────────────────────────
W_L3   = 0.70
W_CTA  = 0.30


# ══════════════════════════════════════════════════════════════
# 1. 建立流動宇宙等權報酬指數（與 quant_layer3 的 proxy 一致）
# ══════════════════════════════════════════════════════════════

def build_proxy(factors: dict) -> pd.Series:
    """流動宇宙日報酬等權平均，累乘成指數。代表台股大盤近似。"""
    close       = factors["close"]
    liquid_mask = factors["liquid_mask"]
    daily_ret   = close.where(liquid_mask, np.nan).pct_change()
    proxy_ret   = daily_ret.mean(axis=1).fillna(0.0)
    return (1.0 + proxy_ret).cumprod()


# ══════════════════════════════════════════════════════════════
# 2. CTA 訊號：TSMOM × 雙均線
# ══════════════════════════════════════════════════════════════

def cta_signal(proxy: pd.Series,
               tsmom_lb: int = 120,
               fast: int = FAST_MA,
               slow: int = SLOW_MA,
               mode: str = "graduated") -> pd.Series:
    """
    複合趨勢訊號（v2 — 軟化版）：

    mode = "strict": 必須 TSMOM AND 金叉，最嚴格（多頭時間 ~30%）
    mode = "or":     TSMOM OR 金叉，較寬鬆（多頭時間 ~70%）
    mode = "graduated": 0/0.5/1 三級曝險（推薦）
                  TSMOM AND 金叉 → 1.0  滿倉
                  TSMOM OR 金叉  → 0.5  半倉
                  皆不滿足       → 0.0  空手

    改動：
      - tsmom_lb: 252 → 120 天（台股趨勢較短，Renaissance 也用 60-180）
      - 多了 graduated 模式，避免 binary on/off 頻繁切換
    """
    # TSMOM（120 天）
    tsmom_ret = proxy.pct_change(tsmom_lb)
    tsmom_sig = (tsmom_ret > 0).astype(int)

    # 雙均線（金叉）
    ma_fast = proxy.rolling(fast,  min_periods=fast  // 2).mean()
    ma_slow = proxy.rolling(slow,  min_periods=slow  // 2).mean()
    ma_sig  = ((proxy > ma_fast) & (ma_fast > ma_slow)).astype(int)

    if mode == "strict":
        signal = (tsmom_sig & ma_sig).astype(float)
    elif mode == "or":
        signal = (tsmom_sig | ma_sig).astype(float)
    else:  # graduated
        # both: 1.0 ; either: 0.5 ; neither: 0.0
        signal = (tsmom_sig + ma_sig) / 2.0

    return signal.shift(1).fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 3. CTA 回測（純多頭，標的 = proxy 等權指數）
# ══════════════════════════════════════════════════════════════

def backtest_cta(proxy: pd.Series,
                 signal: pd.Series,
                 cost_per_trade: float = CTA_COST) -> Tuple[dict, pd.Series]:
    """
    CTA 純多頭回測。

    收益模型：
      持有時報酬 = proxy 日報酬
      空手時報酬 = 無風險利率 / 252（年化 RF_RATE）

    交易成本：
      只在訊號變化時扣費（進場/出場）
      Taiwan ETF 假設：手續費 0.1425% + 滑價 0.1%（無證交稅）
    """
    proxy_ret = proxy.pct_change().fillna(0.0)
    rf_daily  = RF_RATE / 252.0

    # 持有報酬 + 現金報酬
    pos_ret = signal * proxy_ret + (1 - signal) * rf_daily

    # 換倉成本：訊號從 0→1 或 1→0 都計一次成本
    flips     = signal.diff().abs().fillna(0.0)
    trade_cost = flips * cost_per_trade
    net_ret   = pos_ret - trade_cost

    # 從 REPORT_START 開始算淨值
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
    pos_days  = (signal.loc[mask] > 0).sum()
    total_d   = mask.sum()

    stats = {
        "total_ret":   round(total_ret * 100, 2),
        "cagr":        round(cagr * 100, 2),
        "annual_vol":  round(vol * 100, 2),
        "sharpe":      round(sharpe, 3),
        "max_dd":      round(mdd * 100, 2),
        "long_pct":    round(pos_days / total_d * 100, 1),
        "n_trades":    int(flips.loc[mask].sum() / 2),  # 一次完整循環 = 2 次 flip
    }
    return stats, equity


# ══════════════════════════════════════════════════════════════
# 4. 與 Layer 3 多因子策略組合
# ══════════════════════════════════════════════════════════════

def ensemble_with_l3(equity_l3: pd.Series,
                     equity_cta: pd.Series,
                     w_l3: float = W_L3,
                     w_cta: float = W_CTA) -> pd.Series:
    """
    用「日報酬加權」組合兩個策略的淨值曲線。

    為什麼用日報酬而非淨值線性平均：
      淨值平均會在某策略大漲時失真（複利效應）；
      日報酬加權才是正確的「組合配置」(reblanced daily)。
    """
    common  = equity_l3.index.intersection(equity_cta.index)
    if len(common) < 2:
        raise ValueError("L3 與 CTA 淨值無共同期間")

    ret_l3  = equity_l3.reindex(common).pct_change().fillna(0.0)
    ret_cta = equity_cta.reindex(common).pct_change().fillna(0.0)

    blend   = w_l3 * ret_l3 + w_cta * ret_cta
    return (1 + blend).cumprod()


def report_metrics(equity: pd.Series, label: str) -> dict:
    """印出標準績效指標。"""
    rets   = equity.pct_change().dropna()
    years  = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr   = (equity.iloc[-1]) ** (1 / years) - 1
    vol    = rets.std() * np.sqrt(252)
    sharpe = (cagr - RF_RATE) / vol if vol > 0 else 0.0
    mdd    = (equity / equity.cummax() - 1).min()
    print(f"\n  ─── {label} ───")
    print(f"    CAGR        {cagr*100:+.2f}%")
    print(f"    年化波動    {vol*100:.2f}%")
    print(f"    Sharpe      {sharpe:.3f}")
    print(f"    最大回撤    {mdd*100:+.2f}%")
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "mdd": mdd}


# ══════════════════════════════════════════════════════════════
# 5. 主程式
# ══════════════════════════════════════════════════════════════

def run_cta_pipeline(combine_with_l3: bool = False) -> None:
    print("\n" + "=" * 60)
    print("  CTA 趨勢追蹤模組（I）")
    print("=" * 60)

    print("📂 載入資料 + 建構 proxy...")
    data    = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    factors = build_factors(data)
    proxy   = build_proxy(factors)

    print(f"\n🧮 計算 CTA 訊號（TSMOM={TSMOM_LOOKBACK}d, MA={FAST_MA}/{SLOW_MA}）...")
    signal = cta_signal(proxy)

    print("📈 CTA 標準回測...")
    cta_stats, cta_eq = backtest_cta(proxy, signal)

    print("\n" + "=" * 60)
    print("  CTA 標準回測結果")
    print("=" * 60)
    for k, v in cta_stats.items():
        print(f"    {k:<12} {v}")

    Path("reports").mkdir(exist_ok=True)
    cta_eq.to_frame("equity").to_csv("reports/equity_curve_cta.csv")
    print(f"  💾 reports/equity_curve_cta.csv")

    if combine_with_l3:
        l3_path = "reports/equity_curve_L3_ml.csv"   # ML 版本
        if not Path(l3_path).exists():
            l3_path = "reports/equity_curve_L3.csv"
        print(f"\n📂 讀取 L3 淨值：{l3_path}")
        l3_eq = pd.read_csv(l3_path, parse_dates=["date"]).set_index("date")["equity"]

        # 對齊起點為 1.0
        cta_norm = cta_eq / cta_eq.iloc[0]
        l3_norm  = l3_eq  / l3_eq.iloc[0]

        print("\n📊 組合（70% L3 + 30% CTA，日報酬加權）...")
        combined = ensemble_with_l3(l3_norm, cta_norm, W_L3, W_CTA)

        report_metrics(l3_norm,    f"Layer 3 標準（100%）")
        report_metrics(cta_norm,   f"CTA 標準（100%）")
        report_metrics(combined,   f"組合 ({W_L3*100:.0f}% L3 + {W_CTA*100:.0f}% CTA)")

        combined.to_frame("equity").to_csv("reports/equity_curve_combined.csv")
        print(f"\n  💾 reports/equity_curve_combined.csv")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--combine", action="store_true",
                   help="與 L3 多因子做 70/30 組合")
    args = p.parse_args()
    run_cta_pipeline(combine_with_l3=args.combine)
