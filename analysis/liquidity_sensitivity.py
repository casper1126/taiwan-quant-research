"""
Liquidity sensitivity: per-rebalance executable volume limit sweep.

This script runs the preferred strategy configuration (A3_riskparity)
and sweeps `max_pct_dv` to show the impact on annual return, Sharpe, turnover,
and per-rebalance executed turnover & cost.

Usage:
    ./venv/bin/python analysis/liquidity_sensitivity.py

Outputs:
    reports/liquidity_sensitivity_summary.csv
    reports/rebalance_liq_{name}.csv
    reports/equity_liq_{name}.csv
"""
from pathlib import Path
import pandas as pd
import numpy as np
import importlib.util

spec = importlib.util.spec_from_file_location("quant_layer2", "strategy/quant_layer2.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Base strategy params (A3-like)
strategy_params = {
    "top_n": 50,
    "rebal_freq": 252,
    "buffer_multiplier": 2.0,
    "use_risk_parity": True,
    "inertia": 0.85,
}

# Sweep values for max_pct_dv (fraction of universe daily dollar volume allowed per rebalance)
sweep = [None, 0.10, 0.05, 0.03, 0.01, 0.005, 0.001]

Path("reports").mkdir(exist_ok=True)
results = []

# Load data & factors once
print("Loading data and building factors...")
data = mod.load_matrices(mod.DB_PATH, mod.START_DATE, mod.END_DATE)
factors = mod.build_factors(data)
close = data["close"]

for val in sweep:
    name = "nolimit" if val is None else f"maxdv_{val}"
    print(f"\nRunning liquidity limit: {val} -> {name}")
    stats, equity, positions = mod.run_pipeline(
        save_equity_path=f"reports/equity_liq_{name}.csv",
        top_n=strategy_params["top_n"],
        rebal_freq=strategy_params["rebal_freq"],
        buffer_multiplier=strategy_params["buffer_multiplier"],
        use_risk_parity=strategy_params["use_risk_parity"],
        inertia=strategy_params["inertia"],
        max_pct_dv=val,
    )

    # diagnostics: compute detailed backtest outputs
    stats_d, equity_d, diag = mod.run_backtest_detailed(close, positions)

    # per-rebalance decomposition
    rebal_idx = np.where(np.arange(len(close)) % strategy_params["rebal_freq"] == 0)[0]
    rebal_dates = close.index[rebal_idx]
    rebal_turn = diag["turnover"].reindex(rebal_dates).fillna(0.0)
    rebal_cost = diag["total_cost"].reindex(rebal_dates).fillna(0.0)
    rebal_df = pd.DataFrame({"turnover": rebal_turn, "cost": rebal_cost})
    rebal_df.to_csv(Path(f"reports/rebalance_liq_{name}.csv"))

    results.append({
        "name": name,
        "max_pct_dv": val,
        "年化報酬": stats_d["年化報酬"],
        "Sharpe": stats_d["Sharpe Ratio"],
        "年化換手率": stats_d["年化換手率"],
        "總交易成本": diag["total_cost"].sum(),
    })

summary = pd.DataFrame(results)
summary.to_csv(Path("reports/liquidity_sensitivity_summary.csv"), index=False)
print("Saved reports/liquidity_sensitivity_summary.csv and per-rebalance decompositions in reports/")
