from data_loader import fetch_and_clean_data

MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiY2FzcGVyaHNpYW8iLCJlbWFpbCI6ImNhc3BlcmhzaWFvMjZAZ21haWwuY29tIn0.tqP_VGSZGt3G-7wUc3Suu40rcvwC3p3tGdE6kGMx0LM"
START_DATE = "2010-01-01"
END_DATE = "2026-04-20"

if __name__ == "__main__":
    print("🚜 啟動大盤加權指數 (TAIEX) 收割機...")
    # FinMind 中，加權指數的代碼就是 TAIEX
    df = fetch_and_clean_data("TAIEX", START_DATE, END_DATE, MY_TOKEN)
    
    if not df.empty:
        print("🎉 加權指數歷史資料下載完成！防護罩材料準備就緒。")
    else:
        print("❌ 下載失敗，請檢查網路或 Token。")