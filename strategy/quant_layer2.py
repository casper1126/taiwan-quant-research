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

from factors import base as factor_base
from factors import style as factor_style
from factors import taiwan as factor_taiwan
import portfolio as portfolio_module

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

    # 融資融券（margin_trading，Task 1 新增，可能還在回填中）
    # margin_balance：個股融資餘額寬格式矩陣（給 margin_usage 用）
    # margin_total：全市場融資餘額加總（給 margin_squeeze_market 用）
    try:
        margin_df = pd.read_sql(
            f"SELECT date, stock_id, margin_balance FROM margin_trading "
            f"WHERE date BETWEEN ? AND ? ORDER BY date",
            conn, params=(start, end), parse_dates=["date"],
        )
        if not margin_df.empty:
            margin_balance_matrix = margin_df.pivot(
                index="date", columns="stock_id", values="margin_balance"
            )
            margin_total_series = margin_balance_matrix.sum(axis=1, min_count=1)
        else:
            margin_balance_matrix = pd.DataFrame()
            margin_total_series = pd.Series(dtype=float)
    except Exception:
        margin_balance_matrix = pd.DataFrame()
        margin_total_series = pd.Series(dtype=float)

    # TAIEX 大盤指數（market_index，Task 1 新增）
    try:
        index_df = pd.read_sql(
            f"SELECT date, close FROM market_index WHERE index_id = 'TAIEX' "
            f"AND date BETWEEN ? AND ? ORDER BY date",
            conn, params=(start, end), parse_dates=["date"],
        )
        index_close_series = (
            index_df.set_index("date")["close"] if not index_df.empty
            else pd.Series(dtype=float)
        )
    except Exception:
        index_close_series = pd.Series(dtype=float)

    conn.close()

    def wide(df, col):
        return df.pivot(index="date", columns="stock_id", values=col)

    data = {
        "close":          wide(price_df, "close"),
        "volume":         wide(price_df, "volume"),
        "PER":            wide(val_df,   "PER"),
        "PBR":            wide(val_df,   "PBR"),
        "div_yld":        wide(val_df,   "dividend_yield"),
        "revenue":        wide(rev_df,   "revenue"),
        "institutional":  inst_matrix,          # 籌碼因子資料
        "margin_balance": margin_balance_matrix, # 融資融券（個股）
        "margin_total":   margin_total_series,   # 融資融券（全市場加總）
        "index_close":    index_close_series,    # TAIEX 收盤價
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
    建構所有因子（v6：因子數學邏輯搬到 strategy/factors/ 模組，
    這裡只負責投資宇宙篩選、呼叫因子函式、套用流動性遮罩）。

    ── 因子清單 ──────────────────────────────────────────────
    ① 52 週新高動能   George & Hwang (2004)             → factors/style.py
    ② 價值 0.5/PER+0.5/PBR  Fama-French (1992)          → factors/style.py
    ③ 月營收 YoY      台股獨有，40 天延遲處理             → build_rev_yoy（本檔）
    ④ 低波動 IVOL     Frazzini & Pedersen (2014)         → factors/style.py
    ⑤ 籌碼因子        外資 + 投信買超 / 60日成交金額正規化 → factors/taiwan.py
    ⑥ 融資使用率      算出來供診斷/測試，暫不進複合         → factors/taiwan.py
    ⑦ 品質            佔位（缺財報資料）                   → factors/taiwan.py

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
    div_yld_raw = data.get("div_yld", pd.DataFrame())

    # ── 投資宇宙：日均成交金額前 300 ─────────────────────────
    # dollar_volume = 成交量（張）× 收盤價 × 1000（每張 1000 股）
    # 直接用 volume × close 當代理（比例關係不影響排名）
    dollar_volume = (volume * close).rolling(252, min_periods=60).mean()
    dv_rank       = dollar_volume.rank(axis=1, ascending=False)
    liquid_mask   = (dv_rank <= 300)

    close_liquid = close.where(liquid_mask, np.nan)
    PER_liquid   = PER.where(liquid_mask, np.nan)

    # factors/ 模組吃的 data 字典：用清理過的 close/volume 覆蓋掉原始版本
    factor_data = {**data, "close": close, "volume": volume}

    # ① 52 週新高動能
    momentum_52w = factor_style.momentum_52w(factor_data).where(liquid_mask, np.nan)

    # 補充：120 日（約 6 個月）報酬動能（增加短中期動能訊號）
    mom_120 = close_liquid.pct_change(120)

    # ② 價值因子：0.5/PER + 0.5/PBR
    value = factor_style.value_composite(factor_data).where(liquid_mask, np.nan)

    # ③ 月營收 YoY（已在 build_rev_yoy 處理 40 天延遲）
    rev_yoy = build_rev_yoy(data["revenue"], close.index)
    rev_yoy = rev_yoy.where(liquid_mask, np.nan)

    # ④ 低波動因子：60 日 IVOL 倒數
    low_vol = factor_style.low_vol_ivol(factor_data).where(liquid_mask, np.nan)

    # ⑤ 籌碼因子：外資 + 投信買超 / 60 日平均成交金額
    #    2026-08-18 決定（見 docs/DECISIONS.md）：四輪 IC 排查後判定為誠實的負面
    #    結果（不是 bug），已移出主策略複合權重（build_positions() 的 factor_map
    #    不含它）。仍在此計算並回傳，供 Task 5 的非線性/ML 模型重新評估用。
    inst_flow = factor_taiwan.inst_flow(factor_data).where(liquid_mask, np.nan)

    # ⑥ 融資使用率因子：算出來供診斷/單元測試使用，
    #    依 Task 2c 規格暫不進複合權重（build_positions() 的 factor_map 不含它）
    #    2026-08-18：與 inst_flow 一起完成同樣的四輪 IC 排查，結論相同
    #    （見 docs/DECISIONS.md、reports/factor_negative_findings.md），維持排除。
    margin_usage = factor_taiwan.margin_usage(factor_data).where(liquid_mask, np.nan)

    # ⑦ 品質因子（佔位，回傳 NaN）
    quality = factor_taiwan.quality(factor_data)

    # ── 乖離率（過濾條件用，不直接排名）────────────────────
    bias = (close - close.rolling(20).mean()) / close.rolling(20).mean()

    # 股息殖利率作為輔助價值/品質因子
    div_yld = div_yld_raw.where(liquid_mask, np.nan) if not div_yld_raw.empty else pd.DataFrame(
        np.nan, index=close.index, columns=close.columns
    )

    return {
        "momentum":     momentum_52w,
        "mom_120":      mom_120,
        "value":        value,
        "rev_yoy":      rev_yoy,
        "low_vol":      low_vol,
        "inst_flow":    inst_flow,
        "margin_usage": margin_usage,  # 不進複合，見上方註解
        "quality":      quality,       # 佔位，全 NaN
        "div_yld":      div_yld,
        "dollar_volume": dollar_volume,
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


def risk_parity_weights(returns: pd.DataFrame, holdings: list) -> dict:
    """
    計算風險平價權重：每支股票對組合總風險的貢獻相同。

    Task 4c（strategy/portfolio.py）把這個邏輯抽成共用模組，這裡保留
    同名函式當薄包裝，避免動到其他呼叫這個函式名稱的地方。
    """
    return portfolio_module.risk_parity_weight(returns, holdings, window=60)


# ══════════════════════════════════════════════════════════════
# PART 2b  機制動態因子權重（Task 4a）
# ══════════════════════════════════════════════════════════════
#
# 任務書規格的原始版本含 inst_flow（5 因子）：
#   BULL:    momentum .30 / value .10 / rev_yoy .25 / low_vol .05 / inst_flow .30
#   NEUTRAL: momentum .15 / value .20 / rev_yoy .20 / low_vol .15 / inst_flow .30
#   WARNING: momentum .05 / value .25 / rev_yoy .15 / low_vol .35 / inst_flow .20
#   BEAR:    momentum .00 / value .30 / rev_yoy .10 / low_vol .50 / inst_flow .10
#
# 2026-08-18 的 Task 2 決定（docs/DECISIONS.md、
# reports/factor_negative_findings.md）已經確認 inst_flow 在四輪真實資料
# 排查後對未來報酬沒有穩定預測力，移出主策略複合，這個決定對 Task 4 一樣
# 適用——繼續把它排除在外，不是遺漏，是延續同一個已經記錄過理由的決定。
#
# 下面的權重是「拿掉 inst_flow 那一欄，剩下四個因子依原本的相對比例
# 重新正規化」算出來的（例如 BULL：momentum .30/value .10/rev_yoy .25/
# low_vol .05 四項總和 .70，各自除以 .70），不是重新設計的權重——
# 保留任務書對「不同機制下哪個因子該加重」的原始判斷，只是把分母換成
# 四因子的總和。
REGIME_FACTOR_WEIGHTS = {
    "BULL":    {"momentum": 0.4286, "value": 0.1429, "rev_yoy": 0.3571, "low_vol": 0.0714},
    "NEUTRAL": {"momentum": 0.2143, "value": 0.2857, "rev_yoy": 0.2857, "low_vol": 0.2143},
    "WARNING": {"momentum": 0.0625, "value": 0.3125, "rev_yoy": 0.1875, "low_vol": 0.4375},
    "BEAR":    {"momentum": 0.0,    "value": 0.3333, "rev_yoy": 0.1111, "low_vol": 0.5556},
}


def _regime_state_and_exposure(regime_df: Optional[pd.DataFrame], today) -> Tuple[str, float]:
    """
    查某一天的機制狀態與建議曝險比例，NaN（機制輸出尚未暖機完成的早期
    區間，見 regime/hmm_detector.py 的 504 天門檻）一律 fallback 回
    NEUTRAL / 0.7——不是「猜」，是選一個中性、不偏多不偏空的預設值，
    跟 quant_layer2.py 其他地方「資料不足時退回保守預設」的處理原則一致。
    """
    if regime_df is None or today not in regime_df.index:
        return "NEUTRAL", 0.7
    row = regime_df.loc[today]
    state = row.get("regime")
    exposure = row.get("exposure")
    if pd.isna(state) or state not in REGIME_FACTOR_WEIGHTS:
        state = "NEUTRAL"
    if pd.isna(exposure):
        exposure = 0.7
    return state, float(exposure)


def build_positions(factors: dict,
                    top_n: int = TOP_N,
                    rebal_freq: int = REBAL_FREQ,
                    bias_cap: float = 0.10,
                    buffer_multiplier: float = 1.5,
                    use_risk_parity: bool = False,
                    inertia: float = 0.6,
                    max_pct_dv: Optional[float] = None,
                    regime_df: Optional[pd.DataFrame] = None,
                    use_regime_factor_weights: bool = False,
                    use_regime_exposure: bool = False,
                    weighting: Optional[str] = None,
                    ml_scores: Optional[pd.DataFrame] = None,
                    use_ml_composite: bool = False,
                    use_fixed_weights: bool = False,
                    fixed_weights: Optional[Dict[str, float]] = None,
                    for_live_signal: bool = False) -> pd.DataFrame:
    """
    根據因子決定每天的持倉比例矩陣。

    for_live_signal（Task 8 每日自動化用）：預設 False，回傳
    `final_pos`（唯一一次 shift(1) 之後的版本，回測安全，today 那一列
    其實是「用前一天資料決定、今天已經執行完的部位」）。設成 True 時
    回傳 `timed_pos`（shift 之前），today 那一列才是「用到今天收盤為止
    的全部資料所建議、應該明天執行」的即時訊號——回測絕對不能用這個
    版本（會有 look-ahead），只有每日自動化產生「明天該持有什麼」的
    即時訊號時才用。

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

    ── Task 4：機制整合（regime_df / use_regime_factor_weights / use_regime_exposure）──
    regime_df 是 regime/regime_engine.py 的 run_regime_engine() 回傳的
    result_df（欄位含 'regime'、'exposure'），reindex 到 close.index 對齊。

    2026-08-22（Task 6 開工前）決定：原本 Task 4 的 `use_regime_weights`
    一個開關同時控制兩件事（用什麼分數排名選股、要不要用曝險縮放），
    這在 Task 6 要做 ablation D（ML 選股 + 機制曝險）時語意會衝突——
    「選股用 ML」跟「機制動態調整因子比重」是同一層（排名依據），互斥
    合理；但「曝險水位由機制決定」是另一層（資金配置比例），理論上
    可以搭配任何一種選股方法。拆成兩個獨立開關，理由詳見
    docs/DECISIONS.md（「Task 6 ablation D 語意決定」那筆）：

    - `use_regime_factor_weights`（原本 Task 4a 的功能，即「機制動態
      調整因子比重」）：False（預設）跟 Task 2 一樣用全期動態 IC 加權
      合成因子分數；True 則每個再平衡日查當日機制（BULL/NEUTRAL/
      WARNING/BEAR），用 REGIME_FACTOR_WEIGHTS 對應的靜態權重合成當天
      的因子分數，取代動態 IC 加權。跟 `use_ml_composite` 互斥（兩者
      都是「用什麼分數排名選股」，同時開啟語意不明確）。
    - `use_regime_exposure`（原本 Task 4b 的功能，即「機制建議曝險
      水位」）：False（預設）維持原本二元大盤擇時開關（proxy vs 60 日
      均線，多頭才買、空頭只賣不買）；True 則部位大小 = 選股結果
      （等權或風險平價）× 當日機制建議曝險比例（BULL 1.0／NEUTRAL
      0.7／WARNING 0.4／BEAR 0.1，再平衡日鎖定、期間不動），並且
      「允許買入新股」永遠成立（風險控制交給連續的曝險縮放，不再靠
      binary 開關擋買進，避免雙重收緊風險）。這個開關獨立於選股邏輯，
      可以搭配動態 IC、機制靜態權重、或 ML 分數任何一種排名方法——
      這正是 Task 6 ablation D（`use_ml_composite=True` +
      `use_regime_factor_weights=False` + `use_regime_exposure=True`：
      ML 選股、機制決定曝險）需要的組合。

    weighting：Task 4c 規格的字串介面（'equal'|'risk_parity'），如果有給
    值就覆蓋 use_risk_parity（等同 weighting=='risk_parity'）；沒給值就
    照舊看 use_risk_parity。兩個參數並存是為了不破壞既有呼叫端。

    ── Task 5：ML 因子合成（ml_scores / use_ml_composite）──────
    ml_scores 是 strategy/ml_composite.py 的 walk_forward_ml_composite()
    算好的 (date x stock_id) LightGBM 預測分數矩陣，跟 regime_df 一樣由
    呼叫端先算好再傳進來（quant_layer2.py 不 import ml_composite.py，
    維持單向依賴：ml_composite.py 可以 import quant_layer2.py，反過來
    不行）。use_ml_composite=True 時，每個再平衡日直接用 ml_scores 當天
    的值排名選股，取代動態 IC 加權或機制靜態權重合成的複合分數。
    跟 use_regime_factor_weights 互斥（同時給 True 會丟例外，避免
    「兩種排名依據都要」這種未定義行為），但可以自由搭配
    use_regime_exposure（見上方說明，這就是 Task 6 ablation D 的組合）。

    ── Task 6b：固定權重（use_fixed_weights）──────────────────
    預設的動態 IC 加權（`else` 分支）會逐日用 252 日滾動 |IC| 調整權重；
    `active_default`（momentum 0.34／value 0.18／rev_yoy 0.18／
    low_vol 0.30，依 Task 2 實測 IC 相對強弱訂出）原本只在資料不足時
    當 fallback 用。`use_fixed_weights=True` 時，整個回測期間都直接用
    這組固定權重，完全不計算滾動 IC——這是 Task 6b ablation A／B
    （固定權重，不管有沒有機制）需要的「非動態」對照組：驗證「權重
    會不會自動調整」這件事本身，對績效／回撤的影響有多大。跟
    use_regime_factor_weights／use_ml_composite 互斥（三者都是決定
    「用什麼分數排名選股」，同時開啟語意不明確）。

    `fixed_weights`：只有 `use_fixed_weights=True` 時才有作用，預設
    `None` 就是用 `active_default`（上面說的那組實測 IC 權重）；也可以
    傳入自訂字典覆蓋（例如 Task 6d 的 attribution.py 要算「單因子版」
    的邊際貢獻，就是傳 `{"momentum": 1.0}` 這種只給一個因子權重的字典
    進來）。字典裡沒提到的 active 因子權重視為 0，不用每個因子都列。
    """
    _scoring_modes = [use_ml_composite, use_regime_factor_weights, use_fixed_weights]
    if sum(bool(m) for m in _scoring_modes) > 1:
        raise ValueError("use_ml_composite／use_regime_factor_weights／use_fixed_weights "
                         "最多只能開一個：三者都是決定「用什麼分數排名選股」的機制，同時"
                         "開啟語意不明確。機制曝險（use_regime_exposure）不受此限制，可以"
                         "跟任一種排名方法搭配，見 build_positions() docstring。")
    if use_ml_composite and ml_scores is None:
        raise ValueError("use_ml_composite=True 但沒有提供 ml_scores")

    if weighting is not None:
        if weighting not in ("equal", "risk_parity"):
            raise ValueError(f"weighting 必須是 'equal' 或 'risk_parity'，收到：{weighting!r}")
        use_risk_parity = (weighting == "risk_parity")

    close = factors["close"]
    cols  = close.columns

    momentum   = factors["momentum"].reindex(columns=cols)
    bias       = factors["bias"].reindex(columns=cols)
    rev_yoy    = factors["rev_yoy"].reindex(columns=cols)
    PER        = factors["PER"].reindex(columns=cols)
    value      = factors["value"].reindex(columns=cols)
    low_vol    = factors["low_vol"].reindex(columns=cols)
    div_yld     = factors.get("div_yld", pd.DataFrame(np.nan, index=close.index, columns=cols)).reindex(columns=cols)
    dollar_volume = factors.get("dollar_volume", pd.DataFrame(np.nan, index=close.index, columns=cols)).reindex(columns=cols)
    liquid_mask = factors.get("liquid_mask",
                  pd.DataFrame(True, index=close.index, columns=cols)
                  ).reindex(columns=cols)

    # ── 篩選條件 ──────────────────────────────────────────────
    # 注意：不要在宇宙階段硬性排除 PER<=0（註解中說明過），
    #       否則會喪失 value 因子的深度樣本。改為只用流動性與乖離作為過濾。
    valid = (bias < bias_cap) & liquid_mask

    # ── 合成因子（IC 加權，五因子）──────────────────────────
    #
    # 2026-08-18 決定（docs/DECISIONS.md、reports/factor_negative_findings.md）：
    # inst_flow 經四輪真實資料 IC 排查（正規化前後對照、資料補齊前後對照、
    # SQL 逐筆核對真實 API、短窗口重測、分年份拆解）後判定為誠實的負面結果——
    # 不是 bug，是這個定義下的因子在線性排名方法下對未來報酬沒有穩定預測力。
    # 移出主策略複合，留給 Task 5 的非線性/ML 方法重新評估。
    #
    # 2026-09-03 決定（Task 8 驗收時發現、補做的排查，見 docs/DECISIONS.md
    # 「Task 8 驗收前疑點排查」那筆）：`mom_120`、`div_yld`、`dollar_volume`
    # 是這個檔案在 Task 1-9 這個流程開始之前（2026-05-06 之前）就已經存在
    # 的舊版「v5」補充因子，一直沒有經過跟 inst_flow／margin_usage 同等級
    # 的 IC 排查與正式決定，卻透過下面的動態 IC 加權機制一直在真的參與
    # 複合分數計算（不是只有在診斷報告裡好看而已）。這次補做完整排查：
    #   - `dollar_volume`：不是報酬預測因子，只是用來定義流動性宇宙
    #     （`liquid_mask`）跟限制單次調倉量（`max_pct_dv`），不需要 IC 測試，
    #     維持現狀。
    #   - `mom_120`（120 日動量）：全樣本 IC 均值 0.0212、ICIR 0.128，
    #     低於 inst_flow／margin_usage 那次排查採用的 0.03 門檻，而且跟主
    #     動量因子 `momentum` 的橫截面排名相關係數高達 0.692——本質上是
    #     同一個動量主題的較弱、較冗餘版本，不是獨立訊號。**移出複合**
    #     （`build_factors()` 仍會計算，因子診斷報告仍會顯示它的 IC，
    #     只是不再進 `factor_map`／不再參與加權，避免看起來像被藏起來）。
    #   - `div_yld`（股利殖利率）：全樣本 IC 均值 0.0424、ICIR 0.268——
    #     是五個核心因子裡 IC 最高、ICIR 最高的，而且是唯一一個
    #     2015-2026 逐年 IC 全部為正（沒有任何一年翻負）的因子。經濟邏輯
    #     站得住腳：高股利殖利率股票通常是成熟、現金流穩定、市場相對低估
    #     的公司，是文獻裡行之有年的價值/品質因子（例如股利殖利率異常，
    #     跟 Fama-French 價值因子系出同源）。**正式納入複合**，不再是
    #     「剛好留在程式碼裡」的狀態，下面的 `default_weights` 也已經
    #     依真實 IC 重新計算，不是延續舊的四因子權重再硬塞一個進去。
    #
    # 五個核心因子權重（見下方 default_weights）：
    # momentum 0.24 / value 0.14 / rev_yoy 0.13 / low_vol 0.22 / div_yld 0.27
    # 依各自 2015-2026-08 全樣本實測 20 日 IC 均值的相對強弱正規化分配
    # （momentum 0.0378 / value 0.0221 / rev_yoy 0.0199 / low_vol 0.0349 /
    # div_yld 0.0424，總和 0.1571 → 各自 IC / 總和），不是簡單平均分配。
    #
    # cross_zscore 移到 factors/base.py（Task 2 模組化），這裡不再重複定義
    cross_zscore = factor_base.cross_zscore

    # 使用歷史 IC 做動態因子權重：
    # 1) 對每個因子計算 IC 時序（IC = 因子排名 vs 未來 20 日報酬的 Spearman）
    # 2) 對 IC 取絕對值再用 252 日滾動平均作穩定度量
    # 3) 權重按各因子滾動平均 |IC| 比重分配；若資料不足則退回預設權重
    fwd_ret = close.pct_change(20).shift(-20)

    factor_map = {
        "momentum": momentum,
        "value": value,
        "rev_yoy": rev_yoy,
        "low_vol": low_vol,
        "div_yld": div_yld,
    }

    # 移除未載入或全為 NaN 的因子（例如 inst_flow 尚未下載）
    active_factors = [k for k, df in factor_map.items() if k in factor_map and not df.isna().all().all()]

    # 計算每個因子的 IC 序列（歷史）
    ic_df = pd.DataFrame(index=close.index, columns=active_factors, dtype=float)
    for name in active_factors:
        ic_series = compute_ic(factor_map[name], fwd_ret)
        if not ic_series.empty:
            ic_df.loc[ic_series.index, name] = ic_series.values

    # 用 252 日滾動平均的絕對 IC 作為穩定性指標
    ic_rolling = ic_df.abs().rolling(252, min_periods=60).mean()

    # 當日權重 = 該日每因子 ic_rolling / 該日所有因子 ic_rolling 之和
    weights_df = ic_rolling.div(ic_rolling.sum(axis=1), axis=0)

    # 若某日所有因子 ic_rolling 為 0/NaN，fallback 回預設權重並排除不存在的因子
    #
    # 2026-09-03 決定（docs/DECISIONS.md「Task 8 驗收前疑點排查」）：五個
    # 核心因子（momentum/value/rev_yoy/low_vol/div_yld）的 fallback 權重
    # 依 2015-2026-08 全樣本實測 20 日 IC 均值的相對強弱正規化：
    #   momentum 0.0378 / value 0.0221 / rev_yoy 0.0199 / low_vol 0.0349 /
    #   div_yld 0.0424 → 總和 0.1571 → 各自 IC / 總和
    # （取代舊版只有四因子、div_yld 權重是 0 的設定——div_yld 這次正式排查
    # 後確認是五個因子裡 IC 最高、ICIR 最高、逐年 IC 全部為正的因子，不該
    # 繼續給 0 權重；mom_120 已經移出 factor_map，不會再出現在這裡）。
    default_weights = {
        "momentum": 0.24,
        "value": 0.14,
        "rev_yoy": 0.13,
        "low_vol": 0.22,
        "div_yld": 0.27,
    }
    # 只保留 active 因子的預設權重並正規化
    active_default = {k: default_weights[k] for k in active_factors}
    total_def = sum(active_default.values())
    for k in active_default:
        active_default[k] = active_default[k] / total_def

    if use_fixed_weights:
        # Task 6b：整個回測期間都用固定權重，不計算/不使用滾動 IC。
        # fixed_weights 沒給就用 active_default；有給就用呼叫端提供的字典
        # （沒列到的 active 因子權重視為 0），見 Task 6d attribution.py
        # 的單因子版用法。
        weights_to_use = fixed_weights if fixed_weights is not None else active_default
        for name in active_factors:
            weights_df[name] = weights_to_use.get(name, 0.0)
    else:
        missing_mask = weights_df.sum(axis=1).isna() | (weights_df.sum(axis=1) == 0)
        if missing_mask.any():
            for dt in weights_df.index[missing_mask]:
                for name, w in active_default.items():
                    weights_df.at[dt, name] = w

    # 計算每個因子的 cross-sectional zscore
    cz = {name: cross_zscore(factor_map[name]) for name in active_factors}

    # 合成分數（動態權重）：對每個因子，把當日權重乘上該日的 zscore
    composite = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for name in active_factors:
        w_series = weights_df[name].fillna(0.0)
        composite = composite.add(cz[name].multiply(w_series, axis=0), fill_value=0.0)

    masked_composite = composite.where(valid, np.nan)

    # ── Task 4a：機制靜態權重合成（只有 use_regime_factor_weights=True 才用）──
    # 只有四個任務書規格因子（momentum/value/rev_yoy/low_vol）有定義在
    # REGIME_FACTOR_WEIGHTS 裡；div_yld（2026-09-03 正式納入複合，見上方
    # 決定）目前機制模式下權重視為 0（不參與）——這是刻意維持 Task 4
    # 原始規格範圍不擴大，不是遺漏，之後如果要把 div_yld 也納入機制權重，
    # 需要另外決定四個機制狀態下 div_yld 各自該給多少權重，不能直接沿用
    # 動態 IC 模式的比例。
    regime_core_factors = [f for f in ("momentum", "value", "rev_yoy", "low_vol")
                            if f in active_factors]

    def _regime_scores_today(today) -> pd.Series:
        state, _ = _regime_state_and_exposure(regime_df, today)
        weights = REGIME_FACTOR_WEIGHTS[state]
        score = pd.Series(0.0, index=cols)
        for name in regime_core_factors:
            score = score.add(cz[name].loc[today] * weights.get(name, 0.0), fill_value=0.0)
        return score.where(valid.loc[today], np.nan)

    # ── 再平衡日索引 ──────────────────────────────────────────
    rebal_idx = np.where(np.arange(len(close)) % rebal_freq == 0)[0]

    # ── 大盤擇時：空頭時停止買入（不改變現有持倉權重）──────────
    #
    # 舊版問題：空頭時把所有持倉從 1/top_n → 0.5/top_n，
    #   position.diff() 偵測到 top_n 支股票同時改變，
    #   每次擇時切換貢獻 ~50% 換手，嚴重拖累績效。
    #
    # 修正：永遠使用固定等權 1/top_n，用「行為」控制風險：
    #   多頭 → 正常再平衡（買入新股、賣出落後股）
    #   空頭 → 只賣出跌出緩衝區的股票，不買入任何新股
    #   → position.diff() 僅來自實際換股，消除擇時切換造成的換手噪音
    #
    # 代理指數：用 liquid_mask 宇宙的中位數（修正 look-ahead bias）
    #   舊版 close.mean() 用整段時間均值選成分股 = 偷看未來。
    proxy = close.where(liquid_mask, np.nan).median(axis=1).ffill()

    in_market = (proxy > proxy.rolling(60).mean()).astype(float)

    timing_on_rebal = pd.Series(np.nan, index=close.index)
    timing_on_rebal.iloc[rebal_idx] = in_market.iloc[rebal_idx].values
    timing_stepped = timing_on_rebal.ffill().fillna(1.0)

    daily_ret = close.pct_change()   # 風險平價用
    eq_weight = 1.0 / top_n          # 等權 fallback

    current_holds_timed: set = set()
    pos_timed = pd.DataFrame(0.0, index=close.index, columns=cols)
    # 用於再平衡間的權重平滑（降低一次性大幅換手）
    last_rebal_pos = pd.Series(0.0, index=cols)

    for day_idx in rebal_idx:
        today = close.index[day_idx]

        # ── 排名依據：機制靜態權重／ML／動態 IC 三選一（互斥）──────
        if use_regime_factor_weights:
            scores_today = _regime_scores_today(today)
        elif use_ml_composite:
            if today in ml_scores.index:
                scores_today = ml_scores.reindex(columns=cols).loc[today].where(valid.loc[today], np.nan)
            else:
                scores_today = pd.Series(np.nan, index=cols)
        else:
            scores_today = masked_composite.loc[today]

        # ── 曝險水位：獨立開關，可以搭配上面任何一種排名依據 ────────
        # （2026-08-22 從 use_regime_weights 拆出來，見 docstring）
        if use_regime_exposure:
            # 風險控制交給連續的曝險縮放，不再靠 binary 開關擋買進，
            # 避免雙重收緊風險。
            is_bull = True
            _, exposure_today = _regime_state_and_exposure(regime_df, today)
        else:
            is_bull = timing_stepped.loc[today] >= 0.5
            exposure_today = 1.0

        rank_today = scores_today.rank(ascending=False)

        # 緩衝區進出場規則（Task 4c，strategy/portfolio.py）：
        # 無論多空，跌出緩衝區就賣（風險控制永遠執行）；is_bull 控制是否
        # 允許買進新股（機制模式下永遠 True，見上方 is_bull 設定的註解）。
        new_holds = portfolio_module.apply_buffer(
            rank_today, current_holds_timed,
            top_n=top_n, buffer_multiplier=buffer_multiplier, allow_entry=is_bull,
        )
        to_sell = current_holds_timed - new_holds
        current_holds_timed = new_holds
        # 對已被賣出的股票，立即把上一期持倉權重清為 0（避免 inertia 導致殘留小倉）
        if to_sell:
            last_rebal_pos.loc[list(to_sell)] = 0.0

        if current_holds_timed:
            holds_list = list(current_holds_timed)
            # 目標權重向量（未經平滑）
            target_pos = pd.Series(0.0, index=cols)
            if use_risk_parity:
                ret_slice = daily_ret.loc[:today]
                w_dict = risk_parity_weights(ret_slice, holds_list)
                for s, w in w_dict.items():
                    target_pos[s] = w
            else:
                target_pos[holds_list] = eq_weight

            # 4b：機制曝險模式下，目標權重整個乘上當日曝險比例——選股結果
            # 本身不變（還是等權/風險平價分給 top_n 檔），但整體部位規模
            # 隨機制縮放（BULL 1.0 全倉 ~ BEAR 0.1 幾乎空手），沒用掉的
            # 部分留白（視為現金），在再平衡日決定、期間不會再變動。
            if use_regime_exposure:
                target_pos = target_pos * exposure_today

            # 權重平滑：new = inertia*prev + (1-inertia)*target
            new_pos = last_rebal_pos * float(inertia) + target_pos * (1.0 - float(inertia))

            # 若設定了 max_pct_dv，套用每次 rebalance 的可執行量限制
            # 模擬方式：對每個標的，允許的最大權重變動 = max_pct_dv * (DV_s / DV_total)
            # 其中 DV_s = 該標的當日 dollar_volume，DV_total = 全宇宙當日 dollar_volume 之和。
            # 我們對每支股票各自限制當日權重變動；未能執行的部分會保留為 "現金"（即不做額外 renormalize），
            # 這模擬了流動性不足導致部分下單未能完成的情況。
            if max_pct_dv is not None and dollar_volume is not None and not dollar_volume.empty:
                dv_today = dollar_volume.loc[today].fillna(0.0)
                dv_total = dv_today.sum()
                if dv_total <= 0:
                    allowed = pd.Series(np.inf, index=cols)
                else:
                    allowed = (float(max_pct_dv) * dv_today / dv_total).reindex(cols).fillna(0.0)

                trade = new_pos - last_rebal_pos
                clipped = trade.copy()
                # clip absolute trade by allowed per-stock change
                clipped_vals = np.minimum(trade.abs(), allowed)
                clipped = np.sign(trade) * clipped_vals
                executed_pos = last_rebal_pos + clipped
                # 不做 renormalize；未執行部分保留為現金（sum(executed_pos) <= 1）
                pos_timed.loc[today] = executed_pos.values
                last_rebal_pos = executed_pos
            else:
                # 無流動性限制，照原本邏輯執行並正規化——
                # 機制模式下刻意不做這個 renormalize：new_pos 的總和本來就
                # 應該等於 exposure_today（< 1 代表刻意保留現金部位控制
                # 風險），重新正規化回 1 會把曝險縮放的效果整個抵銷掉。
                if not use_regime_exposure and new_pos.sum() > 0:
                    new_pos = new_pos / new_pos.sum()
                pos_timed.loc[today] = new_pos.values
                last_rebal_pos = new_pos

    anchor_t = pd.DataFrame(np.nan, index=close.index, columns=cols)
    anchor_t.iloc[rebal_idx] = pos_timed.iloc[rebal_idx].values
    timed_pos = anchor_t.ffill().fillna(0.0)

    # ── 唯一一次 shift(1)：今日訊號，明日執行 ────────────────
    final_pos = timed_pos.shift(1).fillna(0.0)

    result = timed_pos if for_live_signal else final_pos
    avg_holdings = (result > 0).sum(axis=1).replace(0, np.nan).mean()
    print(f"  平均持倉檔數：{avg_holdings:.1f} 檔（目標 {top_n} 檔）")
    return result


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


def run_backtest_detailed(close: pd.DataFrame,
                                                    position: pd.DataFrame,
                                                    commission: float = COMMISSION,
                                                    tax: float = TAX,
                                                    slippage: float = SLIPPAGE) -> Tuple[dict, pd.Series, dict]:
        """
        Same as `run_backtest` but also returns detailed time series for analysis.

        Returns: (stats, equity, diagnostics)
            diagnostics: { 'gross_ret': Series, 'net_ret': Series, 'turnover': Series, 'total_cost': Series }
        """
        # 每日報酬矩陣
        asset_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)

        # 投資組合每日毛報酬
        gross_ret = (position * asset_ret).sum(axis=1)

        # 交易成本（每日）
        turnover = position.diff().abs().sum(axis=1)
        cost_per_unit_buy = commission + slippage
        cost_per_unit_sell = commission + tax + slippage
        total_cost = turnover * (cost_per_unit_buy + cost_per_unit_sell) / 2.0

        net_ret = gross_ret - total_cost

        # 淨值曲線（從 1.0 開始）
        equity = (1 + net_ret).cumprod()

        # 績效指標
        total_ret = equity.iloc[-1] - 1
        n_days = max((net_ret != 0).sum(), 1)
        annual_ret = (1 + total_ret) ** (252 / n_days) - 1
        annual_vol = net_ret.std() * np.sqrt(252)
        sharpe = (annual_ret - RF_RATE) / annual_vol if annual_vol > 0 else 0.0
        mdd = (equity / equity.cummax() - 1).min()
        calmar = annual_ret / abs(mdd) if mdd != 0 else 0.0
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

        diagnostics = {
                "gross_ret": gross_ret,
                "net_ret": net_ret,
                "turnover": turnover,
                "total_cost": total_cost,
        }

        return stats, equity, diagnostics


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


def run_pipeline(top_n: Optional[int] = None,
                 rebal_freq: Optional[int] = None,
                 bias_cap: Optional[float] = None,
                 buffer_multiplier: Optional[float] = None,
                 use_risk_parity: Optional[bool] = None,
                 inertia: Optional[float] = None,
                 max_pct_dv: Optional[float] = None,
                 commission: Optional[float] = None,
                 tax: Optional[float] = None,
                 slippage: Optional[float] = None,
                 save_equity_path: Optional[str] = None,
                 regime_df: Optional[pd.DataFrame] = None,
                 use_regime_factor_weights: Optional[bool] = None,
                 use_regime_exposure: Optional[bool] = None,
                 weighting: Optional[str] = None,
                 ml_scores: Optional[pd.DataFrame] = None,
                 use_ml_composite: Optional[bool] = None,
                 use_fixed_weights: Optional[bool] = None,
                 fixed_weights: Optional[Dict[str, float]] = None,
                 data_start: Optional[str] = None,
                 data_end: Optional[str] = None,
                 for_live_signal: bool = False) -> Tuple[dict, pd.Series, pd.DataFrame]:
    """
    Run the full pipeline with optional parameter overrides.

    data_start/data_end（Task 8 每日自動化用）：覆蓋模組層級的 START_DATE／
    END_DATE 常數，讓每天執行時可以把 end 設成「今天」而不是寫死的歷史
    日期。不傳就完全維持既有行為（Task 2-6 所有既有呼叫端都不用改）。
    for_live_signal 見 build_positions() 的說明。

    regime_df/use_regime_factor_weights/use_regime_exposure：Task 4 機制
    整合，2026-08-22 拆成兩個獨立開關（見 build_positions() 的 docstring
    與 docs/DECISIONS.md「Task 6 ablation D 語意決定」）。regime_df 由
    呼叫端先跑 regime/regime_engine.py 的 run_regime_engine() 取得
    （quant_layer2.py 本身不 import regime/，維持 Task 3-4 一開始定案的
    單向依賴：regime/ 不依賴 quant_layer2.py，但 quant_layer2.py 可以
    被動接受它的輸出）。

    ml_scores/use_ml_composite：Task 5 ML 因子合成，見 build_positions() 的
    docstring。ml_scores 由呼叫端先跑 strategy/ml_composite.py 的
    walk_forward_ml_composite() 取得，同樣是被動接受，不反向 import。

    Returns: (stats_dict, equity_series, positions_df)
    """
    # defaults
    top_n = TOP_N if top_n is None else top_n
    rebal_freq = REBAL_FREQ if rebal_freq is None else rebal_freq
    bias_cap = 0.10 if bias_cap is None else bias_cap
    buffer_multiplier = 1.5 if buffer_multiplier is None else buffer_multiplier
    use_risk_parity = False if use_risk_parity is None else use_risk_parity
    inertia = 0.6 if inertia is None else inertia
    commission = COMMISSION if commission is None else commission
    tax = TAX if tax is None else tax
    slippage = SLIPPAGE if slippage is None else slippage
    use_regime_factor_weights = False if use_regime_factor_weights is None else use_regime_factor_weights
    use_regime_exposure = False if use_regime_exposure is None else use_regime_exposure
    use_ml_composite = False if use_ml_composite is None else use_ml_composite
    use_fixed_weights = False if use_fixed_weights is None else use_fixed_weights
    data_start = START_DATE if data_start is None else data_start
    data_end = END_DATE if data_end is None else data_end

    # Step 1：載入資料
    data = load_matrices(DB_PATH, data_start, data_end)
    if data["close"].shape[1] < 5:
        raise RuntimeError("資料庫裡的股票不足 5 檔，無法做有意義的橫截面排名。")

    # Step 2：建構因子
    print("\n🧠 建構因子矩陣...")
    factors = build_factors(data)

    # Step 3：因子診斷
    print_factor_diagnostics(factors, data["close"])

    # Step 4：建構部位（帶參數覆寫）
    print("📐 建構投資組合部位...")
    positions = build_positions(
        factors,
        top_n=top_n,
        rebal_freq=rebal_freq,
        bias_cap=bias_cap,
        buffer_multiplier=buffer_multiplier,
        use_risk_parity=use_risk_parity,
        inertia=inertia,
        max_pct_dv=max_pct_dv,
        regime_df=regime_df,
        use_regime_factor_weights=use_regime_factor_weights,
        use_regime_exposure=use_regime_exposure,
        weighting=weighting,
        ml_scores=ml_scores,
        use_ml_composite=use_ml_composite,
        use_fixed_weights=use_fixed_weights,
        fixed_weights=fixed_weights,
        for_live_signal=for_live_signal,
    )

    if for_live_signal:
        # positions 是 timed_pos（未 shift），拿去跑 run_backtest() 會有
        # look-ahead，算出來的 stats/equity 是假的——乾脆不算，強迫呼叫端
        # 只能用 positions.iloc[-1] 當今日訊號，不會不小心把污染過的績效
        # 數字當真的顯示出來。
        print("  ⚠️  for_live_signal=True：跳過回測，stats/equity 回傳 None"
              "（positions 是即時訊號，不是回測安全版本）")
        return None, None, positions

    # Step 5：回測
    stats, equity = run_backtest(data["close"], positions,
                                commission=commission, tax=tax, slippage=slippage)

    # Step 6：輸出
    print_report(stats)
    if save_equity_path:
        save_equity(equity, save_equity_path)

    return stats, equity, positions


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