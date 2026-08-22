"""
strategy/significance.py
─────────────────────────────────────────────────────────────
Task 6c：統計顯著性檢定

三個檢定，全部針對「Task 2 的基準策略」（動態 IC 加權、無機制、無 ML，
`quant_layer2.run_pipeline()` 預設參數）的每日淨報酬：

  deflated_sharpe_ratio()   Bailey & López de Prado (2014)
      「這個 Sharpe ratio 好不好」要考慮到：這個專案的開發過程中其實
      試過不只一種策略配置（Task 2 動態 IC／Task 4 機制版兩種加權法／
      Task 5 ML／Task 6 四個 ablation 版本……），試過的版本越多，
      單純運氣好挑到一個高 Sharpe 的機率也越高（multiple testing /
      selection bias）。DSR 把這個「試過幾次」的資訊也算進去，回答
      「扣掉純粹運氣的成分，這個 Sharpe 顯著大於 0 的機率是多少」。

  sharpe_confidence_interval()   Lo (2002)
      單一點估計的 Sharpe ratio 有多不確定？用 Lo (2002) 的漸進標準誤
      公式算 95% 信賴區間。

  bootstrap_pvalue()
      策略報酬 vs 台股大盤（TAIEX）buy-and-hold 報酬，逐日報酬差異的
      平均值，是不是顯著不等於 0？用 bootstrap 重抽樣（不假設任何
      分佈形狀）直接估計 p-value。

**誠實揭露**：如果任何一項檢定顯示「不顯著」，報告照實寫，不會為了
讓數字好看而調整檢定方法或門檻——這是本專案從 Task 2 開始就一貫的
研究誠信原則。

直接執行：
  python strategy/significance.py
"""

import sqlite3
import sys
import warnings
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
from loguru import logger
from scipy import stats as sp_stats

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import quant_layer2 as q2

# 這個專案開發過程中真正實作並回測過的策略變體數量（見下方 run_significance_analysis()
# 的完整清單），不是隨便湊的數字——DSR 對這個數字敏感，試過的版本越多，
# 「顯著」的門檻越高，誠實列出真正試過的版本數，不要為了讓 DSR 好看而低報。
N_TRIALS_DEFAULT = 8


# ══════════════════════════════════════════════════════════════
# 1. Deflated Sharpe Ratio（Bailey & López de Prado, 2014）
# ══════════════════════════════════════════════════════════════

def _sharpe_std_error_per_period(sharpe_per_period: float, n_days: int,
                                 skew: float, kurt: float) -> float:
    """
    Sharpe ratio 估計值的標準誤（Mertens 2002 / Lo 2002 公式，Bailey &
    López de Prado (2014) 的 DSR 推導也用同一個公式），**單位是「每期」
    （這裡每期 = 每個交易日）**，不是年化：

        σ(SR) ≈ sqrt( (1 − γ3·SR + (γ4−1)/4·SR²) / (T−1) )

    γ3 = 偏度（skewness），γ4 = 峰度（kurtosis，常態分佈時 = 3，
    這裡用非超額峰度，正常態時 (γ4−1)/4 = 0.5）。T = 樣本數（交易日數）。

    這個公式是從「單期」Sharpe 估計量的漸進變異數推導出來的，n_days 是
    「單期」的樣本數——如果直接把年化 Sharpe 代進去，會嚴重低估變異數
    （分母的 T−1 是以日為單位的樣本數，但年化 Sharpe 的尺度卻是日
    Sharpe 的 sqrt(252) 倍），算出來的 DSR 會失真地飽和在 1.0（曾經
    在這裡踩過這個 bug，見 docs/DECISIONS.md「Task 6c」那筆的記錄）。
    """
    numerator = 1 - skew * sharpe_per_period + ((kurt - 1) / 4.0) * sharpe_per_period ** 2
    numerator = max(numerator, 1e-10)  # 數值保護，避免負數開根號
    return float(np.sqrt(numerator / max(n_days - 1, 1)))


