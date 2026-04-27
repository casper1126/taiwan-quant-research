import os
import time
import requests
import pandas as pd
from data_loader import fetch_and_clean_data

# 🔑 你的 API Token
MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2FzcGVyaHNpYW8iLCJlbWFpbCI6ImNhc3BlcmhzaWFvMjZAZ21haWwuY29tIn0.tqP_VGSZGt3G-7wUc3Suu40rcvwC3p3tGdE6kGMx0LM"

START_DATE = "2010-01-01"
END_DATE = "2026-04-20"

def get_all_tw_stocks(token: str) -> list:
    """獲取全市場普通股名單 (終極過濾版)"""
    print("🔍 正在連線獲取全市場普通股名單...")
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {"dataset": "TaiwanStockInfo", "token": token}
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "data" in data:
            df = pd.DataFrame(data["data"])
            
            # 🌟 核心過濾 1：找出上市 (twse) 與上櫃 (tpex) 的標的
            stock_df = df[df['type'].isin(['twse', 'tpex'])]
            
            # 🌟 核心過濾 2：台股普通股的絕對真理 -> 長度剛好 4 碼，且不以 '00' 開頭
            # 這能完美殺掉 ETF (0050)、權證 (03001X)、特別股 (2881A)、存託憑證 (9103)
            stock_df = stock_df[
                (stock_df['stock_id'].str.len() == 4) & 
                (~stock_df['stock_id'].str.startswith('00'))
            ]
            
            stock_list = stock_df['stock_id'].tolist()
            
            # 為了避免有重複，用 set 去重後再轉回 list 並排序
            stock_list = sorted(list(set(stock_list)))
            
            print(f"✅ 成功獲取 {len(stock_list)} 檔純種普通股代碼！")
            return stock_list
        else:
            print("❌ 獲取名單失敗。")
            return []
    except Exception as e:
        print(f"❌ 網路請求錯誤：{e}")
        return []

if __name__ == "__main__":
    universe = get_all_tw_stocks(MY_TOKEN)
    
    # 🌟 測試模式切換：
    # 目前設定為 [:50] 只抓前 50 檔測試。
    # 等這 50 檔跑完且回測沒問題，把它改成 test_universe = universe 就能抓全市場！
    test_universe = universe
    
    print(f"\n🚜 啟動全市場資料收割機！目標數量：{len(test_universe)} 檔")
    
    # API 速率控制參數
    API_CALLS = 0
    SAFE_LIMIT_PER_HOUR = 595 # 保守設定，避免剛好卡在 600 被鎖
    SLEEP_SECONDS = 3600      # 睡滿 1 小時 (60分鐘) 確保額度完全重置
    
    success_count = 0
    
    for i, stock_id in enumerate(test_universe):
        # 🌟 新增：斷點續傳防護罩
        # 檢查 data 資料夾裡，是不是已經有這檔股票的 CSV 檔了
        already_downloaded = any(f.startswith(f"{stock_id}_") for f in os.listdir('data'))
        if already_downloaded:
            print(f"[{i+1}/{len(test_universe)}] ⏩ 標的 {stock_id} 已存在，自動跳過...")
            continue  # 直接跳到下一檔，不消耗 API！

        print(f"[{i+1}/{len(test_universe)}] 處理標的: {stock_id} ...", end=" ")
        
        # 呼叫 API 抓資料
        df = fetch_and_clean_data(stock_id, START_DATE, END_DATE, MY_TOKEN)
        
        API_CALLS += 1
        
        if not df.empty:
            success_count += 1
            print("✅ 成功")
        else:
            print("⚠️ 無資料")
            
        # --- 🚦 核心 API 速率控制邏輯 ---
        if API_CALLS % SAFE_LIMIT_PER_HOUR == 0:
            print(f"\n🛑 已達到每小時安全配額 ({SAFE_LIMIT_PER_HOUR}次)！")
            print(f"💤 系統進入深度休眠 1 小時以重置 Token 額度，請勿關閉程式...")
            time.sleep(SLEEP_SECONDS)
            print("🟢 休眠結束！額度已重置，繼續收割...\n")
        else:
            # 一般情況下，每抓一檔稍微暫停 0.5 秒，避免瞬間高頻連線被防火牆踢掉
            time.sleep(0.5)
            
    print(f"\n🎉 階段性下載完成！成功獲取 {success_count} 家公司的歷史數據。")