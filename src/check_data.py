import os
import pandas as pd

def inspect_valuation_data():
    """自動隨機抽查硬碟裡的估值資料，驗證真偽"""
    print("🔍 啟動資料庫法醫抽查程序...\n")
    
    # 找出 data 資料夾裡所有結尾是 _val.csv 的檔案
    val_files = [f for f in os.listdir('data') if f.endswith('_val.csv')]
    
    if not val_files:
        print("❌ 找不到任何估值檔案！下載任務真的失敗了 (假成功)。")
        return
        
    print(f"📂 總共發現 {len(val_files)} 個估值檔案。")
    
    # 我們隨便挑第一個檔案來「開箱驗屍」
    sample_file = val_files[0]
    file_path = os.path.join('data', sample_file)
    stock_id = sample_file.split('_')[0]
    
    print(f"\n📦 正在開箱抽查標的：【{stock_id}】")
    print("-" * 50)
    
    # 讀取 CSV
    df = pd.read_csv(file_path, index_col='date', parse_dates=True)
    
    # 1. 檢查資料筆數
    print(f"1️⃣ 總筆數驗證：共有 {len(df)} 個交易日的資料。")
    if len(df) < 100:
        print("⚠️ 警告：資料少於 100 筆，可能只抓到最近幾天的碎片！")
    else:
        print("✅ 筆數正常，涵蓋長期的歷史軌跡。")
        
    # 2. 檢查近期資料 (看最近一天的數字長怎樣)
    print("\n2️⃣ 近期真實數據擷取 (Tail)：")
    print(df.tail(3))
    
    # 3. 檢查合理性 (Describe 計算平均值與極值)
    print("\n3️⃣ 數學合理性驗證 (Describe)：")
    print(df.describe())
    
    print("-" * 50)
    print("💡 【判讀教學】")
    print("- PER (本益比): 正常公司通常在 8 ~ 30 之間。如果都是 0 或負數，代表公司長期虧損或資料有誤。")
    print("- PBR (股價淨值比): 正常通常在 0.5 ~ 5 之間。")
    print("- dividend_yield (殖利率): 正常在 0.0 ~ 10.0 之間 (單位是 %)。")

if __name__ == "__main__":
    inspect_valuation_data()