# PROJECT_STATUS.md

盤點日期基準：以當前 repo 實際檔案內容為準（非任務文件描述）。
比對對象：`CLAUDE_CODE_TASKS.md.pdf` 的 Task 1–9。

## 執行紀錄

- **前置修復**：`automation/daily_update.py:493` 的 `return` outside function
  SyntaxError 已修復（見 commit `fix(automation): ...`），該腳本現在可以正常被解析/執行。
- **前置修復（重大）**：本機 venv 完全壞掉——`venv/bin/python3.13` 直接不存在（Homebrew 的
  `python@3.13` 在某個時間點被移除，只剩 `python@3.12`/`python@3.14`），且 `lightgbm` 依賴的
  `libomp.dylib` 也一併消失。已用 `brew install python@3.13` + `python3.13 -m venv --upgrade venv`
  修復（保留原本 site-packages，不用整個重裝），再 `brew install libomp` 修好 lightgbm。
  修復後確認 pandas/numpy/scipy/sklearn/lightgbm/discord.py/matplotlib/pytest/`FINMIND_TOKEN`
  全部可正常匯入/讀取。**這代表在這次修復之前，repo 裡任何 `python xxx.py` 指令在這台機器上
  其實都跑不動**，不只是 Task 8 的 daily_update.py。
- Task 1 執行中（見下方「Task 1 細節」的最新狀態）。

## 環境注意事項（給未來的自己）

這台機器的 venv 已經連續出過兩輪問題，兩輪症狀不同但都跟「venv 沒有隨系統套件更新而同步」有關。
記錄下來是為了下次壞掉時不用重新診斷一遍。

**背景（機台遷移）**：專案後來從 MacBook Air 換到 MacBook Pro 開發，venv 在遷移時整個重建過
（不是修復，是刪掉整個 `venv/` 資料夾重裝：`python3.13 -m venv venv` + 重新
`pip install -r requirements1.txt`）。遷移過程撞到過一次 `certifi` 憑證路徑失效的問題，用
`pip install --force-reinstall certifi` 解決。**現在 Pro 是唯一開發機，Air 已停用，不會再有
兩台機器的資料要合併。**

**這次 session 撞到的問題（第一輪）**：`venv/bin/python3.13` 整個不見了。原因是 Homebrew 的
`python@3.13` 在某個時間點被移除（`brew list` 只剩 `python@3.12` / `python@3.14`），連帶
`lightgbm` 依賴的 `libomp.dylib` 也消失。修復方式是重裝 `python@3.13`，再用 `--upgrade` 模式
修 venv（保留原本已裝好的 site-packages，不用整個重來）：

```bash
brew install python@3.13
/opt/homebrew/opt/python@3.13/bin/python3.13 -m venv --upgrade /Users/casper/Downloads/Quant_Trading_System/venv
brew install libomp   # 修 lightgbm 的 dlopen 錯誤（Library not loaded: @rpath/libomp.dylib）
```

**這次 session 撞到的問題（第二輪，就在修完第一輪之後、全量資料回填跑到一半時發生）**：
`requests` 開始對每一支股票都報 `OSError: Could not find a suitable TLS CA certificate bundle,
invalid path: .../venv/lib/python3.13/site-packages/certifi/cacert.pem`——跟機台遷移時撞到的
**是同一個症狀**。修復方式同樣是強制重裝 `certifi`：

```bash
venv/bin/pip install --force-reinstall certifi
```

**這代表什麼**：`python3.13 -m venv --upgrade venv` 這種「保留 site-packages、只補回直譯器」的
輕量修復方式，`certifi` 這個套件似乎特別容易在之後的真實大量 HTTPS 請求中失效（可能是套件裡的
`cacert.pem` 資料檔在 venv 沒有正確重新連結，import 測試階段不會觸發，只有真的送出大量 HTTPS
請求時才會浮現）。**教訓：修完 venv 之後，除了 `import` 測試，一定要跑一段會真的打 API 的小規模
測試（不只是 import 成功），才能確認 venv 真的健康。**

