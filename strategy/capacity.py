"""
strategy/capacity.py
─────────────────────────────────────────────────────────────
Task 7c：策略容量分析（Capacity Analysis）

任何策略的歷史回測報酬，都隱含一個「這筆資金真的部署得下去」的假設。
一支流動性不足的股票，就算因子分數再高，實際下單時買賣本身就會推
動股價（market impact），小資金測不出來，資金一大就會侵蝕報酬。
這裡用業界常見的 **ADV 5% 規則**：任何一天對單一股票的下單量不超過
它當天 20 日平均成交金額（ADV）的 5%，反推「這個策略在目前的選股
結果下，最多能佈署多少資金」——受限的永遠是持股裡流動性最差的那一檔
（木桶理論的最短那塊板子），不是平均流動性。

用 Task 2 基準策略（動態 IC 加權、無機制、無 ML）的真實回測持倉計算，
不是假設的持倉——每個再平衡日的實際選股結果、實際 ADV，都是真實
資料庫查出來的數字。

直接執行：
  python strategy/capacity.py
"""

import sys
import warnings
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from loguru import logger

warnings.filterwarnings("ignore", category=FutureWarning)

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import quant_layer2 as q2

ADV_WINDOW = 20            # 用來算「當下流動性」的窗口，比 quant_layer2.py
                           # 篩選投資宇宙用的 252 日窗口短，capacity 分析
                           # 關心的是「現在」的流動性，不是長期平均
ADV_PCT_LIMIT = 0.05        # ADV 5% 規則


def detect_rebalance_dates(positions: pd.DataFrame, tol: float = 1e-9) -> List[pd.Timestamp]:
    """從實際部位矩陣反推真正發生換倉的日期（部位相對前一天有變動的日子）。
    比重新推算 rebal_freq 的內部索引更穩健——不管呼叫端用什麼參數跑出這組
    positions，偵測到的都是真正的成交日，不會因為假設跟實際參數不一致而算錯。"""
    diffs = positions.diff().abs().sum(axis=1)
    changed = diffs[diffs > tol]
    return list(changed.index)


