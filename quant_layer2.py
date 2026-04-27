"""
quant_layer2.py
─────────────────────────────────────────────────────────────
台股多因子策略：因子引擎 + 向量化回測引擎

搭配 GUIDE.md 教學文件閱讀。
直接執行：python quant_layer2.py
"""

import sqlite3
import warnings
from pathlib import Path
from typing import Tuple, Dict, Optional

import numpy as np
import pandas as pd
from pandas.tseries.offsets import DateOffset

warnings.filterwarnings("ignore", category=FutureWarning)

# ── 設定（修改這裡調整策略參數） ──────────────────────────────
DB_PATH    = "data/taiwan_stock.db"
START_DATE = "2015-01-01"
END_DATE   = "2026-04-17"

LOOKBACK   = 120      # 動能因子回溯天數（~6個月）
TOP_N      = 10       # 每期持有檔數
REBAL_FREQ = 20       # 每幾個交易日再平衡（20 = 約每月）

COMMISSION = 0.001425  # 買賣各 0.1425%
TAX        = 0.003     # 證交稅 0.3%（只有賣出）
SLIPPAGE   = 0.001     # 滑價估計 0.1%
RF_RATE    = 0.015     # 台灣無風險利率（1.5%）


# ══════════════════════════════════════════════════════════════
# PART 1  資料載入
# ══════════════════════════════════════════════════════════════

