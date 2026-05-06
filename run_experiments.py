#!/usr/bin/env python3
"""
Run a sequence of backtest experiments using strategy/quant_layer2.py's run_pipeline.
"""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("quant_layer2", "strategy/quant_layer2.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

experiments = [
    ("baseline", {}),
    ("A1_lower_turnover", {"rebal_freq": 252, "buffer_multiplier": 2.5, "inertia": 0.90}),
    ("A2_diversify", {"top_n": 50, "rebal_freq": 252, "buffer_multiplier": 2.0, "inertia": 0.85}),
    ("A3_riskparity", {"top_n": 50, "rebal_freq": 252, "buffer_multiplier": 2.0, "use_risk_parity": True, "inertia": 0.85}),
]

results = []
Path("reports").mkdir(exist_ok=True)
for name, params in experiments:
    print(f"\n=== Experiment: {name} | params={params} ===")
    try:
        stats, equity, positions = mod.run_pipeline(save_equity_path=f"reports/equity_{name}.csv", **params)
        print(f"--- Result [{name}] ---")
        for k, v in stats.items():
            print(f"{k}: {v}")
        results.append((name, stats))
    except Exception as e:
        print(f"Experiment {name} failed: {e}")

print("\n=== Summary ===")
for name, stats in results:
    print(f"{name}: {stats.get('年化報酬','-')}, Sharpe: {stats.get('Sharpe Ratio','-')}, Turnover: {stats.get('年化換手率','-')}")
