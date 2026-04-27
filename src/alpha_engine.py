import os
import pandas as pd
import numpy as np
import requests

def get_stock_info_mapping():
    print("📡 正在向 API 索取全市場身分證名冊 (包含產業別)...")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanStockInfo"}
    try:
        res = requests.get(url, params=params).json()
        mapping = {}
        if "data" in res:
            for row in res["data"]:
                mapping[row["stock_id"]] = {
                    "market_type": row["type"],
                    "industry": row.get("industry_category", "未知")
                }
        return mapping
    except Exception as e:
        print("⚠️ 取得名冊失敗。")
        return {}

def load_multi_factor_data(data_dir='data'):
    print(f"📂 正在從 {data_dir}/ 載入 1700 檔【價格+財報+月營收】與雙重氣象局...")
    
    price_files = [f for f in os.listdir(data_dir) if f.endswith('.csv') and not f.endswith('_val.csv') and not f.endswith('_rev.csv') and not f.startswith('TAIEX') and not f.startswith('TPEx')]
    valid_stocks = []
    price_dict, per_dict, pbr_dict, rev_dict = {}, {}, {}, {}
    
    for file in price_files:
        stock_id = file.split('_')[0]
        val_file = f"{stock_id}_val.csv"
        rev_file = f"{stock_id}_rev.csv"
        
        if os.path.exists(os.path.join(data_dir, val_file)) and os.path.exists(os.path.join(data_dir, rev_file)):
            p_df = pd.read_csv(os.path.join(data_dir, file), index_col='date', parse_dates=True)
            v_df = pd.read_csv(os.path.join(data_dir, val_file), index_col='date', parse_dates=True)
            r_df = pd.read_csv(os.path.join(data_dir, rev_file), index_col='date', parse_dates=True)
            
            price_dict[stock_id] = p_df['close']
            per_dict[stock_id] = v_df['PER']
            pbr_dict[stock_id] = v_df['PBR']
            
            # 🌟 修正點：我們只抓最原始的「營業額 (revenue)」
            if 'revenue' in r_df.columns:
                rev_dict[stock_id] = r_df['revenue']
            
            valid_stocks.append(stock_id)

    price_matrix = pd.DataFrame(price_dict).replace(0.0, np.nan).ffill()
    per_matrix = pd.DataFrame(per_dict).replace(0.0, np.nan).ffill()
    pbr_matrix = pd.DataFrame(pbr_dict).replace(0.0, np.nan).ffill()
    
    # ==========================================
    # 🌟 營收矩陣的「自製 YoY 與防作弊」魔法
    # ==========================================
    raw_rev = pd.DataFrame(rev_dict)
    raw_rev.index = pd.to_datetime(raw_rev.index)
    
    # 魔法 1：自己算 YoY！(現在營收 / 12個月前營收 - 1) * 100
    # 因為是月資料，所以往前推 12 格就是去年同月
    yoy_matrix = raw_rev.pct_change(periods=12) * 100
    
    # 魔法 2：避免未來函數，把時間推遲到下個月 10 號 (40天後發布新聞)
    # 完美魔法：往後推 1 個月 (不管大小月)，再加上 9 天 (從 1號 變成 10號)
    from pandas.tseries.offsets import DateOffset
    yoy_matrix.index = yoy_matrix.index + DateOffset(months=1, days=9)
    yoy_matrix = yoy_matrix.sort_index()
    
    # 魔法 3：安全對齊到每一天的交易日，查不到就用舊新聞 (ffill)
    final_yoy_matrix = yoy_matrix.reindex(price_matrix.index, method='ffill')
    
    taiex_file = [f for f in os.listdir(data_dir) if f.startswith('TAIEX')][0]
    tpex_file = [f for f in os.listdir(data_dir) if f.startswith('TPEx')][0]
    taiex_series = pd.read_csv(os.path.join(data_dir, taiex_file), index_col='date', parse_dates=True)['close']
    tpex_series = pd.read_csv(os.path.join(data_dir, tpex_file), index_col='date', parse_dates=True)['close']
    
    print(f"✅ 矩陣對齊完成！(靠大腦自己算出真正的 YoY) 共有 {len(valid_stocks)} 檔股票。")
    return price_matrix, per_matrix, pbr_matrix, final_yoy_matrix, taiex_series, tpex_series

