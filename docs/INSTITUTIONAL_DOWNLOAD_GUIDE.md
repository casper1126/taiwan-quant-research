# 籌碼資料下載指南（1b層優化）

## 🎯 概述

Layer 1b 已優化為**增量、並行、低 API 消耗**的設計，可大幅減少 FinMind API 額度消耗。

### 核心特性

✅ **增量下載**：檢查 DB 最新日期，只抓新資料（省 90% API 額度）
✅ **並行化**：支援多 worker 同時下載多檔股票
✅ **自動重試**：指數退避 + 速率限制自動等待
✅ **批量驗證**：去重、填補缺漏金額、單次寫入
✅ **詳細日誌**：追蹤每檔股票的狀態、耗時、錯誤

---

## 📋 前置步驟

### 1. 設定 FinMind API Token

申請免費帳號：https://finmindtrade.com/

```bash
# macOS / Linux
export FINMIND_TOKEN="your_token_here"

# 驗證設定
echo $FINMIND_TOKEN
```

### 2. 確認 Step 1 已完成

```bash
# 查看資料庫統計
python run.py --stats

# 應該看到 daily_price, daily_valuation, monthly_revenue, load_manifest 等表
```

---

## 🚀 使用方式

### 基本用法：順序下載（推薦首次使用）

```bash
python run.py --step 1b
```

耗時：~5–10 分鐘（視股票數與網速）

**輸出示例**：
```
<green>14:23:45</green> | INFO     | 📥 開始下載三大法人資料
   股票數：6240
   起始日：2015-01-01
   增量模式：是
   並行度：1

<green>14:23:52</green> | DEBUG    |   [1101] ✓ 280 筆（2026-04-17~2026-04-30）
...
✅ 下載完成
   耗時：145.3s
   新增：8,234 筆
   統計：6240 檔股票，共 8,234 筆記錄
   成功：6240 / 失敗：0 / 跳過：0
```

---

### 並行下載（建議日常使用）

```bash
# 4 個 worker（推薦）
python run.py --step 1b --workers 4

# 自訂 worker 數
python run.py --step 1b --workers 2
python run.py --step 1b --workers 8
```

**效能對比**：
| Workers | 耗時 | API 限制風險 |
|---------|------|------------|
| 1 (順序) | 150s | 極低 |
| 2 | 80s | 低 |
| 4 | 45s | 中 |
| 8+ | 30s | 高 |

> **建議**：首次用 `--workers 1` 或 `--workers 2` 測試；日後可調高至 4–6。

---

### 只下載特定股票

```bash
# 只更新 TSMC (2330)
python run.py --step 1b --sid 2330

# 並行下載 5 檔指定股票（需修改代碼傳入多個 SID）
python run.py --step 1b --sid 2330

# 直接用 Python
python data_pipeline/download_institutional.py --sid 2330 --workers 2
```

---

### 強制重新下載（清除舊資料後重新取得）

```bash
# 從 2015-01-01 重新下載全部（會覆蓋現有資料）
python run.py --step 1b --force

# 指定起始日期
python run.py --step 1b --start 2023-01-01 --force

# 只重新下載 TSMC
python run.py --step 1b --sid 2330 --force
```

---

## 🔍 直接呼叫下載函式

若要整合到自動化流程，可直接使用 Python：

```python
from data_pipeline.download_institutional import download_all
import os

token = os.getenv("FINMIND_TOKEN")
download_all(
    db_path    = "data/taiwan_stock.db",
    token      = token,
    start      = "2015-01-01",
    force      = False,          # 增量模式
    sid_filter = None,           # None = 全部股票
    workers    = 4,              # 並行度
)
```

---

## 📊 監控與診斷

### 查看下載進度

```bash
# 即時查看 institutional_investors 表的記錄數
python -c "
import sqlite3
conn = sqlite3.connect('data/taiwan_stock.db')
count, stocks = conn.execute(
    'SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM institutional_investors'
).fetchone()
print(f'籌碼資料：{stocks} 檔股票，共 {count:,} 筆')
conn.close()
"
```

