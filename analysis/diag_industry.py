"""快速診斷產業分散是否生效 + 各層曝險日數。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE, _industry_of,
    load_matrices, build_factors, build_composite, build_positions,
)
import pandas as pd
import numpy as np

print("📂 重建持倉...")
data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
factors = build_factors(data)
composite = build_composite(factors)
positions = build_positions(factors, composite)

# 產業分布
last = positions.iloc[-1]
held = last[last > 0]
ind_map = pd.Series([_industry_of(s) for s in held.index], index=held.index)
print(f"\n📊 最新產業分布（{positions.index[-1].date()}）")
ind_w = held.groupby(ind_map).sum().sort_values(ascending=False)
ind_n = ind_map.value_counts()
for ind in ind_w.index:
    bar = "█" * int(ind_w[ind] * 100)
    print(f"  {ind:<10} {ind_n[ind]:>2} 檔  {ind_w[ind]*100:5.2f}%  {bar}")

# 曝險分布
mask = positions.index >= "2015-01-01"
exposure = positions.sum(axis=1)
print(f"\n📅 2015 起曝險分布")
print(f"  ~30% 底倉:    {((exposure[mask] >= 0.25) & (exposure[mask] < 0.40)).sum()} 天")
print(f"  ~50% 震盪:    {((exposure[mask] >= 0.40) & (exposure[mask] < 0.60)).sum()} 天")
print(f"  ~70% 偏多:    {((exposure[mask] >= 0.60) & (exposure[mask] < 0.85)).sum()} 天")
print(f"  ~100% 滿倉:   {(exposure[mask] >= 0.85).sum()} 天")
print(f"  其他:         {((exposure[mask] < 0.25) | ((exposure[mask] >= 0.85) == False)).sum() - ((exposure[mask] >= 0.25) & (exposure[mask] < 0.85)).sum()} 天")
print(f"  平均曝險:     {exposure[mask].mean()*100:.1f}%")

# 2020 年特別檢查
y2020 = positions.loc["2020-01-01":"2020-12-31"]
exp_2020 = y2020.sum(axis=1)
print(f"\n🦠 2020 年曝險變化（COVID 年）")
print(f"  Q1 (1-3月):   平均 {exp_2020.loc['2020-01-01':'2020-03-31'].mean()*100:.0f}%")
print(f"  Q2 (4-6月):   平均 {exp_2020.loc['2020-04-01':'2020-06-30'].mean()*100:.0f}%")
print(f"  Q3 (7-9月):   平均 {exp_2020.loc['2020-07-01':'2020-09-30'].mean()*100:.0f}%")
print(f"  Q4 (10-12月): 平均 {exp_2020.loc['2020-10-01':'2020-12-31'].mean()*100:.0f}%")
