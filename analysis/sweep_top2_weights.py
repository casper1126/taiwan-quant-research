"""快速 sweep 前 2 因子的權重，找最佳配置。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings("ignore")

from strategy import quant_layer3 as L3

CONFIGS = [
    # (mom, inst, value, rev)
    {"mom_52w": 0.50, "inst_flow": 0.35, "value": 0.10, "rev_mom": 0.05},  # baseline
    {"mom_52w": 0.55, "inst_flow": 0.40, "value": 0.05, "rev_mom": 0.00},
    {"mom_52w": 0.60, "inst_flow": 0.40, "value": 0.00, "rev_mom": 0.00},
    {"mom_52w": 0.55, "inst_flow": 0.45, "value": 0.00, "rev_mom": 0.00},
    {"mom_52w": 0.50, "inst_flow": 0.50, "value": 0.00, "rev_mom": 0.00},
    {"mom_52w": 0.65, "inst_flow": 0.35, "value": 0.00, "rev_mom": 0.00},
    {"mom_52w": 0.70, "inst_flow": 0.30, "value": 0.00, "rev_mom": 0.00},
]

# 載入一次資料（共用）
print("📂 載入資料...")
data = L3.load_matrices(L3.DB_PATH, L3.WARMUP_START, L3.END_DATE)
factors = L3.build_factors(data)
close = factors["close"]

print("\n🔍 Weight Sweep：")
print("─" * 95)
print(f"  {'mom':<5} {'inst':<5} {'val':<5} {'rev':<5}  {'CAGR':<8} {'Sharpe':<8} "
      f"{'MDD':<8} {'Calmar':<8}")
print("─" * 95)

results = []
for cfg in CONFIGS:
    composite = L3.build_composite(factors, weights=cfg)
    positions = L3.build_positions(factors, composite)
    asset_ret = close.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    gross = (positions * asset_ret).sum(axis=1)
    turnover = positions.diff().abs().sum(axis=1)
    cost = turnover * (L3.COMMISSION + L3.SLIPPAGE + L3.COMMISSION + L3.TAX + L3.SLIPPAGE) / 2.0
    net = gross - cost
    mask = net.index >= pd.Timestamp(L3.REPORT_START)
    net_r = net.loc[mask]
    eq = (1 + net_r).cumprod()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1/yrs) - 1
    vol  = net_r.std() * np.sqrt(252)
    sharpe = (cagr - 0.015) / vol if vol > 0 else 0
    mdd = (eq / eq.cummax() - 1).min()
    calmar = cagr / abs(mdd) if mdd != 0 else 0
    print(f"  {cfg['mom_52w']:<5.2f} {cfg['inst_flow']:<5.2f} {cfg['value']:<5.2f} "
          f"{cfg['rev_mom']:<5.2f}  "
          f"{cagr*100:+6.2f}% {sharpe:+7.3f}  {mdd*100:+6.2f}% {calmar:+7.3f}")
    results.append({**cfg, "cagr": cagr, "sharpe": sharpe, "mdd": mdd, "calmar": calmar})

print("─" * 95)
df = pd.DataFrame(results)
print(f"\n🥇 最高 Sharpe：mom={df.loc[df.sharpe.idxmax(), 'mom_52w']:.2f}, "
      f"inst={df.loc[df.sharpe.idxmax(), 'inst_flow']:.2f}, "
      f"Sharpe {df.sharpe.max():.3f}")
print(f"🚀 最高 CAGR  ：mom={df.loc[df.cagr.idxmax(), 'mom_52w']:.2f}, "
      f"inst={df.loc[df.cagr.idxmax(), 'inst_flow']:.2f}, "
      f"CAGR {df.cagr.max()*100:+.2f}%")