def deflated_sharpe_ratio(sharpe: float, n_trials: int, n_days: int,
                          skew: float, kurt: float,
                          periods_per_year: int = 252) -> Dict[str, float]:
    """
    Deflated Sharpe Ratio（Bailey & López de Prado, 2014）。

    `sharpe` 傳入**年化** Sharpe ratio（跟這個專案其他地方報的 Sharpe
    單位一致），內部先換算回「單期」（日）Sharpe 才代入變異數公式——
    DSR 論文的統計量推導是以「單期」為尺度，n_days 也是「單期」的樣本數，
    兩者單位一定要一致，否則變異數會嚴重失真（見上面
    `_sharpe_std_error_per_period()` 的說明）。

    回傳 dsr：P(真實 Sharpe > 0 | 觀測到的 Sharpe 是 n_trials 次試驗裡
    選出來的最佳值)，> 0.95 通常視為顯著。

    **簡化說明（誠實列出）**：完整版 DSR 需要知道 n_trials 次試驗各自
    的 Sharpe ratio 分佈的變異數，這裡沒有真的跑 n_trials 個完全獨立的
    策略再算它們 Sharpe 的變異數，而是用這個策略自己的 Sharpe 標準誤
    （Mertens/Lo 公式）當作跨試驗變異數的代理——這是業界常見的實務簡化
    （因為大多數情況下你不會真的重新訓練 N 個完整的獨立策略只為了算
    這一個統計量），但嚴格來說不是論文原始定義的精確版本。
    """
    sr_period = sharpe / np.sqrt(periods_per_year)
    sigma_sr_period = _sharpe_std_error_per_period(sr_period, n_days, skew, kurt)

    euler_gamma = 0.5772156649015329
    if n_trials < 2:
        sr0_period = 0.0
    else:
        z1 = sp_stats.norm.ppf(1 - 1.0 / n_trials)
        z2 = sp_stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
        sr0_period = sigma_sr_period * ((1 - euler_gamma) * z1 + euler_gamma * z2)

    if sigma_sr_period <= 0:
        dsr = 0.5
    else:
        z = (sr_period - sr0_period) / sigma_sr_period
        dsr = float(sp_stats.norm.cdf(z))

    return {
        "dsr": dsr,
        "sr0_expected_max_under_null": sr0_period * np.sqrt(periods_per_year),
        "sigma_sr": sigma_sr_period * np.sqrt(periods_per_year),
        "sharpe": sharpe,
        "n_trials": n_trials,
        "n_days": n_days,
    }


# ══════════════════════════════════════════════════════════════
# 2. Sharpe 信賴區間（Lo, 2002）
# ══════════════════════════════════════════════════════════════

def sharpe_confidence_interval(daily_returns: pd.Series, rf_daily: float = 0.0,
                               confidence: float = 0.95) -> Dict[str, float]:
    """
    Lo (2002) 簡化版（iid 假設）Sharpe ratio 標準誤：

        SE(SR) ≈ sqrt( (1 + SR²/2) / T )

    這裡 SR／SE 都先用日頻算，再乘 sqrt(252) 年化（Sharpe 年化用
    sqrt(252) 縮放，標準誤也用同樣比例縮放，年化後的信賴區間才跟
    quant_layer2.py 其他地方報的年化 Sharpe 是同一個單位）。
    """
    excess = daily_returns - rf_daily
    mu = excess.mean()
    sigma = excess.std()
    sr_daily = mu / sigma if sigma > 0 else 0.0
    T = len(daily_returns)

    se_daily = np.sqrt((1 + 0.5 * sr_daily ** 2) / T) if T > 1 else np.nan

    sr_annual = sr_daily * np.sqrt(252)
    se_annual = se_daily * np.sqrt(252)

    z = sp_stats.norm.ppf(0.5 + confidence / 2)
    lower = sr_annual - z * se_annual
    upper = sr_annual + z * se_annual

    return {
        "sharpe_annual": float(sr_annual),
        "se_annual": float(se_annual),
        "ci_lower": float(lower),
        "ci_upper": float(upper),
        "confidence": confidence,
        "n_days": T,
        "excludes_zero": bool(lower > 0 or upper < 0),
    }


# ══════════════════════════════════════════════════════════════
# 3. Bootstrap p-value（策略 vs TAIEX buy-and-hold）
# ══════════════════════════════════════════════════════════════