**這次 session 撞到的問題（第三輪）**：在 Task 2 進行中，發現 `venv/lib/python3.13/site-packages/`
的內容整個換掉了——套件版本全部往上跳（例如 `pandas` 3.0.2→3.0.5、`numpy` 2.4.4→2.5.2、
`lightgbm` 4.6.0→4.7.0），而且只剩下 `requirements1.txt` 裡列的那幾個套件（連同它們的依賴），
原本裝著的 `matplotlib`、`pytest`、`discord.py`、`FinMind`、`tejapi` 等等全部消失。這應該是
在同一個 session 裡，這台 Pro 機器又做了一次跟「Air→Pro 遷移」當時一樣的操作
（`rm -rf venv && python3.13 -m venv venv && pip install -r requirements1.txt`）。
**這證實了一件事：`requirements1.txt` 目前不完整，會直接導致「整個重建 venv」這個（看起來已經
用過不只一次的）标準修復手段，重建出一個缺套件的環境。** 已經動手修：把 `matplotlib`、`pytest`
補進 `requirements1.txt` 並裝回 venv（這兩個是目前**活躍程式碼**真的會用到的：`matplotlib`
被 `pairing_analyzer.py`／`portfolio_combiner.py` 使用，`pytest` 是 Task 2 驗收要求）。
`hmmlearn`、`shap`、`discord.py` 目前沒裝也沒補進 `requirements1.txt`——前兩個照任務書規劃
本來就是 Task 8 才要補的，`discord.py` 則是 `_disabled/discord/bot.py` 停用中才用得到，
見 `_disabled/discord/README_RESTORE.md`。

**未來遇到 venv 壞掉時的判斷順序**：
1. `venv/bin/python3 --version` 失敗 → 直譯器不見了 → 檢查 `brew list | grep python`，
   重裝對應版本 + `python3.13 -m venv --upgrade venv`
2. `import lightgbm` 時 `dlopen` 失敗、抱怨 `libomp.dylib` → `brew install libomp`
3. 大量 `requests` 呼叫時開始報 TLS/certifi 相關的 `OSError` → `pip install --force-reinstall certifi`
4. 如果以上都試過還是不穩，才考慮整個重建（`rm -rf venv && python3.13 -m venv venv &&
   venv/bin/pip install -r requirements1.txt`）——**這個指令現在應該是安全的**，因為
   `matplotlib`／`pytest` 已經補進 `requirements1.txt`；但如果之後又手動裝了新套件，
   記得同步補進 `requirements1.txt`，不然下次重建又會重演這輪的問題。

**風險提示**：`brew install` 常常會自動觸發 `brew cleanup`（這次修復過程就親眼看到，
`brew install libomp` 之後自動跑了 cleanup），有可能在無關的維護操作中把 `python@3.13` 或
`libomp` 又清掉。之後如果又遇到「昨天還能跑，今天 import 就炸」，先懷疑 venv，不要懷疑程式碼。

## 0. 先看這個：任務文件的「現況」描述已經落後於實際程式碼

`DEVELOPMENT_PROMPT.md.pdf` 與 `CLAUDE_CODE_TASKS.md.pdf` 都把 `quant_layer2.py` 描述成
「v5、籌碼因子佔位未載入、IC 0.014」。但實際讀 [strategy/quant_layer2.py](../strategy/quant_layer2.py)：

- `inst_flow`（籌碼因子）**已經接入**，用 60 日累積淨買超計算，且已在因子權重中佔 0.24
- 因子權重**不是任務書假設的靜態值**，而是用 252 日滾動 `|IC|` 動態分配權重（`ic_rolling`），
  静態權重只是資料不足時的 fallback
- 除了任務書提到的 momentum/value/rev_yoy/low_vol 之外，還多了 `mom_120`、`div_yld` 兩個因子

更關鍵的是：專案裡還有一整條任務文件完全沒提到的研究分支（[strategy/](../strategy/) 底下）：
`quant_layer3.py`（Layer 3 重新設計版）、`quant_pure_ml.py`（純 ML 策略 M1）、`cta_module.py`
（CTA 策略 I）、`pead_module.py`（PEAD 策略 J）、`pairs_module.py`（配對交易 K）、
`pairing_analyzer.py`、`portfolio_combiner.py`（多 sleeve 組合 N2）。`predict_model.py`
目前實際使用的策略是 `config.json` 裡寫的 **「N1 v2 ML (mom_52w 60% + inst_flow 40%)」**，
跟任務書鎖定的 `quant_layer2.py` 已經是不同世代的東西。

