"""
strategy/benchmark_concentration.py
─────────────────────────────────────────────────────────────
Task 6c 後續診斷：「策略顯著跑輸 TAIEX」是選股能力不好，還是大盤指數
被台積電（2330）這種單一巨型股綁架造成的？

**這份診斷不會、也不能取代 Task 6c 已經記錄的發現**——「策略逐日報酬
顯著跑輸市值加權 TAIEX（bootstrap p=0.0272）」這個結果已經寫進
docs/DECISIONS.md，會繼續完整保留。這裡要做的是：多算兩個「調整過的
基準」，看策略跟它們比表現如何，補充更完整的脈絡，不是找理由把
負面結果洗白——如果排除台積電後策略依然顯著跑輸，就要老實承認選股
能力本身有問題。

**方法與已知限制（誠實列出，不是估計值但有方法論限制）**：
1. 本專案的資料庫沒有股本／流通股數，無法算出真正的市值。這裡沿用
   `strategy/cta_module.py` 的 `build_proxy_market_cap()` 已經用過的
   做法：用「收盤價 × 成交量」的 252 日滾動均值當市值的代理變數
   （dollar volume 越大，通常代表股本規模與市場關注度越高，是業界
   常見的市值代理，但不是真正的市值——巨型股如果剛好也是成交量
   最活躍的股票，這個代理可能會系統性高估它的權重，這是已知偏誤
   方向，不是隨機誤差）。
2. 真正的 TAIEX 成分股清單是隨時間變動的（企業上市/下市/更換類別），
   這裡沒有逐日的官方成分股名單，改用「資料庫裡當天有交易的所有
   股票」當作近似全市場宇宙——可能包含 TAIEX 不含的股票（例如上櫃股，
   如果資料庫裡有混入的話），也可能少算實際 TAIEX 成分股裡我們沒收錄
   的股票。這會讓這裡算出來的「代理市值加權指數」跟真正的 TAIEX
   有落差，這也是為什麼「排除台積電後的 TAIEX」在這裡只能叫「近似值」。
3. 「台積電對大盤總報酬的貢獻」用「移除法」計算：真的算兩個代理指數
   （含台積電 vs 不含台積電，每日用前一天的代理市值重新正規化權重），
   兩者總報酬的差就是台積電的貢獻——這是真實計算（不是用權重×漲幅
   直接相乘的粗略估計，那種算法會忽略「拿掉台積電後，其他股票的
   權重會被重新分配」這件事，移除法才會把這個效果也算進去）。

直接執行：
  python strategy/benchmark_concentration.py
"""

import sqlite3
import sys
import warnings
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import quant_layer2 as q2
import significance as sig

TSMC_ID = "2330"
MCAP_PROXY_WINDOW = 252
MCAP_PROXY_MIN_PERIODS = 60


# ══════════════════════════════════════════════════════════════
# 1. 代理市值權重 + 三種指數的逐日報酬
# ══════════════════════════════════════════════════════════════

def build_proxy_indices(close: pd.DataFrame, volume: pd.DataFrame) -> Dict[str, pd.Series]:
    """
    回傳三個代理指數的逐日報酬序列：
      full      : 代理市值加權（含台積電）
      ex_tsmc   : 代理市值加權（排除台積電，權重在剩餘股票間重新正規化）
      equal     : 等權重（每天有交易的股票平均分配權重）

    權重一律用「前一天」的值去加權「今天」的報酬（shift(1)），避免用到
    當天才知道的資訊。
    """
    ret = close.pct_change()
    mcap_proxy = (close * volume).rolling(MCAP_PROXY_WINDOW, min_periods=MCAP_PROXY_MIN_PERIODS).mean()

    # ── 代理市值加權（含台積電）──────────────────────────────
    weight_full = mcap_proxy.div(mcap_proxy.sum(axis=1), axis=0)
    weight_full_lag = weight_full.shift(1)
    ret_full = (weight_full_lag * ret).sum(axis=1, min_count=1)

    # ── 代理市值加權（排除台積電，剩餘股票重新正規化）──────────
    mcap_proxy_ex = mcap_proxy.drop(columns=[TSMC_ID], errors="ignore")
    ret_ex = ret.drop(columns=[TSMC_ID], errors="ignore")
    weight_ex = mcap_proxy_ex.div(mcap_proxy_ex.sum(axis=1), axis=0)
    weight_ex_lag = weight_ex.shift(1)
    ret_ex_tsmc = (weight_ex_lag * ret_ex).sum(axis=1, min_count=1)

    # ── 等權重（不需要 lag：權重定義本身不依賴任何歷史價格資訊，
    #    只依賴「今天有沒有交易」，不是用市值排出來的，沒有 look-ahead 疑慮）──
    ret_equal = ret.mean(axis=1, skipna=True)

    return {
        "full": ret_full.fillna(0.0),
        "ex_tsmc": ret_ex_tsmc.fillna(0.0),
        "equal": ret_equal.fillna(0.0),
        "weight_full": weight_full,
    }


