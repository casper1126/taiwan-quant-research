"""
Cost sensitivity and rebalancing-level decomposition for the strategy.

This script runs the chosen strategy configuration and varies trading cost
parameters to show impact on annual return, Sharpe, and turnover. It also
computes turnover and cost by rebalance date and writes CSV outputs.

Usage:
    ./venv/bin/python analysis/cost_sensitivity.py

Outputs:
    reports/cost_sensitivity_summary.csv
    reports/cost_sensitivity_details_{name}.csv (per-run daily diagnostics)
    reports/rebalance_decomp_{name}.csv (per-rebalance turnover & cost)
"""
from pathlib import Path
import itertools
import pandas as pd
import numpy as np
import importlib.util

spec = importlib.util.spec_from_file_location("quant_layer2", "strategy/quant_layer2.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Strategy config to analyze (A3 risk-parity setup we found effective)
strategy_params = {
    "top_n": 50,
    "rebal_freq": 252,
    "buffer_multiplier": 2.0,
    "use_risk_parity": True,
    "inertia": 0.85,
}

# Cost parameter grid (reasonable ranges)
commissions = [0.001425, 0.0007, 0.0003, 0.0]
slippages = [0.001, 0.0005, 0.0001, 0.0]
taxes = [0.003, 0.0015, 0.0]

Path("reports").mkdir(exist_ok=True)

# load data and build positions once (positions don't depend on costs)
print("Loading data and building positions...")
data = mod.load_matrices(mod.DB_PATH, mod.START_DATE, mod.END_DATE)
factors = mod.build_factors(data)
positions = mod.build_positions(factors, **strategy_params)
close = data["close"]

results = []

for c, s, t in itertools.product(commissions, slippages, taxes):
    name = f"c{c}_s{s}_tax{t}".replace('.', 'p')
    print(f"\nRunning costs: commission={c}, slippage={s}, tax={t}  -> {name}")
    stats, equity, diagnostics = mod.run_backtest_detailed(close, positions,
                                                         commission=c, tax=t, slippage=s)

    # Save daily diagnostics
    df_diag = pd.DataFrame({
        "gross_ret": diagnostics["gross_ret"],
        "net_ret": diagnostics["net_ret"],
        "turnover": diagnostics["turnover"],
        "total_cost": diagnostics["total_cost"],
        "equity": equity,
    })
    diag_path = Path(f"reports/cost_sensitivity_details_{name}.csv")
    df_diag.to_csv(diag_path)

    # Rebalance decomposition
    rebal_idx = np.where(np.arange(len(close)) % strategy_params["rebal_freq"] == 0)[0]
    rebal_dates = close.index[rebal_idx]
    turnover_on_rebal = diagnostics["turnover"].reindex(rebal_dates).fillna(0.0)
    cost_on_rebal = diagnostics["total_cost"].reindex(rebal_dates).fillna(0.0)
    rebal_df = pd.DataFrame({"turnover": turnover_on_rebal, "cost": cost_on_rebal})
    rebal_df["cum_cost"] = rebal_df.cumsum()["cost"]
    rebal_df.to_csv(Path(f"reports/rebalance_decomp_{name}.csv"))

    results.append({
        "name": name,
        "commission": c,
        "slippage": s,
        "tax": t,
        "年化報酬": stats["年化報酬"],
        "Sharpe": stats["Sharpe Ratio"],
        "年化換手率": stats["年化換手率"],
        "總交易成本": df_diag["total_cost"].sum(),
    })

summary = pd.DataFrame(results)
summary.to_csv(Path("reports/cost_sensitivity_summary.csv"), index=False)
print("\nSaved summary to reports/cost_sensitivity_summary.csv")
print("Saved per-run diagnostics and rebalance decompositions to reports/.")