def compute_adv(close: pd.DataFrame, volume: pd.DataFrame, window: int = ADV_WINDOW) -> pd.DataFrame:
    """window 日平均每日成交金額（NT$），ADV 5% 規則的基礎。"""
    dollar_volume = (close * volume).replace(0.0, np.nan)
    return dollar_volume.rolling(window, min_periods=max(5, window // 2)).mean()


def compute_capacity_at_rebalance(positions: pd.DataFrame, adv: pd.DataFrame,
                                  rebal_dates: List[pd.Timestamp],
                                  adv_pct_limit: float = ADV_PCT_LIMIT) -> pd.DataFrame:
    """
    對每個真正換倉的日期，算出：
      - 受限最嚴重的持股（binding stock）
      - 這檔持股在 ADV 5% 規則下，反推整個投資組合最多能佈署多少資金
        （NT$）：max_portfolio_capital = (adv_pct_limit × ADV_該股) / 該股權重
        （權重越大、越依賴這檔股票，它的流動性限制就越早卡住整體資金上限）
    """
    rows = []
    for dt in rebal_dates:
        if dt not in adv.index:
            continue
        weights = positions.loc[dt]
        held = weights[weights > 0]
        if held.empty:
            continue
        adv_today = adv.loc[dt, held.index]
        valid = adv_today.notna() & (adv_today > 0)
        if not valid.any():
            continue
        held_valid = held[valid]
        adv_valid = adv_today[valid]

        implied_capital = (adv_pct_limit * adv_valid) / held_valid
        binding_stock = implied_capital.idxmin()
        binding_capital = float(implied_capital.min())

        rows.append({
            "date": dt,
            "n_holdings": int(valid.sum()),
            "binding_stock": binding_stock,
            "binding_weight": float(held_valid[binding_stock]),
            "binding_adv": float(adv_valid[binding_stock]),
            "max_capital_ntd": binding_capital,
        })

    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame(
        columns=["n_holdings", "binding_stock", "binding_weight", "binding_adv", "max_capital_ntd"])


def run_capacity_analysis() -> Dict:
    logger.info("capacity：跑 Task 2 基準策略取得真實持倉...")
    stats, equity, positions = q2.run_pipeline(save_equity_path=None)

    logger.info("capacity：載入資料算 ADV...")
    data = q2.load_matrices(q2.DB_PATH, q2.START_DATE, q2.END_DATE)
    close = data["close"].replace(0.0, np.nan)
    volume = data["volume"].replace(0.0, np.nan)
    adv = compute_adv(close, volume)

    rebal_dates = detect_rebalance_dates(positions)
    logger.info(f"capacity：偵測到 {len(rebal_dates)} 個真正換倉日")

    capacity_df = compute_capacity_at_rebalance(positions, adv, rebal_dates)

    if capacity_df.empty:
        raise RuntimeError("capacity：沒有算出任何一筆有效的容量估計，請檢查 ADV/positions 是否對齊。")

    result = {
        "capacity_df": capacity_df,
        "median_capacity": float(capacity_df["max_capital_ntd"].median()),
        "min_capacity": float(capacity_df["max_capital_ntd"].min()),
        "max_capacity": float(capacity_df["max_capital_ntd"].max()),
        "latest_capacity": float(capacity_df["max_capital_ntd"].iloc[-1]),
        "latest_date": capacity_df.index[-1],
        "latest_binding_stock": capacity_df["binding_stock"].iloc[-1],
        "strategy_stats": stats,
    }
    logger.info(f"capacity：完成。最新一次再平衡（{result['latest_date'].date()}）容量估計 "
               f"NT${result['latest_capacity']:,.0f}，全期中位數 NT${result['median_capacity']:,.0f}")
    return result


def write_report(result: Dict, path: str = "reports/capacity_analysis.md") -> None:
    df = result["capacity_df"]
    lines = []
    lines.append("# 策略容量分析（Task 7c）\n")
    lines.append(f"用 ADV {ADV_PCT_LIMIT*100:.0f}% 規則（單日對單一股票的下單量不超過它 "
                 f"{ADV_WINDOW} 日平均成交金額的 {ADV_PCT_LIMIT*100:.0f}%）反推 Task 2 "
                 "基準策略在歷史每次真實換倉時，最多能佈署多少資金而不至於讓下單本身"
                 "造成過大的市場衝擊。受限的永遠是持股裡流動性最差的那一檔（binding "
                 "stock），不是平均流動性——這是容量分析的標準做法，資金規模上限"
                 "由最弱的一環決定。\n")

    lines.append("## 1. 全期容量估計摘要\n")
    lines.append(f"- 偵測到的真實換倉次數：{len(df)}")
    lines.append(f"- **最新一次換倉**（{result['latest_date'].date()}）容量估計：**NT$ "
                 f"{result['latest_capacity']:,.0f}**（binding stock：{result['latest_binding_stock']}）")
    lines.append(f"- 全期中位數：NT$ {result['median_capacity']:,.0f}")
    lines.append(f"- 全期最小值：NT$ {result['min_capacity']:,.0f}")
    lines.append(f"- 全期最大值：NT$ {result['max_capacity']:,.0f}\n")

    lines.append("## 2. 逐次換倉容量估計\n")
    show_df = df.copy()
    show_df["max_capital_ntd"] = show_df["max_capital_ntd"].apply(lambda x: f"{x:,.0f}")
    show_df["binding_adv"] = show_df["binding_adv"].apply(lambda x: f"{x:,.0f}")
    show_df["binding_weight"] = show_df["binding_weight"].apply(lambda x: f"{x*100:.2f}%")
    show_df.index = show_df.index.date
    try:
        lines.append(show_df.to_markdown() + "\n")
    except ImportError:
        lines.append(show_df.to_string() + "\n")

    lines.append("## 3. 白話解讀\n")
    lines.append(f"這個策略目前（TOP_N={q2.TOP_N} 檔、等權重）在 ADV 5% 規則下，最新一次"
                 f"換倉時能安全佈署的資金上限約是 **NT$ {result['latest_capacity']/1e8:.2f} 億元**"
                 f"（NT$ {result['latest_capacity']:,.0f}）。這是一個「不考慮衝擊成本會侵蝕"
                 "報酬」的上限估計，不是「超過這個數字絕對不能做」的硬性規定——實務上通常"
                 "會抓這個數字的一個折扣（例如 50-70%）當作實際操作的資金上限，保留緩衝。"
                 f"全期中位數（NT$ {result['median_capacity']/1e8:.2f} 億元）跟最新值的差距，"
                 "反映的是這段期間市場整體流動性的變化，不是策略本身的變化。\n")

    lines.append("**限制**：這個估計假設下單可以剛好卡在 ADV 的某個百分比、不考慮分散多天"
                 "執行降低衝擊的策略、也不考慮台股漲跌停限制對單日可執行量的額外限制——"
                 "這些都會讓實際可操作的資金規模比這裡的估計更寬鬆（分散執行）或更嚴格"
                 "（漲跌停鎖死時完全無法交易）。這裡的數字是一個工程上合理的量級估計，"
                 "不是精確到個位數可以直接拿去做資金配置決策的數字。")

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    logger.info(f"capacity：報告已存到 {path}")


if __name__ == "__main__":
    result = run_capacity_analysis()
    write_report(result)
    print("\n✅ Task 7c 策略容量分析完成。")
    print(f"   最新容量估計：NT$ {result['latest_capacity']:,.0f}"
         f"（binding stock: {result['latest_binding_stock']}）")
    print(f"   全期中位數：NT$ {result['median_capacity']:,.0f}")