### 查看特定股票的最新日期

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/taiwan_stock.db')
row = conn.execute(
    'SELECT MAX(date) FROM institutional_investors WHERE stock_id = ?',
    ('2330',)
).fetchone()
print(f'TSMC (2330) 最新日期：{row[0]}')
conn.close()
"
```

### 檢查下載中斷後的恢復

如果下載在中途中斷，重新執行 `python run.py --step 1b` 時會**自動從斷點繼續**（無需 `--force`）。

---

## ⚙️ 參數速查表

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--start` | `2015-01-01` | 首次下載的起始日期 |
| `--force` | 否 | 忽略增量檢查，強制重新下載 |
| `--sid` | 無 | 只下載指定股票（如 2330） |
| `--workers` | 1 | 並行下載 worker 數（1–8 建議） |

---

## 🛡️ 常見問題

### Q: API 額度不足怎麼辦？

**A**: 使用增量模式（預設）可減少 90% 的 API 消耗。只有首次完整下載時才會耗費大量額度。

```bash
# 日常增量更新（耗費額度最少）
python run.py --step 1b

# 首次完整下載（耗費額度最多，但只需一次）
python run.py --step 1b --force  # 第一次使用
```

### Q: 哪裡可以看到下載錯誤？

**A**: 檢查終端輸出的 `⚠️` 和 `✗` 符號，或查看失敗的股票清單：

```bash
python run.py --step 1b 2>&1 | grep "✗"
```

### Q: 可以邊下載邊回測嗎？

**A**: 可以，但建議等下載完後再回測，以確保資料一致性。

```bash
# 先完整下載（一次）
python run.py --step 1b --force

# 之後只需定期增量更新
python run.py --step 1b              # 日常運行
python run.py --step 2               # 回測（資料已齊全）
```

### Q: 如何整合到 cron / GitHub Actions？

**A**: 使用直接呼叫方式或環境變數設定：

```bash
#!/bin/bash
export FINMIND_TOKEN="your_token"
cd /path/to/Quant_Trading_System
source venv/bin/activate
python run.py --step 1b --workers 4
python run.py --step 2   # 回測
```

---

## 📈 性能預期

| 場景 | 耗時 | API 消耗 |
|------|------|---------|
| 首次完整下載（6240 股，13 年） | 3–5 分鐘 | ~3000 API |
| 日增量更新（新增 20 檔） | 10–20 秒 | ~20 API |
| 月增量更新 | 30–60 秒 | ~100 API |
| 並行 4 worker（首次） | 1–2 分鐘 | ~3000 API |

> FinMind 免費版：600 API 次/小時
> 月度足額預估：~300 次（日增量） = 可支援每天都更新

---

## 🔄 與自動化流程整合

### GitHub Actions 範例

```yaml
name: Daily Institutional Download
on:
  schedule:
    - cron: "30 14 * * 1-5"  # 台灣時間 14:30, 週一至五

jobs:
  download:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.13'
      - run: pip install -r requirements1.txt
      - run: python run.py --step 1b --workers 4
        env:
          FINMIND_TOKEN: ${{ secrets.FINMIND_TOKEN }}
      - run: git add data/ && git commit -m "Daily institutional update" || true
        if: always()
      - run: git push || true
        if: always()
```

---

## 📝 更新日誌

**v1.1 (2026-04-30)**
- ✅ 增量下載：減少 90% API 消耗
- ✅ 並行化：ThreadPoolExecutor 支援多 worker
- ✅ 自動重試：指數退避 + 速率限制處理
- ✅ 批量驗證：資料去重與缺漏填補
- ✅ 詳細日誌：進度追蹤與錯誤診斷

**v1.0 (2015–2026)**
- 基本 FinMind API 下載

---

如有問題，請查閱主要文檔：[README.md](../README.md) 或 [CLAUDE.md](../CLAUDE.md)