def load_matrices(db_path: str, start: str, end: str) -> Dict[str, pd.DataFrame]:
    """
    從 SQLite 讀取三種資料，轉為寬格式矩陣（index=date, columns=stock_id）。

    為什麼用寬格式？
    因子計算的核心是「橫截面比較」：在同一天，同時比較所有股票的值。
    寬格式讓這個操作只需要一行：
        close.pct_change(120).rank(axis=1)   ← axis=1 = 沿股票方向排名
    """
    print(f"📂 載入資料 {start} ~ {end}...")
    conn = sqlite3.connect(db_path)

    # 日頻價格
    price_df = pd.read_sql(
        f"SELECT date, stock_id, close, volume FROM daily_price "
        f"WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )

    # 日頻估值
    val_df = pd.read_sql(
        f"SELECT date, stock_id, PER, PBR, dividend_yield FROM daily_valuation "
        f"WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )

    # 月頻營收（不加日期篩選，保留全部歷史以算 YoY）
    rev_df = pd.read_sql(
        "SELECT date, stock_id, revenue FROM monthly_revenue ORDER BY date",
        conn, parse_dates=["date"],
    )

    conn.close()

    def wide(df, col):
        return df.pivot(index="date", columns="stock_id", values=col)

    data = {
        "close":   wide(price_df, "close"),
        "volume":  wide(price_df, "volume"),
        "PER":     wide(val_df,   "PER"),
        "PBR":     wide(val_df,   "PBR"),
        "div_yld": wide(val_df,   "dividend_yield"),
        "revenue": wide(rev_df,   "revenue"),
    }

    n_stocks = data["close"].shape[1]
    n_days   = data["close"].shape[0]
    print(f"✅ 載入完成：{n_stocks} 檔股票 × {n_days} 個交易日")
    return data


# ══════════════════════════════════════════════════════════════
# PART 2  因子建構
# ══════════════════════════════════════════════════════════════

def build_rev_yoy(rev_matrix: pd.DataFrame,
                  price_dates: pd.DatetimeIndex) -> pd.DataFrame:
    """
    月營收 → 日頻 YoY，並處理 40 天的公告延遲。

    Look-Ahead Bias 說明：
    FinMind 裡，3 月的營收資料日期標記為 2024-03-01。
    但現實中，這個數字要等到 2024-04-10 才公告。
    如果你在 3 月 1 日就用這個 YoY，等於偷看了未來資訊——
    回測報酬會虛高，放到實盤必然失敗。

    解決方式：把每個月的 YoY 日期往後推 40 天，
    再 forward-fill 到每個交易日，確保任何時間點
    都只使用「已公告」的資料。
    """
    monthly_yoy = rev_matrix.sort_index().pct_change(periods=12)

    # 推遲 40 天（保守估計，確保不會 look-ahead）
    monthly_yoy.index = monthly_yoy.index + DateOffset(days=40)
    monthly_yoy = monthly_yoy.sort_index()

    # 對齊到交易日，空白日用前值填充
    return monthly_yoy.reindex(price_dates, method="ffill")


def build_factors(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """
    建構所有因子。

    因子清單：
    ① momentum  ：過去 LOOKBACK 天報酬率（動能）
    ② value     ：1/PER（本益比倒數，值越高代表越被低估）
    ③ rev_yoy   ：月營收年增率（已處理 40 天延遲）
    ④ bias      ：與 20 日均線的乖離率（防追高用）
    """
    close = data["close"].replace(0.0, np.nan)
    PER   = data["PER"]    # 虧損股 = NaN（Layer 1 已處理）

    # ① 動能因子
    momentum = close.pct_change(periods=LOOKBACK)

    # ② 價值因子：PER 倒數，讓「越便宜 = 因子值越高」
    #    NaN（虧損股）保持 NaN，排名時自動排到最後
    value = (1.0 / PER).replace([np.inf, -np.inf], np.nan)

    # ③ 月營收 YoY
    rev_yoy = build_rev_yoy(data["revenue"], close.index)

    # ④ 乖離率（用於過濾條件，不直接作為排名因子）
    bias = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    return {
        "momentum": momentum,
        "value":    value,
        "rev_yoy":  rev_yoy,
        "bias":     bias,
        "PER":      PER,
        "close":    close,
    }


def compute_ic(factor: pd.DataFrame, fwd_return: pd.DataFrame,
               min_stocks: int = 10) -> pd.Series:
    """
    計算因子的月度 IC（Information Coefficient）。

    IC = 因子排名 與 未來報酬排名 的 Spearman 相關係數。

    IC > 0.05 有效，> 0.10 優秀。
    ICIR（IC均值/IC標準差）> 0.5 代表因子穩定。
    """
    records = []
    for date in factor.index:
        if date not in fwd_return.index:
            continue
        f = factor.loc[date].dropna()
        r = fwd_return.loc[date].dropna()
        common = f.index.intersection(r.index)
        if len(common) < min_stocks:
            continue
        ic = f[common].rank().corr(r[common].rank(), method="spearman")
        records.append({"date": date, "IC": ic})

    if not records:
        return pd.Series(dtype=float, name="IC")
    return pd.DataFrame(records).set_index("date")["IC"]


def print_factor_diagnostics(factors: dict, close: pd.DataFrame):
    """印出每個因子的 IC 摘要，幫助判斷因子是否有預測力"""
    print("\n" + "═"*55)
    print("  📐 因子診斷報告（IC Analysis，預測視窗 = 20 天）")
    print("═"*55)

    # 未來 20 日報酬（因子要預測的目標）
    fwd_ret = close.pct_change(20).shift(-20)

    rows = []
    for name, matrix in factors.items():
        if name in ("bias", "PER", "close"):
            continue
        ic = compute_ic(matrix, fwd_ret)
        if ic.empty:
            continue
        icir = ic.mean() / ic.std() if ic.std() != 0 else 0
        rows.append({
            "因子":        name,
            "IC 均值":     round(ic.mean(), 4),
            "IC 標準差":   round(ic.std(), 4),
            "ICIR":        round(icir, 3),
            "IC>0 比例":   f"{(ic > 0).mean():.1%}",
        })

    if rows:
        print(pd.DataFrame(rows).set_index("因子").to_string())
        print("\n  💡 ICIR > 0.5 且 IC>0 比例 > 55%：值得使用的因子")
    else:
        print("  （資料不足，無法計算）")
    print("═"*55 + "\n")


# ══════════════════════════════════════════════════════════════
# PART 3  部位建構
# ══════════════════════════════════════════════════════════════

def build_market_score(proxy: pd.Series) -> pd.Series:
    """
    四重均線擇時分數（來自你原始程式碼，邏輯正確，保留）。

    股價高於 10/30/60/120 日均線各得 0.25 分。
    score=1.0 → 全倉；score=0.5 → 半倉；score=0 → 空倉。
    用途：在空頭市場自動降低曝險，減少系統性虧損。
    """
    score = pd.Series(0.0, index=proxy.index)
    for w in [10, 30, 60, 120]:
        score += (proxy > proxy.rolling(w).mean()).astype(float)
    return score / 4.0


def build_positions(factors: dict,
                    top_n: int = TOP_N,
                    rebal_freq: int = REBAL_FREQ,
                    bias_cap: float = 0.10,
                    rev_yoy_min: float = 0.0) -> pd.DataFrame:
    """
    根據因子決定每天的持倉比例矩陣。

    建倉邏輯：
    1. 篩選：PER>0（有獲利）＆ Rev YoY>0（正成長）＆ 乖離率<10%（不追高）
    2. 在通過篩選的股票裡，用動能因子排名
    3. 等權持有前 top_n 名（每檔 1/top_n）
    4. 每 rebal_freq 個交易日更新一次（其他天維持不動）
    5. 乘上大盤擇時分數（空頭期自動減倉）

    ⚠️ shift(1) 只在這裡做一次：
    今天計算的訊號，明天才能執行（收盤後看訊號，隔日開盤下單）。
    你原本的程式在 alpha_engine 和 backtest_engine 各做一次，
    等於延遲了兩天——這個版本統一在最後只做一次。
    """
    close    = factors["close"]
    momentum = factors["momentum"]
    bias     = factors["bias"]
    rev_yoy  = factors["rev_yoy"]
    PER      = factors["PER"]

    # ── 篩選條件（所有條件同時成立才通過）────────────────────
    # NaN 在比較式中回傳 False，自動被排除，不需要特別處理
    valid = (
        (PER > 0)
        & (rev_yoy >= rev_yoy_min)
        & (bias < bias_cap)
        & (momentum > 0)
    )

    # ── 動能排名（只在通過篩選的標的中排序）─────────────────
    masked_mom = momentum.where(valid, np.nan)
    rank       = masked_mom.rank(axis=1, ascending=False)

    # ── 等權重，只取前 top_n 名 ───────────────────────────────
    weight = 1.0 / top_n
    raw_pos = (rank <= top_n).astype(float) * weight

    # ── 只在再平衡日更新，其他日 ffill ───────────────────────
    is_rebal = np.arange(len(close)) % rebal_freq == 0
    pos_on_rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    pos_on_rebal.iloc[is_rebal] = raw_pos.iloc[is_rebal].values
    pos_filled = pos_on_rebal.ffill().fillna(0.0)

    # ── 大盤擇時：以市場中位數當代理指數 ─────────────────────
    proxy_index  = close.median(axis=1).ffill()
    market_score = build_market_score(proxy_index).reindex(close.index).fillna(0.0)
    timed_pos    = pos_filled.mul(market_score, axis=0)

    # ── 唯一一次 shift(1)：今日訊號，明日執行 ────────────────
    final_pos = timed_pos.shift(1).fillna(0.0)

    # 持倉資訊
    avg_holdings = (final_pos > 0).sum(axis=1).replace(0, np.nan).mean()
    print(f"  平均持倉檔數：{avg_holdings:.1f} 檔（目標 {top_n} 檔）")
    return final_pos


# ══════════════════════════════════════════════════════════════
# PART 4  回測引擎
# ══════════════════════════════════════════════════════════════

def run_backtest(close: pd.DataFrame,
                 position: pd.DataFrame,
                 commission: float = COMMISSION,
                 tax: float = TAX,
                 slippage: float = SLIPPAGE) -> Tuple[dict, pd.Series]:
    """
    向量化回測引擎（含交易成本）。

    向量化 vs 事件驅動：
    向量化用矩陣乘法一次算完所有報酬，速度快 100 倍以上。
    缺點是無法精確模擬盤中行為（如限價單 vs 市價單的差異）。
    對因子研究來說，向量化的精度足夠。

    年化報酬用幾何平均（正確做法）：
    (1 + total_return)^(252/n) - 1
    ← 而不是 daily_mean * 252（算術平均，會高估複利）
    """
    print("📈 執行回測...")

    # 每日報酬矩陣
    asset_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # 投資組合每日毛報酬
    # position 已在 build_positions 中 shift(1)，這裡直接用
    gross_ret = (position * asset_ret).sum(axis=1)

    # 交易成本
    turnover   = position.diff().abs().sum(axis=1)
    total_cost = turnover * (commission + slippage + (commission + tax + slippage)) / 2

    net_ret = gross_ret - total_cost

    # 淨值曲線（從 1.0 開始）
    equity = (1 + net_ret).cumprod()

    # ── 績效指標 ──────────────────────────────────────────────
    total_ret = equity.iloc[-1] - 1

    # 幾何平均年化報酬
    n_days      = max((net_ret != 0).sum(), 1)
    annual_ret  = (1 + total_ret) ** (252 / n_days) - 1
    annual_vol  = net_ret.std() * np.sqrt(252)
    sharpe      = (annual_ret - RF_RATE) / annual_vol if annual_vol > 0 else 0.0

    # 最大回撤
    mdd = (equity / equity.cummax() - 1).min()

    # Calmar Ratio = 年化報酬 / 最大回撤絕對值
    calmar = annual_ret / abs(mdd) if mdd != 0 else 0.0

    # 年化換手率
    annual_turnover = turnover.mean() * 252 * 2

    stats = {
        "回測區間":     f"{equity.index[0].date()} ~ {equity.index[-1].date()}",
        "總報酬":        f"{total_ret*100:.1f}%",
        "年化報酬":      f"{annual_ret*100:.1f}%",
        "年化波動度":    f"{annual_vol*100:.1f}%",
        "Sharpe Ratio": f"{sharpe:.2f}",
        "最大回撤":      f"{mdd*100:.1f}%",
        "Calmar Ratio": f"{calmar:.2f}",
        "年化換手率":    f"{annual_turnover:.1%}",
    }
    return stats, equity


# ══════════════════════════════════════════════════════════════
# PART 5  主程式
# ══════════════════════════════════════════════════════════════

def print_report(stats: dict):
    w = 45
    print("\n" + "═"*w)
    print("  📊 回測績效報告")
    print("═"*w)
    for k, v in stats.items():
        print(f"  {k:<15} {v}")
    print("═"*w)

    # 健康診斷
    try:
        sharpe = float(stats["Sharpe Ratio"])
        mdd    = float(stats["最大回撤"].replace("%", ""))
        ann_r  = float(stats["年化報酬"].replace("%", ""))
        print("\n  🩺 健康診斷：")
        print(f"    Sharpe > 1.0 : {'✅' if sharpe > 1.0 else '⚠️ '} {sharpe:.2f}")
        print(f"    MDD < 20%    : {'✅' if abs(mdd) < 20 else '⚠️ '} {mdd:.1f}%")
        print(f"    年化 > 15%   : {'✅' if ann_r > 15 else '⚠️ '} {ann_r:.1f}%")
        print()
        if sharpe < 0.5:
            print("  ⚠️  Sharpe 偏低：可能因子有效性不足，或交易成本太高")
        if abs(mdd) > 30:
            print("  ⚠️  回撤偏大：考慮收緊大盤擇時條件或降低持倉集中度")
    except Exception:
        pass


def save_equity(equity: pd.Series, path: str = "reports/equity_curve.csv"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    equity.to_frame("equity").to_csv(path)
    print(f"  💾 淨值曲線已儲存：{path}")


if __name__ == "__main__":

    # Step 1：載入資料
    data = load_matrices(DB_PATH, START_DATE, END_DATE)

    if data["close"].shape[1] < 5:
        print("\n⚠️  資料庫裡的股票不足 5 檔，無法做有意義的橫截面排名。")
        print("   請先執行 run.py 的 Step 1 載入所有 CSV。\n")
        exit()

    # Step 2：建構因子
    print("\n🧠 建構因子矩陣...")
    factors = build_factors(data)

    # Step 3：因子診斷
    print_factor_diagnostics(factors, data["close"])

    # Step 4：建構部位
    print("📐 建構投資組合部位...")
    positions = build_positions(
        factors,
        top_n      = TOP_N,
        rebal_freq = REBAL_FREQ,
        bias_cap   = 0.10,
        rev_yoy_min= 0.0,
    )

    # Step 5：回測
    stats, equity = run_backtest(data["close"], positions)

    # Step 6：輸出
    print_report(stats)
    save_equity(equity)

    print("✅ Layer 2 完成！淨值曲線已存到 reports/equity_curve.csv\n")