**建議先跟你確認一件事再開始 Task 1**：Task 3–4 的機制偵測要接到任務書指定的
`quant_layer2.py`（照文件字面走），還是要接到你後來發展出的、看起來更成熟的
Layer 3 / N1 / N2 組合？這會影響 Task 2、4 的實際落點，越晚發現代價越大。

---

## 1. Task 1–9 完成度總表

| Task | 標題 | 完成度 | 判斷依據 |
|---|---|---|---|
| 1 | 資料補完 | **程式碼完成，資料回填中（institutional/TAIEX 已通過驗收，margin_trading 進行中）** | 見下方細節 |
| 2 | 因子庫模組化重構 | **程式碼完成，`python run.py --step 2` 跑通，pytest 11/11 過；⚠️ inst_flow IC 驗收未過關（實測 0.0034 < 0.03，誠實記錄，非模擬值）** | 見下方細節 |
| 3 | 機制偵測模組 | **未開始 (0%)** | `regime/` 資料夾不存在 |
| 4 | 機制整合到策略 | **未開始 (0%)** | 依賴 Task 3；`REGIME_FACTOR_WEIGHTS` 未出現在程式碼中 |
| 5 | ML 因子合成 | **未開始 (0%)**，但有可參考的既有 ML 策略 | `strategy/ml_composite.py` 不存在；`quant_pure_ml.py` 精神類似但規格不同 |
| 6 | 驗證框架 | **未開始 (0%)**，但有舊版可參考 | 規格要求的檔案/報告都不存在，見下方 |
| 7 | 研究誠信模組 | **未開始 (0%)** | `survivorship.py` / `decay_monitor.py` / `capacity.py` 均不存在 |
| 8 | 自動化整合 | **部分完成，且目前壞掉** | 見下方（`daily_update.py` 有 SyntaxError） |
| 9 | 文件與發布 | **未開始** | README 現況以 Discord bot 為敘事核心，非作品集規格 |

### Task 1 細節（資料補完）—— 最新狀態

- **1a schema.py 三張表**：全部完成。`institutional_investors` 原本就有；新增
  `margin_trading`、`market_index` 兩張表 + `stock_id`/`date`/`index_id` 索引，寫在
  [data_pipeline/schema.py](../data_pipeline/schema.py) 的 `TABLE_DDL`/`TABLE_INDEXES`
  （單一事實來源，沒有另外複製一份 DDL）。
- **1b download_supplementary.py**：已建立
  [data_pipeline/download_supplementary.py](../data_pipeline/download_supplementary.py)。
  **範疇決定**（跟任務書字面規格不同，這裡說明理由）：只負責 `margin_trading`（融資融券）
  與 `market_index`（TAIEX），**不**重新實作三大法人買賣超——那份邏輯已經在
  [data_pipeline/download_institutional.py](../data_pipeline/download_institutional.py)
  裡跑得很好且已達標（2056 檔 ≥ 1500），重寫一份等於維護兩套幾乎一樣的下載器，風險大於好處。
  已實作：斷點續傳（`MAX(date)` 查詢）、固定節奏速率控制（0.5s/call，同一整點時段滿 550 次後
  睡到下個整點，照任務書規格）、3 次重試（含 402 特殊處理）、tqdm 進度條。
- **1c run.py --step 1b**：已擴充 [run.py](../run.py) 的 `step1b_institutional()`，現在會
  依序呼叫法人下載 → 融資融券/TAIEX 下載，`--step 1b` 一個指令涵蓋全部三個資料集。
- **小規模實測**（`--sid 2330`，真實打 FinMind API）：三個資料集都正確寫入，`market_index`
  抓到 2015-01-05~2026-08-17 共 2831 筆合理的 TAIEX 資料，`margin_trading` 抓到 2330 的
  2831 筆融資融券資料，數值量級正常。
- **全量回填**：`python run.py --step 1b`（不帶 `--sid`，涵蓋全部 ~2095 檔）目前在你自己的終端機
  （PID 69006）執行中——過程中撞到兩個process 衝突 + certifi 失效的插曲，細節見上方
  「環境注意事項」，已排除。因為 FinMind 免費版配額限制（同一整點最多 ~550-600 次呼叫），
  全量回填分兩階段：先追法人資料到最新（~2095 檔，每檔 1 次呼叫），再回填融資融券
  （~2095 檔 × 從 2015 年起的全部歷史，每檔 1 次呼叫）。
