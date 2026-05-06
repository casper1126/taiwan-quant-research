"""
show_holdings.py — 把回測產生的 position 矩陣轉成「人看得懂」的持倉表

Layer 3 的 build_positions() 會回傳完整持倉矩陣，我們直接呼叫該流程，
取最後一個有持倉的日期，列出當天每檔股票權重。

用法：
  python analysis/show_holdings.py            # 顯示最新持倉 + 歷史平均
  python analysis/show_holdings.py 2026-04-17 # 顯示指定日期持倉
"""
import sys
from pathlib import Path

# 讓 strategy 和 data_pipeline 可以 import
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "strategy"))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE,
    load_matrices, build_factors, build_composite, build_positions,
)
import pandas as pd
import numpy as np


def show_one_day(positions: pd.DataFrame, day: pd.Timestamp) -> None:
    if day not in positions.index:
        # 找最近的交易日
        closest = positions.index[positions.index <= day][-1] if (positions.index <= day).any() else positions.index[-1]
        print(f"  （{day.date()} 非交易日，改顯示 {closest.date()}）")
        day = closest

    row = positions.loc[day]
    held = row[row > 0].sort_values(ascending=False)

    print(f"\n📅 {day.date()} 的資產配置")
    print(f"  總曝險：{held.sum() * 100:.1f}%（剩下是現金）")
    print(f"  持有檔數：{len(held)}")
    print(f"  ─────────────────────────────")
    print(f"  {'排名':<4} {'股票':<8} {'權重':<8} 視覺化")
    for i, (sid, w) in enumerate(held.items(), 1):
        bar = "█" * int(w * 200)  # 1% = 2 格
        print(f"  {i:<4} {sid:<8} {w * 100:5.2f}% {bar}")


def show_history(positions: pd.DataFrame) -> None:
    """歷史持倉統計：平均檔數、平均曝險、換手率。"""
    n_held = (positions > 0).sum(axis=1)
    exposure = positions.sum(axis=1)
    turnover = positions.diff().abs().sum(axis=1)

    print("\n📊 歷史統計（從 2015 起）")
    mask = positions.index >= "2015-01-01"
    print(f"  平均持有檔數：{n_held[mask].replace(0, np.nan).mean():.1f}")
    print(f"  平均曝險：{exposure[mask].mean() * 100:.1f}%")
    print(f"  曝險為 0% 的天數：{(exposure[mask] < 0.01).sum()} / {mask.sum()} 天 "
          f"({(exposure[mask] < 0.01).mean() * 100:.1f}%)")
    print(f"  曝險為 50% 的天數：{((exposure[mask] > 0.4) & (exposure[mask] < 0.6)).sum()} 天")
    print(f"  曝險為 100% 的天數：{(exposure[mask] > 0.9).sum()} 天")
    print(f"  日均換手率：{turnover[mask].mean() * 100:.2f}%（年化 {turnover[mask].mean() * 252 * 100:.0f}%）")


def main():
    print("📂 重建 Layer 3 持倉矩陣...")
    data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
    factors = build_factors(data)
    composite = build_composite(factors)
    positions = build_positions(factors, composite)

    target = pd.Timestamp(sys.argv[1]) if len(sys.argv) > 1 else positions.index[-1]
    show_one_day(positions, target)
    show_history(positions)


if __name__ == "__main__":
    main()
