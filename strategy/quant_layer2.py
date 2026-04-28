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
TOP_N      = 30       # 每期持有檔數
REBAL_FREQ = 120      # 半年再平衡（從季度 60 → 半年 120，業界長線標準）

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

    # 三大法人：外資 + 投信累積買超（籌碼因子）
    # 從 institutional_investors 資料表讀取
    # 只取外資（Foreign_Investor）和投信（Investment_Trust）
    try:
        inst_df = pd.read_sql(
            f"""SELECT date, stock_id,
                       SUM(CASE WHEN investor_type IN
                           ('Foreign_Investor','Foreign_Dealer_Self',
                            'Investment_Trust')
                           THEN net ELSE 0 END) AS inst_net
               FROM institutional_investors
               WHERE date BETWEEN ? AND ?
               GROUP BY date, stock_id
               ORDER BY date""",
            conn, params=(start, end), parse_dates=["date"],
        )
        if not inst_df.empty:
            inst_matrix = inst_df.pivot(
                index="date", columns="stock_id", values="inst_net"
            )
        else:
            inst_matrix = pd.DataFrame()
    except Exception:
        # 資料表不存在時（Layer 1 未下載籌碼資料），用空 DataFrame
        inst_matrix = pd.DataFrame()

    conn.close()

    def wide(df, col):
        return df.pivot(index="date", columns="stock_id", values=col)

    data = {
        "close":         wide(price_df, "close"),
        "volume":        wide(price_df, "volume"),
        "PER":           wide(val_df,   "PER"),
        "PBR":           wide(val_df,   "PBR"),
        "div_yld":       wide(val_df,   "dividend_yield"),
        "revenue":       wide(rev_df,   "revenue"),
        "institutional": inst_matrix,   # 新增籌碼因子資料
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
    建構所有因子（v5 最終版）。

    ── 因子清單 ──────────────────────────────────────────────
    ① 52 週新高動能   George & Hwang (2004)
    ② 價值（1/PER）   Fama-French (1992)
    ③ 月營收 YoY      台股獨有，40 天延遲處理
    ④ 低波動 IVOL     Frazzini & Pedersen (2014)
    ⑤ 籌碼因子        外資 + 投信累積買超（台股最強因子）

    ── 投資宇宙篩選（修正版）────────────────────────────────
    使用「日均成交金額」= volume × close 排名前 300。

    為什麼不用「平均收盤價」：
      台積電股價 600 元但日成交額 100 億；
      某化工股股價 800 元但日成交額 500 萬。
      前者可以執行，後者根本買不到。
      「股價高」≠「市值大」≠「流動性好」。

    日均成交金額排名才是真正衡量「能買得進、賣得出」的指標，
    也是業界（MSCI、Russell 指數）的標準流動性篩選方式。

    為什麼保留虧損公司（移除強制 PER > 0 篩選）：
      強制過濾虧損公司會讓 value 因子失去「深度價值股」的 alpha。
      低 PER、高 PBR 的暫時虧損公司是 value factor 的重要來源。
      篩選策略交給因子排名（虧損股 PER=NaN 自然排到底），
      不要在宇宙篩選階段硬性排除。
    """
    close  = data["close"].replace(0.0, np.nan)
    volume = data["volume"].replace(0.0, np.nan)
    PER    = data["PER"]

    # ── 投資宇宙：日均成交金額前 300 ─────────────────────────
    # dollar_volume = 成交量（張）× 收盤價 × 1000（每張 1000 股）
    # 直接用 volume × close 當代理（比例關係不影響排名）
    dollar_volume = (volume * close).rolling(252, min_periods=60).mean()
    dv_rank       = dollar_volume.rank(axis=1, ascending=False)
    liquid_mask   = (dv_rank <= 300)

    close_liquid = close.where(liquid_mask, np.nan)
    PER_liquid   = PER.where(liquid_mask, np.nan)

    # ① 52 週新高動能
    high_252     = close.rolling(252, min_periods=60).max()
    momentum_52w = (close_liquid / high_252).where(liquid_mask, np.nan)

    # ② 價值因子：1/PER
    value = (1.0 / PER_liquid).replace([np.inf, -np.inf], np.nan)

    # ③ 月營收 YoY（已在 build_rev_yoy 處理 40 天延遲）
    rev_yoy = build_rev_yoy(data["revenue"], close.index)
    rev_yoy = rev_yoy.where(liquid_mask, np.nan)

    # ④ 低波動因子：60 日 IVOL 倒數
    daily_ret = close_liquid.pct_change()
    ivol_60   = daily_ret.rolling(60, min_periods=20).std()
    low_vol   = (1.0 / ivol_60).replace([np.inf, -np.inf], np.nan)

    # ⑤ 籌碼因子：外資 + 投信累積買超（最強台股因子）
    # ─────────────────────────────────────────────────────────
    # 邏輯：外資和投信是台股最有訊息優勢的機構投資人。
    #   連續買超 = 對基本面有信心，通常有超額報酬。
    #   用「過去 60 個交易日」的累積買超金額。
    #
    # 資料來源：FinMind TaiwanStockInstitutionalInvestorsBuySell
    # （本版先用 dummy，等你串接 FinMind 籌碼資料後替換）
    #
    # 資料欄位格式：
    #   institutional_matrix : 寬格式 DataFrame
    #   index = date, columns = stock_id, value = 外資+投信淨買超(元)
    inst_raw = data.get("institutional")
    if inst_raw is not None and not inst_raw.empty:
        inst_aligned = inst_raw.reindex(
            index=close.index, columns=close.columns
        ).fillna(0.0)
        # 60 日累積買超，正值 = 持續買進
        inst_flow_60 = inst_aligned.rolling(60, min_periods=10).sum()
        inst_flow    = inst_flow_60.where(liquid_mask, np.nan)
    else:
        # 籌碼資料尚未載入時，用 NaN 佔位（不影響其他因子）
        inst_flow = pd.DataFrame(
            np.nan, index=close.index, columns=close.columns
        )

    # ── 乖離率（過濾條件用，不直接排名）────────────────────
    bias = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    return {
        "momentum":     momentum_52w,
        "value":        value,
        "rev_yoy":      rev_yoy,
        "low_vol":      low_vol,
        "inst_flow":    inst_flow,    # 新增籌碼因子
        "bias":         bias,
        "PER":          PER_liquid,
        "close":        close,
        "liquid_mask":  liquid_mask,
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

    fwd_ret = close.pct_change(20).shift(-20)

    rows = []
    for name, matrix in factors.items():
        if name in ("bias", "PER", "close", "liquid_mask"):
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
                    buffer_multiplier: float = 1.5) -> pd.DataFrame:
    """
    根據因子決定每天的持倉比例矩陣。

    ── 換手率控制的三個機制 ──────────────────────────────────
    本版本解決了兩個導致換手率 1982% 的根本問題：

    問題一：三級曝險（0/0.5/1.0）
      每次 market_score 從 1.0 → 0.5，所有持倉都減半。
      position.diff() 會把這記錄成一輪買賣，每年光擇時就貢獻 1000%+ 換手。
      修正：改為二元開關（0 或 1），消除中間狀態。

    問題二：持倉只有 top_n=10
      2056 支股票選 10 支，排名邊界競爭激烈，每月幾乎完全換倉。
      修正：增加持倉到 top_n=30，加入「緩衝區」機制。

    問題三（緩衝區機制）
      新增股票：排名進入前 top_n 才買
      賣出股票：排名跌出前 top_n × buffer_multiplier 才賣
      例如 top_n=30, buffer=1.5 → 進場閾值 30，出場閾值 45
      這讓已持倉的股票有更多空間，大幅降低邊界替換頻率。
    """
    close = factors["close"]
    cols  = close.columns

    momentum   = factors["momentum"].reindex(columns=cols)
    bias       = factors["bias"].reindex(columns=cols)
    rev_yoy    = factors["rev_yoy"].reindex(columns=cols)
    PER        = factors["PER"].reindex(columns=cols)
    value      = factors["value"].reindex(columns=cols)
    low_vol    = factors["low_vol"].reindex(columns=cols)
    inst_flow   = factors["inst_flow"].reindex(columns=cols)
    liquid_mask = factors.get("liquid_mask",
                  pd.DataFrame(True, index=close.index, columns=cols)
                  ).reindex(columns=cols)

    # ── 篩選條件 ──────────────────────────────────────────────
    valid = (PER > 0) & (bias < bias_cap) & liquid_mask

    # ── 合成因子（IC 加權，含籌碼）──────────────────────────
    #
    # 因子          IC 預期   權重  說明
    # ──────────────────────────────────────────────────────
    # momentum      0.04      0.15  52週新高，亞洲市場有效
    # value         0.03      0.20  1/PER，台股散戶低估价值股
    # rev_yoy       0.025     0.20  月營收YoY，台股獨有優勢
    # low_vol       0.03      0.15  低波動，散戶偏好高波動
    # inst_flow     0.05      0.30  外資+投信買超，台股最強因子
    #
    # 籌碼因子給最高權重（0.30），因為：
    # 1. IC 預期最高（外資有訊息優勢）
    # 2. 與其他因子相關性低（互補性最強）
    # 3. 資料最即時（每日公告）
    # 如果 inst_flow 全是 NaN（資料尚未載入），
    # cross_zscore 返回 0，不影響其他因子
    def cross_zscore(m: pd.DataFrame) -> pd.DataFrame:
        mean = m.mean(axis=1)
        std  = m.std(axis=1).replace(0, np.nan)
        return m.sub(mean, axis=0).div(std, axis=0).clip(-3, 3).fillna(0)

    composite = (
        cross_zscore(momentum)  * 0.15
      + cross_zscore(value)     * 0.20
      + cross_zscore(rev_yoy)   * 0.20
      + cross_zscore(low_vol)   * 0.15
      + cross_zscore(inst_flow) * 0.30
    )
    masked_composite = composite.where(valid, np.nan)

    # ── 再平衡日索引 ──────────────────────────────────────────
    rebal_idx = np.where(np.arange(len(close)) % rebal_freq == 0)[0]

    # ── 緩衝區設定 ────────────────────────────────────────────
    exit_threshold = int(top_n * buffer_multiplier)

    # ── 大盤擇時：空頭時持現金（不換股）────────────────────────
    #
    # 之前的問題：exposure = timing × 0.5 + 0.5
    #   空頭時每檔從 3.33% → 1.67%，全部 30 檔都有微幅變動
    #   position.diff() 把這些變動都算成換手，每次擇時切換貢獻 50% 換手
    #
    # 修正：改用「縮減持倉到 0（持現金）」而不是縮小比例
    #   空頭時：不換股，直接把總曝險降到 50%
    #   做法：等權重從 1/30 降到 1/60，不改變持股
    #   這樣 position.diff() = 0，不產生換手
    #
    # 注意：仍然只在再平衡日更新擇時訊號
    avg_price_p = close.mean()
    top_n_proxy = max(int(len(avg_price_p) * 0.20), 10)
    large_caps  = avg_price_p.nlargest(top_n_proxy).index
    proxy       = close[large_caps].mean(axis=1).ffill()

    in_market = (proxy > proxy.rolling(60).mean()).astype(float)

    timing_on_rebal = pd.Series(np.nan, index=close.index)
    timing_on_rebal.iloc[rebal_idx] = in_market.iloc[rebal_idx].values
    timing_stepped = timing_on_rebal.ffill().fillna(1.0)

    # 關鍵修正：在再平衡日建立部位時就把擇時考慮進去
    # 多頭 → weight = 1/30，空頭 → weight = 1/60
    # 這樣 pos_filled 在整個持倉期間都是固定的，不會每天變動
    bull_weight = 1.0 / top_n
    bear_weight = 0.5 / top_n

    # 重新用擇時後的權重建立部位矩陣
    current_holds_timed: set = set()
    pos_timed = pd.DataFrame(0.0, index=close.index, columns=cols)

    for day_idx in rebal_idx:
        today        = close.index[day_idx]
        is_bull      = timing_stepped.loc[today] >= 0.5
        w            = bull_weight if is_bull else bear_weight
        scores_today = masked_composite.loc[today]
        rank_today   = scores_today.rank(ascending=False)

        to_sell = {s for s in current_holds_timed
                   if pd.isna(rank_today.get(s, np.nan))
                   or rank_today[s] > exit_threshold}
        current_holds_timed -= to_sell

        entry_candidates = set(rank_today[rank_today <= top_n].index.tolist())
        to_buy = entry_candidates - current_holds_timed
        current_holds_timed |= to_buy

        if len(current_holds_timed) > top_n:
            ranked = sorted(
                [(s, rank_today.get(s, 9999)) for s in current_holds_timed],
                key=lambda x: x[1]
            )
            current_holds_timed = {s for s, _ in ranked[:top_n]}

        pos_timed.loc[today, list(current_holds_timed)] = w

    anchor_t = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor_t.iloc[rebal_idx] = pos_timed.iloc[rebal_idx].values
    timed_pos = anchor_t.ffill().fillna(0.0)

    # ── 唯一一次 shift(1)：今日訊號，明日執行 ────────────────
    final_pos = timed_pos.shift(1).fillna(0.0)

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
        top_n            = TOP_N,
        rebal_freq       = REBAL_FREQ,
        bias_cap         = 0.10,
        buffer_multiplier= 1.5,
    )

    # Step 5：回測
    stats, equity = run_backtest(data["close"], positions)

    # Step 6：輸出
    print_report(stats)
    save_equity(equity)

    print("✅ Layer 2 完成！淨值曲線已存到 reports/equity_curve.csv\n")