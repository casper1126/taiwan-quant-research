import requests, urllib3
urllib3.disable_warnings()
_orig = requests.Session.request
def _no_verify(self, method, url, **kwargs):
    kwargs["verify"] = False
    return _orig(self, method, url, **kwargs)
requests.Session.request = _no_verify

# 以上必須在 import tejapi 之前
import tejapi
tejapi.ApiConfig.api_base = "https://api.tej.com.tw"
tejapi.ApiConfig.api_key  = "2By44kOBDXUj0kl9WHbY2SxNiYxO0W"

df = tejapi.get(
    "TRAIL/TAPRCD",
    paginate=True,
    coid="2330",
    mdate={"gte": "2024-01-01", "lte": "2024-03-31"}
)
print(df.head())
print(f"欄位：{list(df.columns)}")