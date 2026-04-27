import pandas as pd
import numpy as np

def run_portfolio_backtest(price_matrix, position_matrix, commission=0.001425, tax=0.003, slippage=0.001):
    """
    專業級多資產回測引擎：加入交易成本與橫截面報酬計算。
    """
    print("📈 啟動多資產回測引擎 (含交易成本計算)...")
    
    # --- 1. 計算資產每日報酬率矩陣 (加入無限大防呆機制) ---
    asset_returns = price_matrix.pct_change()
    # 🌟 拆彈關鍵：把 inf 和 -inf 替換成 NaN，然後把所有 NaN 補 0
    asset_returns = asset_returns.replace([np.inf, -np.inf], np.nan).fillna(0)
    
    # 2. 計算投資組合每日總報酬
    portfolio_daily_return = (position_matrix.shift(1) * asset_returns).sum(axis=1)
    
    # --- 3. 交易成本計算 ---
    diff_position = position_matrix.diff().abs().sum(axis=1)
    trading_costs = diff_position * (commission + (tax/2) + slippage)
    
    # 扣除成本後的淨報酬
    net_daily_return = portfolio_daily_return - trading_costs
    
    # 4. 計算累積指標
    equity_curve = (1 + net_daily_return).cumprod()
    total_return = equity_curve.iloc[-1] - 1
    
    # 5. 風險指標：MDD 與 Sharpe
    rolling_max = equity_curve.cummax()
    drawdown = equity_curve / rolling_max - 1
    mdd = drawdown.min()
    
    annual_return = net_daily_return.mean() * 252
    annual_vol = net_daily_return.std() * np.sqrt(252)
    sharpe = (annual_return / annual_vol) if annual_vol != 0 else 0
    
    return {
        "Total Return": f"{total_return*100:.2f}%",
        "Max Drawdown": f"{mdd*100:.2f}%",
        "Sharpe Ratio": f"{sharpe:.2f}"
    }, equity_curve

if __name__ == "__main__":
    from alpha_engine import load_multi_factor_data, value_momentum_strategy, get_stock_info_mapping
    
    mapping = get_stock_info_mapping()
    
    # 🌟 這裡多接收一個 revs (營收矩陣)
    prices, pers, pbrs, revs, taiex, tpex = load_multi_factor_data()
    
    # 🌟 這裡把 revs 傳給大腦
    positions = value_momentum_strategy(prices, pers, pbrs, revs, taiex, tpex, mapping, lookback=120, top_n=10)
    
    stats, curve = run_portfolio_backtest(prices, positions)
    
    print("\n📊 【月營收成長 + 價值動能 + 四重裝甲】終極回測報告：")
    for k, v in stats.items():
        print(f"  {k}: {v}")