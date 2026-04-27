import pandas as pd

def add_technical_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    接收乾淨的 OHLCV DataFrame，並計算各種量化特徵 (Alpha Factors)。
    
    Args:
        df (pd.DataFrame): 包含 DatetimeIndex 與 OHLCV 的乾淨資料。
        
    Returns:
        pd.DataFrame: 附加了新特徵欄位的 DataFrame。
    """
    # 為了不污染原始資料，我們先複製一份
    data = df.copy()
    
    print("🧠 正在進行特徵工程計算...")
    
    # --- 1. 計算日報酬率 (Daily Return) ---
    # 這是所有量化統計模型最基礎的輸入值，比單純的價格更有數學意義
    # pct_change() 會自動計算 (今天收盤價 - 昨天收盤價) / 昨天收盤價
    data['daily_return'] = data['close'].pct_change()
    
    # --- 2. 計算移動平均線 (Moving Averages) ---
    # rolling(window=20).mean() 代表取過去 20 天的平均值
    data['SMA_20'] = data['close'].rolling(window=20).mean()
    data['SMA_60'] = data['close'].rolling(window=60).mean()
    
    # --- 3. 計算歷史波動率 (Historical Volatility) ---
    # 風險管理的核心：過去 20 天報酬率的標準差 (Standard Deviation)
    data['volatility_20'] = data['daily_return'].rolling(window=20).std()
    
    # --- 4. 處理 NaN 值 ---
    # 因為我們計算了 60 MA，前 59 天一定算不出東西 (產生 NaN)
    # 專業做法是直接把這些「尚未預熱完成」的列刪除掉
    data = data.dropna()
    
    print("✨ 特徵計算完成！")
    return data

# ==========================================
# 測試區塊 (模擬串接)
# ==========================================
if __name__ == "__main__":
    # 這裡我們模擬從 data_loader 引入剛剛寫好的函數
    from data_loader import fetch_and_clean_data
    
    # 🔑 貼上你的 Token
    MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2FzcGVyaHNpYW8iLCJlbWFpbCI6ImNhc3BlcmhzaWFvMjZAZ21haWwuY29tIn0.tqP_VGSZGt3G-7wUc3Suu40rcvwC3p3tGdE6kGMx0LM"
    
    # 步驟一：抓取並清洗資料 (時間拉長到 4 年)
    raw_df = fetch_and_clean_data(
        stock_id="2330", 
        start_date="2010-01-01", 
        end_date="2026-04-15",
        token=MY_TOKEN
    )
    
    if not raw_df.empty:
        # 步驟二：把洗好的資料丟進我們的新模組，進行特徵工程
        feature_df = add_technical_features(raw_df)
        
        print("\n📊 加上特徵後的 DataFrame 長這樣 (只顯示前 5 筆)：")
        # 為了看清楚所有欄位，我們可以這樣設定 pandas
        pd.set_option('display.max_columns', None)
        print(feature_df.head())