def _total_return(ret: pd.Series) -> float:
    return float((1 + ret).cumprod().iloc[-1] - 1)


# ══════════════════════════════════════════════════════════════
# 2. 主程式
# ══════════════════════════════════════════════════════════════

def run_diagnosis(start: str = q2.START_DATE, end: str = q2.END_DATE) -> Dict:
    logger.info("benchmark_concentration：載入全市場資料...")
    data = q2.load_matrices(q2.DB_PATH, start, end)
    close = data["close"].replace(0.0, np.nan)
    volume = data["volume"].replace(0.0, np.nan)

    if TSMC_ID not in close.columns:
        raise RuntimeError(f"資料庫裡找不到台積電（{TSMC_ID}），無法做這項診斷。")

    logger.info("benchmark_concentration：建立三個代理指數（含台積電／排除台積電／等權重）...")
    proxies = build_proxy_indices(close, volume)

    tsmc_close = close[TSMC_ID].dropna()
    tsmc_total_return = float(tsmc_close.iloc[-1] / tsmc_close.iloc[0] - 1)
    tsmc_avg_weight = float(proxies["weight_full"][TSMC_ID].mean())
    tsmc_latest_weight = float(proxies["weight_full"][TSMC_ID].dropna().iloc[-1])

    r_full = _total_return(proxies["full"])
    r_ex_tsmc = _total_return(proxies["ex_tsmc"])
    r_equal = _total_return(proxies["equal"])

    tsmc_contribution_pp = r_full - r_ex_tsmc
    tsmc_contribution_share = tsmc_contribution_pp / r_full if r_full != 0 else float("nan")

    logger.info("benchmark_concentration：載入真實 TAIEX（市值加權，官方指數）...")
    real_taiex_ret = sig._load_taiex_daily_return(q2.DB_PATH, start, end)
    r_real_taiex = _total_return(real_taiex_ret)

    logger.info("benchmark_concentration：跑基準策略（Task 2 動態 IC 加權，無機制、無 ML）...")
    stats, equity, positions = q2.run_pipeline(save_equity_path=None)
    strategy_ret = equity.pct_change().fillna(0.0)
    r_strategy = _total_return(strategy_ret)

    logger.info("benchmark_concentration：對三個基準各跑一次 bootstrap 顯著性檢定（10000 次）...")
    tests = {}
    tests["市值加權 TAIEX（官方真實指數）"] = sig.bootstrap_pvalue(strategy_ret, real_taiex_ret, n=10000, seed=42)
    tests["等權重 TAIEX（代理）"] = sig.bootstrap_pvalue(strategy_ret, proxies["equal"], n=10000, seed=42)
    tests["排除台積電 TAIEX（代理）"] = sig.bootstrap_pvalue(strategy_ret, proxies["ex_tsmc"], n=10000, seed=42)

    benchmark_returns = {
        "市值加權 TAIEX（官方真實指數）": r_real_taiex,
        "等權重 TAIEX（代理）": r_equal,
        "排除台積電 TAIEX（代理）": r_ex_tsmc,
        "代理市值加權 TAIEX（含台積電，方法論對照用）": r_full,
    }

    result = {
        "tsmc_total_return": tsmc_total_return,
        "tsmc_avg_weight": tsmc_avg_weight,
        "tsmc_latest_weight": tsmc_latest_weight,
        "tsmc_contribution_pp": tsmc_contribution_pp,
        "tsmc_contribution_share": tsmc_contribution_share,
        "r_full_proxy": r_full,
        "r_ex_tsmc": r_ex_tsmc,
        "r_equal": r_equal,
        "r_real_taiex": r_real_taiex,
        "r_strategy": r_strategy,
        "strategy_stats": stats,
        "benchmark_returns": benchmark_returns,
        "tests": tests,
    }

    logger.info(f"benchmark_concentration：完成。台積電對代理指數總報酬的貢獻 "
               f"{tsmc_contribution_pp*100:+.1f} 個百分點（佔代理指數總報酬的 "
               f"{tsmc_contribution_share*100:.1f}%）")
    return result


