import os
import pandas as pd
from FinMind.data import DataLoader

# 確保專案底下有一個名為 data 的資料夾來放 CSV
if not os.path.exists('data'):
    os.makedirs('data')

def fetch_and_clean_data(stock_id: str, start_date: str, end_date: str, token: str) -> pd.DataFrame:
    """
    具備「本地緩存 (Local Cache)」功能的資料獲取模組。
    """
    # 定義這個資料的專屬檔案名稱
    file_path = f"data/{stock_id}_{start_date}_{end_date}.csv"
    
    # 🌟 核心邏輯：如果硬碟裡已經有這個檔案，直接讀取！不需要網路！
    if os.path.exists(file_path):
        print(f"📂 發現本地快取！直接從硬碟讀取 {stock_id} 的資料...")
        # 讀取 CSV，並將 date 欄位轉回時間索引
        df = pd.read_csv(file_path, index_col='date', parse_dates=True)
        return df
        
    # 如果硬碟沒有，才去呼叫 API
    print(f"📡 本地無資料，正在向伺服器請求 {stock_id} 的標準股價資料...")
    api = DataLoader()
    api.login_by_token(api_token=token)
    
    try:
        df = api.taiwan_stock_daily(
            stock_id=stock_id,
            start_date=start_date,
            end_date=end_date
        )
        
        if df.empty:
            print("⚠️ 警告：無法獲取資料。")
            return pd.DataFrame()
            
        print("✅ 原始資料抓取成功，進行清洗...")
        
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        df = df.rename(columns={'max': 'high', 'min': 'low', 'Trading_Volume': 'volume'})
        df = df[['open', 'high', 'low', 'close', 'volume']]
        
        # 🌟 核心邏輯：清洗完畢後，存入硬碟備用
        df.to_csv(file_path)
        print(f"💾 資料已儲存至 {file_path}")
        
        return df
        
    except Exception as e:
        print(f"❌ 發生錯誤：{e}")
        return pd.DataFrame()

# 測試區塊
if __name__ == "__main__":
    MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2FzcGVyaHNpYW8iLCJlbWFpbCI6ImNhc3BlcmhzaWFvMjZAZ21haWwuY29tIn0.tqP_VGSZGt3G-7wUc3Suu40rcvwC3p3tGdE6kGMx0LM" 
    
    # 你可以連續執行這個檔案兩次！
    # 第一次它會顯示「正在向伺服器請求...」並儲存
    # 第二次它會瞬間顯示「發現本地快取！」並印出資料
    clean_df = fetch_and_clean_data("2330", "2010-01-01", "2026-04-15", MY_TOKEN)
    print(clean_df.head())