import os
import time
import requests
import pandas as pd

# 🔑 你的 API Token
MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNi0wNC0yMCAxOTo1OToyOCIsInVzZXJfaWQiOiJjYXNwZXJoc2lhbyIsImlwIjoiMTQwLjExOS42Ni4xMTUiLCJleHAiOjE3NzcyOTExNjh9.5Hh_j2YR9X5W60PzoQHC6jHbKwkuktOzV30GCvxrI4c"

START_DATE = "2010-01-01"
END_DATE = "2026-04-20"

# 🛡️ API 速率控制參數
API_CALLS = 0
SAFE_LIMIT_PER_HOUR = 595 # 保守設定，避免剛好卡在 600 被鎖
SLEEP_SECONDS = 1

def fetch_valuation_data(stock_id: str, start_date: str, end_date: str, token: str, retries=3) -> pd.DataFrame:
    """使用原生 Requests 獲取估值資料 (附帶工業級自動重試機制)"""
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockPER",
        "data_id": stock_id,
        "start_date": start_date,
        "end_date": end_date,
        "token": token
    }
    
    # 🌟 核心魔法：重試迴圈
    for attempt in range(retries):
        try:
            # 把 timeout 拉長到 30 秒
            response = requests.get(url, params=params, timeout=30)
            data = response.json()
            
            if "data" in data and len(data["data"]) > 0:
                df = pd.DataFrame(data["data"])
                df['date'] = pd.to_datetime(df['date'])
                df.set_index('date', inplace=True)
                df = df[['dividend_yield', 'PER', 'PBR']]
                return df
            return pd.DataFrame() # 真的沒有資料
            
        except requests.exceptions.Timeout:
            print(f"⏳ 伺服器處理過久 (Timeout)，正在進行第 {attempt + 1}/3 次重試...")
            time.sleep(3) # 喘口氣再重試
        except Exception as e:
            print(f"❌ 網路請求錯誤: {e}")
            return pd.DataFrame()
            
    print("❌ 重試 3 次仍失敗，暫時跳過此標的。")
    return pd.DataFrame()

if __name__ == "__main__":
    price_files = [f for f in os.listdir('data') if f.endswith('.csv') and not f.endswith('_val.csv') and not f.startswith('TAIEX') and not f.startswith('TPEx')]
    stock_ids = sorted(list(set([f.split('_')[0] for f in price_files])))
    
    print(f"🚜 啟動估值資料原生收割機！目標數量：{len(stock_ids)} 檔")
    
    success_count = 0
    for i, stock_id in enumerate(stock_ids):
        file_path = f"data/{stock_id}_val.csv"
        
        # 🌟 斷點續傳：如果檔案存在，且檔案大小 > 10 bytes (不是空殼)，就跳過
        if os.path.exists(file_path) and os.path.getsize(file_path) > 10:
            print(f"[{i+1}/{len(stock_ids)}] ⏩ 標的估值 {stock_id} 已存在，自動跳過...")
            success_count += 1
            continue

        # 🛑 速率控制防護罩
        if API_CALLS >= SAFE_LIMIT_PER_HOUR:
            print(f"\n🛑 已達到每小時安全配額 ({SAFE_LIMIT_PER_HOUR}次)。")
            print(f"😴 程式將進入深度休眠 {SLEEP_SECONDS/60:.1f} 分鐘以重置配額...")
            time.sleep(SLEEP_SECONDS)
            API_CALLS = 0
            print("🌅 休眠結束，恢復執行！\n")

        print(f"[{i+1}/{len(stock_ids)}] 處理標的估值: {stock_id} ...", end=" ")
        val_df = fetch_valuation_data(stock_id, START_DATE, END_DATE, MY_TOKEN)
        API_CALLS += 1
        
        if not val_df.empty:
            val_df.to_csv(file_path)
            success_count += 1
            print("✅ 成功")
            time.sleep(1) # 基本防護
        else:
            print("⚠️ 無資料 (可能為 ETF 或當年無財報)")
            
    print(f"\n🎉 估值資料下載完成！現有 {success_count} 家公司的 PE/PB 數據。")