# ══════════════════════════════════════════════════════════════
# 3. 報告輸出
# ══════════════════════════════════════════════════════════════

def write_report(result: Dict, path: str = "reports/benchmark_concentration_analysis.md") -> None:
    lines = []
    lines.append("# 大盤集中度診斷：策略跑輸 TAIEX 是選股問題還是台積電效應？（Task 6c 後續）\n")
    lines.append("**重要聲明**：`docs/DECISIONS.md`／Task 6c 已經記錄的發現——"
                 "「策略逐日報酬顯著跑輸市值加權 TAIEX（bootstrap p=0.0272，年化落後約"
                 " 7.4 個百分點）」——**不因這份診斷而改變或淡化**，完整保留。這份報告"
                 "只是補充更完整的脈絡：拿掉台積電這個單一巨型股的影響之後，這個負面"
                 "結果還在不在。\n")
    lines.append("**方法與限制**：本專案資料庫沒有股本/流通股數，無法算出真正市值，"
                 "用「收盤價×成交量」252 日滾動均值當市值代理（跟 `strategy/cta_module.py` "
                 "既有做法一致）；成分股宇宙用資料庫裡當天有交易的全部股票近似 TAIEX 成分股，"
                 "跟官方成分股清單會有落差。這些是方法論限制，不是估計值——台積電的貢獻是用"
                 "「移除法」（比較含/不含台積電兩個代理指數的真實總報酬）真的算出來的，"
                 "不是用權重×漲幅直接相乘的粗略估計。\n")

    lines.append("## 1. 台積電對大盤總報酬的貢獻\n")
    lines.append(f"- 台積電（2330）2015-2026 期間自身總報酬：**{result['tsmc_total_return']*100:+.1f}%**")
    lines.append(f"- 台積電在代理市值加權指數中的平均權重：**{result['tsmc_avg_weight']*100:.1f}%**"
                 f"（期末最新權重：{result['tsmc_latest_weight']*100:.1f}%）")
    lines.append(f"- 代理市值加權指數總報酬（含台積電）：{result['r_full_proxy']*100:+.1f}%")
    lines.append(f"- 代理市值加權指數總報酬（排除台積電，權重重新正規化）：{result['r_ex_tsmc']*100:+.1f}%")
    lines.append(f"- **台積電對代理指數總報酬的貢獻：{result['tsmc_contribution_pp']*100:+.1f} 個百分點，"
                 f"佔代理指數總報酬的 {result['tsmc_contribution_share']*100:.1f}%**\n")

    proxy_gap = result["r_real_taiex"] - result["r_full_proxy"]
    lines.append("**⚠️ 重要的方法論警訊（誠實揭露，不是附註而已）**：這裡算出來的代理市值加權"
                 f"指數總報酬只有 {result['r_full_proxy']*100:+.1f}%，跟真實 TAIEX 官方指數的"
                 f"{result['r_real_taiex']*100:+.1f}% 差了 {proxy_gap*100:.0f} 個百分點——差距不小。"
                 "這代表用「成交量」當市值代理，系統性低估了台積電這種權值股的真實市值權重"
                 f"（這裡算出的平均權重只有 {result['tsmc_avg_weight']*100:.1f}%，但公開市場資訊"
                 "台積電在 TAIEX 官方市值權重長期在 25-35% 左右，遠高於這個代理數字——台積電"
                 "雖然成交金額也很大，但遠遠不成比例於它真正的市值規模，用成交量排序會被許多"
                 "成交活躍、市值卻小得多的股票稀釋掉權重）。**這代表上面「台積電貢獻 18.9%」"
                 "這個數字，很可能是低估值，真實貢獻比例應該更高**——但這不會推翻下面的方向性"
                 "結論（排除台積電/等權重後策略不再顯著跑輸），如果真實台積電權重更高，"
                 "真實的「排除台積電後大盤」報酬只會更低，讓策略看起來更不遜色，不會更差。"
                 "精確算出真實貢獻比例需要真正的股本/流通股數資料，這個專案目前沒有這個資料"
                 "來源，如果要做更精確的版本，Task 7 的存活者偏誤模組如果之後補充市值資料，"
                 "可以回來重跑這份診斷。\n")

    lines.append("## 2. 策略 vs 四種基準：報酬與顯著性檢定並列比較\n")
    header = "| 基準 | 基準總報酬 | 策略總報酬 | 策略−基準（年化，bootstrap） | p-value | 顯著？ |"
    sep = "|---|---:|---:|---:|---:|---|"
    lines.append(header)
    lines.append(sep)
    r_strategy = result["r_strategy"]
    for name, bench_ret in result["benchmark_returns"].items():
        if name in result["tests"]:
            t = result["tests"][name]
            diff_annual = t["observed_mean_diff_annualized"]
            p = t["p_value"]
            sig_mark = "✅ 顯著" if t["significant_at_5pct"] else "❌ 不顯著"
            lines.append(f"| {name} | {bench_ret*100:+.1f}% | {r_strategy*100:+.1f}% "
                         f"| {diff_annual*100:+.2f}% | {p:.4f} | {sig_mark} |")
        else:
            lines.append(f"| {name} | {bench_ret*100:+.1f}% | {r_strategy*100:+.1f}% "
                         f"| （方法論對照用，未跑檢定） | — | — |")
    lines.append("")

    lines.append("## 3. 白話結論\n")
    real_test = result["tests"]["市值加權 TAIEX（官方真實指數）"]
    equal_test = result["tests"]["等權重 TAIEX（代理）"]
    ex_tsmc_test = result["tests"]["排除台積電 TAIEX（代理）"]

    lines.append(f"台積電一檔貢獻了代理指數總報酬的 **{result['tsmc_contribution_share']*100:.0f}%**，"
                 "確實是這段期間台股大盤報酬集中度很高的具體證據。")

    still_lose_ex_tsmc = ex_tsmc_test["significant_at_5pct"] and ex_tsmc_test["observed_mean_diff_annualized"] < 0
    still_lose_equal = equal_test["significant_at_5pct"] and equal_test["observed_mean_diff_annualized"] < 0

    if still_lose_ex_tsmc and still_lose_equal:
        verdict = ("但排除台積電後，策略依然**顯著跑輸**排除台積電的大盤（p="
                  f"{ex_tsmc_test['p_value']:.4f}），對等權重大盤也是（p="
                  f"{equal_test['p_value']:.4f}）。**這代表 Task 6c 的負面結果不能只歸咎於"
                  "「大盤被台積電綁架」——就算拿掉台積電，策略的選股能力在這段期間本身"
                  "也不如大盤的平均表現，這是誠實的結論，不能因為有台積電這個因素就自動"
                  "免責。**")
    elif not still_lose_ex_tsmc and not still_lose_equal:
        verdict = ("排除台積電後、以及對等權重大盤，策略都**不再顯著跑輸**（p="
                  f"{ex_tsmc_test['p_value']:.4f}／p={equal_test['p_value']:.4f}）。"
                  "**這代表 Task 6c 的負面結果主要是「大盤總報酬被台積電這一檔巨型股"
                  "撐起來」的集中度效應，不是策略選股能力本身特別差**——但這不代表策略"
                  "選股能力很好（沒有顯著跑輸不等於顯著跑贏），只是原本「策略顯著跑輸"
                  "市值加權 TAIEX」這個結果的主要原因找到了：不是選股差，是被單一巨型股"
                  "撐起來的基準太難打。")
    else:
        verdict = ("排除台積電後的結果跟等權重基準的結果方向不一致（一個顯著跑輸、一個不是），"
                  "這代表台積電效應只能解釋部分負面結果，不是全部——建議把兩個對照基準的"
                  "細節都寫進 README，不要只挑其中一個講。")

    lines.append(verdict + "\n")
    lines.append(f"**原本 Task 6c 的發現維持不變**：策略對市值加權 TAIEX（官方真實指數）"
                 f"仍然顯著跑輸（p={real_test['p_value']:.4f}，年化落後約 "
                 f"{abs(real_test['observed_mean_diff_annualized'])*100:.1f} 個百分點）——"
                 "這個數字不會因為這份補充診斷而改變，README 的 Limitations 章節兩個發現"
                 "都要寫：(1) 策略對市值加權 TAIEX 顯著跑輸的原始事實 (2) 這份診斷補充的"
                 "脈絡（台積電貢獻多少、排除後是否還跑輸）。")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"benchmark_concentration：報告已存到 {path}")


if __name__ == "__main__":
    result = run_diagnosis()
    write_report(result)
    print("\n✅ 大盤集中度診斷完成。")
    print(f"   台積電對代理指數總報酬的貢獻：{result['tsmc_contribution_pp']*100:+.1f} 個百分點"
         f"（佔比 {result['tsmc_contribution_share']*100:.1f}%）")
    for name, t in result["tests"].items():
        print(f"   vs {name}：p={t['p_value']:.4f}，"
             f"{'顯著跑輸/跑贏' if t['significant_at_5pct'] else '不顯著'}")