def bootstrap_pvalue(strategy_ret: pd.Series, benchmark_ret: pd.Series,
                     n: int = 10000, seed: int = 42) -> Dict[str, float]:
    """
    H0：策略逐日淨報酬與 TAIEX buy-and-hold 逐日報酬的平均差異 = 0。

    不假設任何分佈形狀，直接對「逐日報酬差異」這個序列做 n 次有放回
    重抽樣，得到重抽樣平均值的經驗分佈，用兩尾百分位數法算 p-value。

    **簡化說明**：這裡用 iid 重抽樣（每天獨立抽），沒有做 block
    bootstrap 保留報酬的序列相關性（例如波動群聚）——iid bootstrap
    對「平均值是否顯著不為 0」這種一階統計量通常還算穩健，但嚴格來說
    低估了自相關存在時真正的不確定性，是常見但不是最嚴謹的做法。
    """
    aligned = pd.DataFrame({"strat": strategy_ret, "bench": benchmark_ret}).dropna()
    diff = (aligned["strat"] - aligned["bench"]).values
    n_obs = len(diff)
    observed_mean = float(diff.mean())

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n_obs, size=(n, n_obs))
    boot_means = diff[idx].mean(axis=1)

    # 兩尾 percentile bootstrap 檢定：H0 下差異均值應該是 0，
    # p-value = 2 * min(P(boot_mean <= 0), P(boot_mean >= 0))
    p_low = float((boot_means <= 0).mean())
    p_high = float((boot_means >= 0).mean())
    p_value = float(min(1.0, 2 * min(p_low, p_high)))

    ci_lower, ci_upper = np.percentile(boot_means, [2.5, 97.5])

    return {
        "observed_mean_diff_daily": observed_mean,
        "observed_mean_diff_annualized": observed_mean * 252,
        "p_value": p_value,
        "n_bootstrap": n,
        "n_obs": n_obs,
        "ci_95_lower": float(ci_lower),
        "ci_95_upper": float(ci_upper),
        "significant_at_5pct": p_value < 0.05,
    }


# ══════════════════════════════════════════════════════════════
# 資料載入
# ══════════════════════════════════════════════════════════════

def _load_taiex_daily_return(db_path: str, start: str, end: str) -> pd.Series:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        "SELECT date, close FROM market_index WHERE index_id = 'TAIEX' "
        "AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    conn.close()
    if df.empty:
        return pd.Series(dtype=float)
    close = df.set_index("date")["close"]
    return close.pct_change().fillna(0.0)


# ══════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════

def run_significance_analysis(n_trials: int = N_TRIALS_DEFAULT,
                              path: str = "reports/significance_report.md") -> Dict:
    logger.info("significance：跑 Task 2 基準策略（動態 IC 加權，無機制、無 ML）...")
    stats, equity, positions = q2.run_pipeline(save_equity_path=None)
    daily_ret = equity.pct_change().fillna(0.0)

    n_days = int((daily_ret != 0).sum())
    total_ret = equity.iloc[-1] / equity.iloc[0] - 1
    ann_ret = (1 + total_ret) ** (252 / max(n_days, 1)) - 1
    ann_vol = daily_ret.std() * np.sqrt(252)
    sharpe = (ann_ret - q2.RF_RATE) / ann_vol if ann_vol > 0 else 0.0
    skew = float(daily_ret.skew())
    kurt = float(daily_ret.kurt() + 3)  # pandas .kurt() 回傳超額峰度，公式要非超額峰度

    dsr_result = deflated_sharpe_ratio(sharpe, n_trials, n_days, skew, kurt)
    ci_result = sharpe_confidence_interval(daily_ret)

    logger.info("significance：載入 TAIEX buy-and-hold 逐日報酬...")
    taiex_ret = _load_taiex_daily_return(q2.DB_PATH, q2.START_DATE, q2.END_DATE)

    logger.info("significance：跑 bootstrap p-value（10000 次重抽樣）...")
    boot_result = bootstrap_pvalue(daily_ret, taiex_ret, n=10000)

    _write_report(sharpe, ann_ret, n_days, skew, kurt, dsr_result, ci_result, boot_result, path)

    return {
        "sharpe": sharpe, "annual_return": ann_ret, "n_days": n_days,
        "dsr": dsr_result, "ci": ci_result, "bootstrap": boot_result,
    }


