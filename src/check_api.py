import requests

# 🔑 換成你的 Token
MY_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJkYXRlIjoiMjAyNi0wNC0yMCAxOTo1OToyOCIsInVzZXJfaWQiOiJjYXNwZXJoc2lhbyIsImlwIjoiMTQwLjExOS42Ni4xMTUiLCJleHAiOjE3NzcyOTExNjh9.5Hh_j2YR9X5W60PzoQHC6jHbKwkuktOzV30GCvxrI4c"

def check_api_status(token):
    print("📡 正在連線 FinMind 伺服器查詢真實額度...")
    
    url = "https://api.web.finmindtrade.com/v2/user_info"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        resp = requests.get(url, headers=headers)
        data = resp.json()
        
        limit = data.get("api_request_limit", "未知")
        used = data.get("user_count", "未知")
        
        print("\n📊 【API 額度真實狀態】")
        print(f"  總配額上限：{limit} 次 / 小時")
        print(f"  目前已使用：{used} 次")
        
        if type(used) == int and type(limit) == int:
            remaining = limit - used
            print(f"  ✨ 剩餘可用：{remaining} 次")
            
            if remaining < 50:
                print("\n🛑 警告：你的額度快乾了！請去讀書，半小時後再回來測。")
            else:
                print("\n✅ 安全：你可以啟動收割機了！")
                
    except Exception as e:
        print(f"❌ 查詢失敗: {e}")
        print("可能是 Token 錯誤或伺服器維護中。")

if __name__ == "__main__":
    check_api_status(MY_TOKEN)