- **驗收結果**（真實查詢，最後查詢時間點的數字，margin_trading 仍在回填中）：
  - `institutional_investors` distinct stock_id ✅ **通過**（2056 ≥ 1500）
  - `market_index` TAIEX 覆蓋 2015-2026 ✅ **通過**（2015-01-05~2026-08-17，2831 筆）
  - 法人資料「追到最新（8月）」進度：1221 / ~2095 檔（即時查詢值，持續增加中）
  - `margin_trading` 覆蓋 ≥1500 檔 ⏳ **PENDING - 待真實資料補齊**：目前僅 1 檔（小規模測試留下的
    2330），全量回填要等法人追趕階段先跑完才會開始。完成後回來更新這裡的真實數字。

### Task 2 細節（因子庫模組化重構）—— 最新狀態

- **2a strategy/factors/**：已建立 `base.py`（`cross_zscore`、`winsorize`）、
  `style.py`（`momentum_52w`、`value_composite`、`low_vol_ivol`）、
  `taiwan.py`（`inst_flow`、`margin_usage`、`margin_squeeze_market`、`quality` 佔位）。
  統一簽名 `def factor_name(data: dict) -> pd.DataFrame`。
- **行為變更（不是單純搬程式碼，這裡誠實列出）**：
  - `value_composite`：原本 v5 只用 1/PER；照 Task 2b 規格改成
    `0.5×(1/PER) + 0.5×(1/PBR)`。
  - `inst_flow`：原本 v5 是「60 日累積買超原始金額」（未正規化）；照 Task 2b 規格改成
    `60 日累積淨買超 / 60 日平均成交金額`（正規化，避免大型股買超金額天生較大而系統性勝出）。
- **2b margin_usage / margin_squeeze_market**：照規格實作（融資 20 日變化率取負號；
  squeeze_ratio = 融資回檔幅度 / TAIEX 回檔幅度，大盤回檔 <3% 回傳 NaN）。
- **2c quant_layer2.py**：`load_matrices()` 新增讀取 `margin_trading`／`market_index`；
  `build_factors()` 改呼叫 `factors` 模組而非 inline 計算；複合權重的 fallback 預設值
  改成 Task 2c 規格（momentum 0.15 / value 0.20 / rev_yoy 0.20 / low_vol 0.15 /
  inst_flow 0.30），現有的 `mom_120`／`div_yld` 補充因子保留在 `factor_map` 裡但
  fallback 權重設為 0（不稀釋規格權重，資料充足時仍會透過動態 IC 加權機制自然拿到權重）。
  `margin_usage`／`quality` 算出來但**不放進** `factor_map`（複合），照 2c 規格留給 Task 5。
- **2d tests/test_factors.py**：11 項測試全過（見下方驗收結果）。

### Task 2 驗收結果（逐項對照，全部為真實查詢/真實計算，無估計值）

| 驗收項目 | 結果 | 數字 |
|---|---|---|
| `python run.py --step 2` 跑通 | ✅ 通過 | 無錯誤，`reports/equity_curve.csv` 正常產出 |
| `pytest` 全過 | ✅ 通過 | `tests/test_factors.py`：11 passed |
| inst_flow IC > 0.03 | ❌ **未通過** | 實測 IC 均值 = **0.0034**（ICIR 0.037，IC>0 比例 52.1%），來自
  `python run.py --step 2` 真實資料的因子診斷報告，不是估計值 |

**inst_flow IC 未過關的診斷**（沒有跳過，照你的規則先診斷）：另外寫了一段對照測試，
分別算「正規化前」（v5 舊版原始金額）與「正規化後」（Task 2b 規格）在全樣本（無流動性篩選）
下的 IC，兩者都在 0 附近（分別 -0.0003 與 -0.0024），代表**正規化本身不是造成 IC 偏低的原因**——
兩個版本表現相近，都偏弱。目前的假設是這個因子的 60 日累積買超訊號，用簡單 Spearman IC
（20 日前瞻報酬、逐日重疊窗口）量測，本來就沒有很強的線性/單調預測力，不是實作 bug。
**我沒有調整因子定義或篩選方式去湊過 0.03**——那樣做等於在用同一份資料反覆調參數直到通過，
本身就是這個專案在打擊的那種資料窺探偏誤。這個結果照實記錄，需要你決定下一步（例如：接受這個
誠實的負面結果、換一種評估方式如月度 IC、或在 Task 5 用 ML 版本重新評估非線性關係）。

margin_usage 的真實 IC 因為 `margin_trading` 還在回填，標記 **PENDING - 待真實資料補齊**，
回填完成後會補跑並附上真實數字，不會用估計值先填。

### Task 3–4 細節（機制偵測 + 整合）

`regime/` 資料夾完全不存在，`indicators.py` / `health_score.py` / `hmm_detector.py` /
`ml_alert.py` / `regime_engine.py` / `plot_regimes.py` 全部未開始。`quant_layer2.py` 裡
沒有 `REGIME_FACTOR_WEIGHTS`、沒有機制曝險邏輯。**這是任務書裡「核心」的部分，目前是空白。**

### Task 5 細節（ML 因子合成）

`strategy/ml_composite.py` 不存在。但 `strategy/quant_pure_ml.py`（M1：完全 ML-driven 策略）
已經有 LightGBM 相關邏輯，設計哲學上與 Task 5 有重疊（都是拿 ML 做因子/報酬預測），但欄位、
walk-forward 訓練切法、SHAP 輸出都不符合 Task 5 的規格。值得回收利用，但不能直接算完成。

### Task 6 細節（驗證框架）

`strategy/walk_forward.py`（Layer 2 舊版）與 `strategy/walk_forward_l3.py`（Layer 3 anchored
版）都存在，且 `reports/` 裡已有 `walk_forward_l3.json` / `walk_forward_l3.md` 的輸出，但：
- 沒有依照 Task 6a 規格「訓練 [2015,y-1] → 測試 [y]，y=2020..2025 共 6 fold」重新產出
  `reports/walk_forward_results.md/.json`
- `strategy/ablation.py`、`strategy/significance.py`、`strategy/attribution.py` 都不存在
- 對應的 `ablation_results.md`、`significance_report.md`、`attribution_report.md` 都沒有

### Task 7 細節（研究誠信模組）

`data_pipeline/survivorship.py`、`regime/decay_monitor.py`、`strategy/capacity.py` 都不存在，
對應報告 `survivorship_analysis.md`、`factor_decay_history.png`、`capacity_analysis.md` 也都沒有。

### Task 8 細節（自動化整合）——⚠️ 目前處於壞掉狀態

- `automation/daily_update.py` 有 Step 1a（價格/估值增量）與 Step 1b（法人資料增量），但**沒有**
  融資融券、TAIEX 的增量更新，也**沒有**機制訊號寫入 `signals/*.json` 的 `"regime"` 欄位
- `automation/notifier.py` 只有 LINE + Notion，機制降級警示邏輯不存在
- **發現一個會擋住整支腳本執行的 bug**：`automation/daily_update.py` 第 493 行在
  `if __name__ == "__main__":` 區塊裡直接寫 `return`，但這裡不在任何函式內 ——
  這是 Python 的 `SyntaxError`（`'return' outside function`），代表**目前
  `python automation/daily_update.py` 完全無法執行，連 import 都會失敗**。細節見下方「問題清單」。
- `requirements1.txt` 缺 `hmmlearn`、`matplotlib`、`shap`、`pytest`

### Task 9 細節（文件與發布）

`README.md` 存在，但 Overview 直接寫「live trading via Discord bot」，整份文件是圍繞 Discord bot
（`/signals /actions /run_model`）敘事的操作手冊，跟 Task 9 要求的「MFE 作品集等級、含 Regime
方法論 / OOS 表 / Ablation 表 / DSR / Attribution / Limitations / 15+ 篇文獻引用」的學術報告
格式完全是兩回事，屬於整份重寫（不是修訂）。

---

## 2. 現有檔案清單與角色

### 根目錄

| 檔案 | 角色 |
|---|---|
| `run.py` | 主執行入口（`--step 1/1b/2/3`、`--stats`、`--verify`） |
| `bot.py` | Discord bot（本次工作 2 已隔離移出，見下） |
| `predict_model.py` | 目前實際在用的訊號產生器（N1 v2 ML 策略），被 `bot.py` 的 `/run_model` 呼叫，也可獨立執行 |
| `main.py` | 空檔案（0 bytes） |
| `config.json` | `predict_model.py` 與 `bot.py` 共用設定檔（帳戶資金、Discord 頻道 ID） |
| `portfolio.json` | 目前持倉快照，`predict_model.py` 讀寫用於算買賣動作 |
| `run_experiments.py` | 實驗批次執行腳本 |
| `run_1b.sh` | Step 1b 便捷啟動腳本（含 venv 啟動） |
| `test_tej.py` | TEJ API 連線測試小工具 |
| `README.md` | 專案說明（現況以 Discord bot 為核心，Task 9 待重寫） |
| `TRADINGVIEW_README.md` / `taiwan_multi_factor_strategy.pine` | TradingView Pine Script 版本（獨立分支，非 Python pipeline 一部分） |
| `requirements1.txt` | Python 依賴清單 |
| `CLAUDE.md` | 給 Claude Code 的專案指引 |
| `DEVELOPMENT_PROMPT.md.pdf` / `CLAUDE_CODE_TASKS.md.pdf` | 兩份任務規劃文件 |

### `data_pipeline/`

| 檔案 | 角色 |
|---|---|
| `schema.py` | 資料表 DDL + 清洗規則定義（單一事實來源） |
| `cleaner.py` | 依 `schema.py` 規則清洗原始資料 |
| `loader.py` | CSV → SQLite 載入器，含 `read_merged()` 標準讀取介面 |
| `download_institutional.py` | FinMind 三大法人買賣超下載（斷點續傳/速率控制/重試已完成） |
| `download_supplementary.py` | **新增（Task 1）**：FinMind 融資融券 + TAIEX 下載（固定節奏速率控制、斷點續傳） |
| `fetch_stock_names.py` | 抓股票代號↔名稱對照表 |
| `update_prices_fugle.py` | Fugle API 增量股價更新（FinMind 的替代來源，任務文件未提及） |

### `strategy/`

| 檔案 | 角色 |
|---|---|
| `quant_layer2.py` | 多因子回測引擎（任務書鎖定的目標檔案；已內建動態 IC 加權，因子邏輯已模組化到 `factors/`） |
| `factors/base.py` | **新增（Task 2）**：`cross_zscore`、`winsorize` 共用工具 |
| `factors/style.py` | **新增（Task 2）**：`momentum_52w`、`value_composite`、`low_vol_ivol` |
| `factors/taiwan.py` | **新增（Task 2）**：`inst_flow`、`margin_usage`、`margin_squeeze_market`、`quality`（佔位） |
| `quant_layer3.py` | Layer 3 重新設計版（價值+品質因子+風險平價，任務書未提及） |
| `quant_pure_ml.py` | 純 ML-driven 策略 M1（任務書未提及） |
| `cta_module.py` | CTA 趨勢追蹤策略 I（任務書未提及） |
| `pead_module.py` | PEAD 事件驅動策略 J（任務書未提及） |
| `pairs_module.py` | 配對交易策略 K（任務書未提及） |
| `pairing_analyzer.py` | 策略搭配效果分析工具 |
| `portfolio_combiner.py` | 多 sleeve 組合配置器 N2 |
| `walk_forward.py` | Layer 2 專用 walk-forward 驗證（舊版，非 Task 6 規格） |
| `walk_forward_l3.py` | Layer 3 專用 anchored walk-forward 驗證 |

### `tests/`

| 檔案 | 角色 |
|---|---|
| `test_factors.py` | **新增（Task 2）**：`factors/` 模組的單元測試（合成資料，11 項全過） |

### `automation/`

| 檔案 | 角色 |
|---|---|
| `daily_update.py` | 每日增量更新（⚠️ 含 SyntaxError，目前無法執行；Step 2-4 舊訊號邏輯已停用） |
| `notifier.py` | LINE + Notion 通知（乾淨，無 Discord 程式碼） |

### `analysis/`（研究過程的探索工具，非正式 pipeline）

`cost_sensitivity.py`、`liquidity_sensitivity.py`、`diagnose.py`、`diag_industry.py`、
`o2_ablation.py`、`o2_combinations.py`、`sweep_top2_weights.py`、`n1_pead_sweep.py`、
`run_pairing.py`、`show_holdings.py`、`verify_inst_flow.py`、`debug_timing.py`

### `docs/`

`DAILY_AUTOMATION_GUIDE.md`、`INSTITUTIONAL_DOWNLOAD_GUIDE.md` — 既有操作文件。

### `.github/workflows/daily_quant.yml` 與 `github/workflows/daily_quant.yml`

內容相同；`.github/` 版本是實際被 GitHub Actions 讀取的，`github/`（無點）版本被 `.gitignore`
排除、屬本地備份，不衝突。

---

## 3. 發現的問題清單

1. ~~**【阻斷性】`automation/daily_update.py:493` SyntaxError**~~ **已修復**——
   `if __name__ == "__main__":` 區塊內的 `return` outside function 已移除（連同它守護的死碼），
   `python -m py_compile` 確認語法通過。

2. ~~**schema.py 缺表**~~ **已修復**——`margin_trading`、`market_index` 已加入 `TABLE_DDL`。

3. **死程式碼** — `daily_update.py` 第 495–519 行（註解寫「以下是舊邏輯，已停用」）在
   第 493 行的 `return`（若修正後）之後永遠不會執行，屬於明確的死碼，應清理或改成真正的
   opt-in 分支而不是留著。

4. ~~**requirements1.txt 缺依賴**~~ **部分修復**——`matplotlib`、`pytest` 已補進
   `requirements1.txt`（Task 2 期間發現這個問題：venv 被整個重建後，這兩個套件連同其他
   非 `requirements1.txt` 套件全部消失，見上方「環境注意事項」第三輪）。仍未列（照原計畫
   留到 Task 8 才補）：`hmmlearn`、`shap`。`bot.py` 用到的 `discord.py` 也還沒補回去，
   因為 `bot.py` 目前停用中，見 `_disabled/discord/README_RESTORE.md`。

5. **`.env` 實際變數與兩份任務文件的假設不符** — 任務文件都寫「`.env` 已存在
   `FINMIND_TOKEN / LINE_NOTIFY_TOKEN / NOTION_TOKEN / NOTION_DATABASE_ID`」，但實際 `.env`
   只有 `DISCORD_TOKEN / ADMIN_ID / NOTIFICATION_CHANNEL_ID / FINMIND_TOKEN / FUGLE_API_KEY`。
   `LINE_NOTIFY_TOKEN`、`NOTION_TOKEN`、`NOTION_DATABASE_ID` 目前**都沒有設定**。程式碼有做
   graceful degradation（`notifier.py` 缺 token 時會印警告並跳過，不會 crash），但代表就算
   `daily_update.py` 的 SyntaxError 修好，LINE/Notion 通知現在也發不出去 —— 這是 Task 8
   驗收「LINE 收到通知」的另一個獨立阻礙，需要你之後補上這三個環境變數。

6. **`quant_layer2.py` 的 `inst_flow` 權重（0.24，動態 IC 加權下的預設值）與 Task 2c
   指定的固定權重（0.30）不一致** —— 不是 bug，是既有設計已經比任務書規格更進階（動態 vs
   靜態），但如果之後要照抄任務書規格重構，需要決定要不要保留現在的動態加權機制。

7. **任務文件基準過時**（詳見本文件開頭第 0 節）—— 這不是程式碼問題，但會實質影響 Task 2、4、5
   怎麼做，建議在動工前跟你確認清楚。

---

## 4. 建議的執行起點

- **Task 1 可以直接開始**，且工作量比任務書描述的小：法人資料已達標，只需要新增
  `margin_trading` / `market_index` 兩張表 + 擴充下載邏輯（融資融券 + TAIEX），不用整個重寫。
- **在動 Task 2 之前，強烈建議先解決第 0 節的範疇問題**（機制偵測要接 `quant_layer2.py` 還是
  更成熟的 Layer 3/N 系列），否則 Task 2 的模組化重構可能做錯目標檔案。
- **`automation/daily_update.py` 的 SyntaxError 建議盡快修掉**，不一定要現在做（不在 Task 1
  範圍內），但它會持續擋住任何「本機跑通 daily_update.py」的驗收，越晚修代價不變、只是一直卡著。
  是否現在順手修掉這一行，還是留到 Task 8 一起處理，請你決定。
- **LINE_NOTIFY_TOKEN / NOTION_TOKEN / NOTION_DATABASE_ID 需要你另外申請並補進 `.env`**，
  這不是我能代勞的部分。
- Task 3（機制偵測）是任務書裡最重的一塊，且完全空白，一旦 Task 1/2 的資料與因子基礎確定，
  應該是下一個重點。
