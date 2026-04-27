import time
import os
from data_loader import fetch_and_clean_data

# 構建我們的黃金股票池 (Universe)：精選 20 檔涵蓋各產業的台灣龍頭股
universe = [
    "2330", "2317", "2454", "2308", "2382", # 電子半導體
    "2881", "2882", "2891", "2886", "2884", # 金融保險
    "2002", "1101", "1301", "1303", "2603", # 傳產航運
    "2412", "3045", "4904", "2912", "1216"  # 電信與內需消費
]

# 🔑 你的 API Token
MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2FzcGVyaHNpYW8iLCJlbWFpbCI6ImNhc3BlcmhzaWFvMjZAZ21haWwuY29tIn0.tqP_VGSZGt3G-7wUc3Suu40rcvwC3p3tGdE6kGMx0LM"

# 我們抓取過去 15 年的資料 (經歷過多頭與空頭的完整循環)
START_DATE = "2010-01-01"
END_DATE = "2026-04-15"

if __name__ == "__main__":
    print(f"🚜 啟動全自動資料收割機！準備下載 {len(universe)} 檔股票歷史數據...")
    print("這將會建立我們的本地金礦資料庫 (Local Cache)。\n")

    for i, stock_id in enumerate(universe):
        print(f"[{i+1}/{len(universe)}] 正在處理標的: {stock_id} ...", end=" ")
        
        # 呼叫你之前寫好的、具備「本地快取」功能的資料載入器
        # 如果硬碟已經有，它會瞬間跳過；如果沒有，它會去下載
        df = fetch_and_clean_data(stock_id, START_DATE, END_DATE, MY_TOKEN)
        
        if not df.empty:
            # 為了保護 API 不被伺服器當作惡意攻擊而封鎖，我們每抓一檔就強迫程式「睡覺 3 秒」
            time.sleep(3)
        else:
            print(f"⚠️ {stock_id} 下載失敗或無資料。")

    print("\n🎉 全部下載完成！請檢查你的 data/ 資料夾，裡面應該有滿滿的 CSV 檔案了！")