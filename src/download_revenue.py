import os
import time
import requests
import pandas as pd

# 🔑 你的 API Token
MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2FzcGVyaHNpYW8iLCJlbWFpbCI6ImNhc3BlcmhzaWFvMjZAZ21haWwuY29tIn0.tqP_VGSZGt3G-7wUc3Suu40rcvwC3p3tGdE6kGMx0LM"

START_DATE = "2010-01-01"
END_DATE = "2026-04-20"

# 🛡️ API 速率控制參數
API_CALLS = 0
SAFE_LIMIT_PER_HOUR = 595
SLEEP_SECONDS = 3450

def fetch_revenue_data(stock_id: str, start_date: str, end_date: str, token: str, retries=3) -> pd.DataFrame:
    """獲取月營收資料 (TaiwanStockMonthRevenue)"""
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date,
        "token": token
    }
    
    for attempt in range(retries):
        try:
            response = requests.get(url, params=params, timeout=30)
            data = response.json()
            
            if "data" in data and len(data["data"]) > 0:
                df = pd.DataFrame(data["data"])
                # 營收資料的 date 通常標記為當月1日，我們在回測時會處理「10日公佈」的延遲問題
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
                
                # 我們只需要：當月營收(revenue) 與 營收年增率(revenue_YearOnYear_ratio)
                if 'revenue_YearOnYear_ratio' in df.columns:
                    df = df[['revenue', 'revenue_YearOnYear_ratio']]
                else:
                    df = df[['revenue']] # 有些很舊的資料可能沒算好 YoY，我們先保底抓 revenue
                return df
            return pd.DataFrame()
            
        except requests.exceptions.Timeout:
            print(f"⏳ 伺服器超時，正在重試 {attempt + 1}/3 ...")
            time.sleep(3)
        except Exception as e:
            print(f"❌ 網路錯誤: {e}")
            return pd.DataFrame()
            
    return pd.DataFrame()

if __name__ == "__main__":
    # 掃描已下載的實體公司
    price_files = [f for f in os.listdir('data') if f.endswith('.csv') and not f.endswith('_val.csv') and not f.endswith('_rev.csv') and not f.startswith('TAIEX') and not f.startswith('TPEx')]
    stock_ids = sorted(list(set([f.split('_')[0] for f in price_files])))
    
    print(f"🚜 啟動【月營收】原生收割機！目標數量：{len(stock_ids)} 檔")
    
    success_count = 0
    for i, stock_id in enumerate(stock_ids):
        file_path = f"data/{stock_id}_rev.csv"
        
        # 斷點續傳防護罩
        if os.path.exists(file_path) and os.path.getsize(file_path) > 10:
            print(f"[{i+1}/{len(stock_ids)}] ⏩ 月營收 {stock_id} 已存在，自動跳過...")
            success_count += 1
            continue

        # API 速率控制
        if API_CALLS >= SAFE_LIMIT_PER_HOUR:
            print(f"\n🛑 達到配額，進入休眠 {SLEEP_SECONDS/60:.1f} 分鐘...")
            time.sleep(SLEEP_SECONDS)
            API_CALLS = 0
            print("🌅 休眠結束，繼續收割！\n")

        print(f"[{i+1}/{len(stock_ids)}] 抓取月營收: {stock_id} ...", end=" ")
        rev_df = fetch_revenue_data(stock_id, START_DATE, END_DATE, MY_TOKEN)
        API_CALLS += 1
        
        if not rev_df.empty:
            rev_df.to_csv(file_path)
            success_count += 1
            print("✅ 成功")
            time.sleep(1)
        else:
            print("⚠️ 無資料")
            
    print(f"\n🎉 營收資料下載完成！現有 {success_count} 家公司的月營收數據。")