def _write_report(sharpe, ann_ret, n_days, skew, kurt, dsr, ci, boot, path) -> None:
    lines = []
    lines.append("# 統計顯著性檢定（Task 6c）\n")
    lines.append("針對 Task 2 基準策略（動態 IC 加權、無機制、無 ML，`quant_layer2.run_pipeline()` "
                 "預設參數）的每日淨報酬，做三項統計檢定。**任何一項顯示「不顯著」都照實寫，"
                 "不調整檢定方法或門檻去湊出「顯著」的結論。**\n")
    lines.append(f"基準策略：年化報酬 {ann_ret*100:+.2f}%，Sharpe {sharpe:.3f}，"
                 f"樣本 {n_days} 個有效交易日，日報酬偏度 {skew:.3f}、峰度 {kurt:.3f}。\n")

    lines.append("## 1. Deflated Sharpe Ratio（Bailey & López de Prado, 2014）\n")
    lines.append(f"n_trials = {dsr['n_trials']}——這個專案開發過程中真正實作並回測過的策略"
                 "變體數：(1) Task 2 動態 IC 加權基準版 (2) Task 4 機制版-等權 (3) Task 4 "
                 "機制版-風險平價 (4) Task 5 ML 因子合成版 (5) Task 6 Ablation A 固定權重"
                 "無機制 (6) Ablation B 固定權重+機制曝險 (7) Ablation C 動態權重+機制曝險 "
                 "(8) Ablation D ML+機制曝險——不是隨便湊的數字，是真的做過的 8 個版本"
                 "（見 git 歷史／`reports/`底下對應的產出）。DSR 對這個數字敏感：試過的"
                 "版本越多，「顯著」的門檻越高，這裡沒有為了讓 DSR 好看而低報。\n")
    lines.append(f"- 期望的隨機最佳 Sharpe（SR0，null 假設下 {dsr['n_trials']} 次試驗中"
                 f"最好的那個純靠運氣能達到多高）：{dsr['sr0_expected_max_under_null']:.4f}")
    lines.append(f"- Sharpe 估計標準誤 σ(SR)：{dsr['sigma_sr']:.4f}")
    lines.append(f"- **DSR（P(真實 Sharpe > 0)）：{dsr['dsr']:.4f}**")
    verdict_dsr = "✅ 顯著（DSR > 0.95）" if dsr["dsr"] > 0.95 else "❌ 不顯著（DSR ≤ 0.95）"
    lines.append(f"- 結論：{verdict_dsr}\n")

    lines.append("## 2. Sharpe 95% 信賴區間（Lo, 2002）\n")
    lines.append(f"- 年化 Sharpe 點估計：{ci['sharpe_annual']:.3f}")
    lines.append(f"- 年化標準誤：{ci['se_annual']:.3f}")
    lines.append(f"- 95% 信賴區間：[{ci['ci_lower']:.3f}, {ci['ci_upper']:.3f}]")
    verdict_ci = ("✅ 信賴區間不含 0（顯著）" if ci["excludes_zero"]
                  else "❌ 信賴區間包含 0（不顯著——不能排除真實 Sharpe 其實是 0 或負的可能性）")
    lines.append(f"- 結論：{verdict_ci}\n")

    lines.append("## 3. Bootstrap p-value：策略 vs TAIEX Buy-and-Hold\n")
    lines.append(f"- 逐日報酬差異均值（策略 − TAIEX）：{boot['observed_mean_diff_daily']*100:.4f}%/日"
                 f"（年化約 {boot['observed_mean_diff_annualized']*100:+.2f}%）")
    lines.append(f"- 樣本數：{boot['n_obs']} 個交易日，Bootstrap 重抽樣次數：{boot['n_bootstrap']}")
    lines.append(f"- **p-value：{boot['p_value']:.4f}**")
    lines.append(f"- 95% bootstrap 信賴區間（重抽樣均值分佈）：[{boot['ci_95_lower']*100:.4f}%, "
                 f"{boot['ci_95_upper']*100:.4f}%]")
    verdict_boot = "✅ 顯著（p < 0.05）" if boot["significant_at_5pct"] else "❌ 不顯著（p ≥ 0.05）"
    lines.append(f"- 結論：{verdict_boot}\n")

    lines.append("## 白話總結\n")
    n_significant = sum([dsr["dsr"] > 0.95, ci["excludes_zero"], boot["significant_at_5pct"]])
    lines.append(f"三項檢定中有 {n_significant}/3 項顯示統計上顯著。")
    if n_significant < 3:
        lines.append("這不代表策略沒有價值——樣本期間（2015-2026，約 11 年）對於嚴謹的統計"
                     "顯著性檢定而言不算長，尤其 DSR 在考慮了「試過 8 個版本」的多重比較"
                     "調整後，門檻本來就比單一策略的顯著性檢定更嚴格。誠實的結論是：目前的"
                     "樣本還不足以排除「這個 Sharpe 是運氣」的可能性，需要更長的樣本期間、"
                     "或更多獨立的樣本外驗證（例如 Task 6a 的 walk-forward 結果）才能提高"
                     "信心。")
    lines.append("")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"significance：報告已存到 {path}")


if __name__ == "__main__":
    result = run_significance_analysis()
    print("\n✅ Task 6c 統計顯著性檢定完成。")
    print(f"   DSR = {result['dsr']['dsr']:.4f}")
    print(f"   Sharpe 95% CI = [{result['ci']['ci_lower']:.3f}, {result['ci']['ci_upper']:.3f}]")
    print(f"   Bootstrap p-value = {result['bootstrap']['p_value']:.4f}")
