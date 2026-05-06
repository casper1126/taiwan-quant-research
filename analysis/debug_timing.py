"""直接查看擇時系統在 2020 年的判斷過程。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from strategy.quant_layer3 import (
    DB_PATH, WARMUP_START, END_DATE,
    load_matrices, build_factors, market_timing_score,
)
import pandas as pd
import numpy as np

print("📂 載入資料...")
data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
factors = build_factors(data)
close = factors["close"]
liquid_mask = factors["liquid_mask"]

print("\n🧮 計算擇時 proxy 與分數...")
score = market_timing_score(close, liquid_mask)

# 重建 proxy 顯示
daily_ret = close.where(liquid_mask, np.nan).pct_change()
proxy_ret = daily_ret.mean(axis=1).fillna(0.0)
proxy = (1.0 + proxy_ret).cumprod()

print(f"\n📈 等權報酬累積指數（proxy）關鍵點：")
key_dates = ["2020-01-02", "2020-03-19", "2020-06-30",
             "2020-09-30", "2020-10-15", "2020-11-30", "2020-12-15"]
for d in key_dates:
    if pd.Timestamp(d) in proxy.index:
        p = proxy.loc[d]
        ma10 = proxy.loc[d] > proxy.rolling(10).mean().loc[d]
        ma30 = proxy.loc[d] > proxy.rolling(30).mean().loc[d]
        ma60 = proxy.loc[d] > proxy.rolling(60).mean().loc[d]
        ma120 = proxy.loc[d] > proxy.rolling(120).mean().loc[d]
        sc = score.loc[d]
        print(f"  {d}: proxy={p:.3f}  MA10={ma10}  MA30={ma30}  MA60={ma60}  MA120={ma120}  score={sc:.2f}")

# 顯示 2020 Q4 每個 rebal 日的實際分數
REBAL_FREQ = 21
rebal_idx = np.where(np.arange(len(close)) % REBAL_FREQ == 0)[0]
rebal_dates = close.index[rebal_idx]
q4_rebals = [d for d in rebal_dates if pd.Timestamp("2020-10-01") <= d <= pd.Timestamp("2020-12-31")]
print(f"\n🔁 2020 Q4 的 rebal 日（每 21 天一次）：")
for d in q4_rebals:
    sc = score.loc[d]
    if sc >= 0.75:
        exp = 1.0
    elif sc >= 0.50:
        exp = 0.7
    elif sc >= 0.25:
        exp = 0.5
    else:
        exp = 0.3
    print(f"  {d.date()}: score={sc:.2f} → 曝險 {exp*100:.0f}%")

# 顯示 score 分布
print(f"\n📊 2015 起 score 分布")
mask = score.index >= "2015-01-01"
s = score[mask]
print(f"  score=0.00（4 條 MA 全破）: {(s == 0.0).sum()} 天 ({(s == 0.0).mean()*100:.1f}%)")
print(f"  score=0.25:               {(s == 0.25).sum()} 天")
print(f"  score=0.50:               {(s == 0.50).sum()} 天")
print(f"  score=0.75:               {(s == 0.75).sum()} 天")
print(f"  score=1.00（4 條 MA 全站上）: {(s == 1.00).sum()} 天 ({(s == 1.0).mean()*100:.1f}%)")