# 🌟 注意參數多了 rev_matrix
def value_momentum_strategy(price_matrix, per_matrix, pbr_matrix, rev_matrix, taiex_series, tpex_series, mapping, lookback=120, top_n=10):
    print(f"🧠 啟動全市場多因子運算 (目標: 前 {top_n} 名 | 新增 YoY > 15% 火力濾網)...")
    
    # 1. 🌟 終極邏輯：價值 + 月營收高成長
    print(f"🧠 啟動全市場多因子運算 (目標: 前 {top_n} 名 | 裝載防追高乖離率系統)...")
    
    # 🌟 新增：計算 20 日乖離率 (Bias Ratio)
    # 公式：(今日股價 - 20日均價) / 20日均價
    ma_20_matrix = price_matrix.rolling(window=20).mean()
    bias_20_matrix = (price_matrix - ma_20_matrix) / ma_20_matrix
    
    # 🌟 終極邏輯：極簡暴力美學 + 拒絕追高！
    # 條件 1: PE > 0 (有賺錢)
    # 條件 2: rev_matrix > 0 (營收正成長)
    # 條件 3: bias_20_matrix < 0.15 (乖離率小於 15%，拒絕買在連續噴出的末升段，不當韭菜！)
    value_growth_mask = (per_matrix > 0) & (rev_matrix > 0.0) & (bias_20_matrix < 0.70)
    
    momentum_matrix = price_matrix.pct_change(periods=lookback)
    masked_momentum = momentum_matrix.where(value_growth_mask, np.nan)
    rank_matrix = masked_momentum.rank(axis=1, ascending=False)
    
    position_matrix = pd.DataFrame(0.0, index=price_matrix.index, columns=price_matrix.columns)
    rebalance_mask = np.arange(len(price_matrix)) % 20 == 0
    target_weights = pd.DataFrame(np.nan, index=price_matrix.index, columns=price_matrix.columns)
    
    base_weight = 1.0 / top_n
    valid_buy = (rank_matrix <= top_n) & (masked_momentum > 0)
    target_weights.loc[rebalance_mask] = np.where(valid_buy.loc[rebalance_mask], base_weight, 0.0)
    position_matrix = target_weights.ffill().fillna(0.0)
    
    # 2. 四重均線動態計分系統 (維持不變)
    def calculate_market_score(index_series):
        score = pd.Series(0, index=index_series.index)
        score += (index_series > index_series.rolling(window=10).mean()).astype(int)
        score += (index_series > index_series.rolling(window=30).mean()).astype(int)
        score += (index_series > index_series.rolling(window=60).mean()).astype(int)
        score += (index_series > index_series.rolling(window=120).mean()).astype(int)
        return score / 4.0 

    taiex_exposure = calculate_market_score(taiex_series).reindex(position_matrix.index).fillna(0.0)
    tpex_exposure = calculate_market_score(tpex_series).reindex(position_matrix.index).fillna(0.0)
    
    # 3. 掛載對應的曝險水位
    for stock in position_matrix.columns:
        stock_info = mapping.get(stock, {"market_type": "twse"})
        market_type = stock_info.get("market_type", "twse")
        
        if market_type == 'tpex':
            position_matrix[stock] = position_matrix[stock] * tpex_exposure
        else:
            position_matrix[stock] = position_matrix[stock] * taiex_exposure
            
    position_matrix = position_matrix.shift(1).fillna(0.0)
    
    print("✨ 營收飆股動態裝甲計算完成！")
    return position_matrix