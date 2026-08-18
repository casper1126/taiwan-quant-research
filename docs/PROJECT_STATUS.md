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
| 1 | 資料補完 | **✅ 全部三項驗收通過**（institutional 2056 檔 / margin_trading 1957 檔 / TAIEX 完整覆蓋，見 §5 事故報告的重啟結果） | 見下方細節 |
| 2 | 因子庫模組化重構 | **程式碼、測試、無回歸全部完成；⚠️ inst_flow（0.0034）與 margin_usage（-0.0039）兩項 IC 驗收都未過 0.03 門檻，誠實記錄，等你決定如何處理** | 見下方細節 |
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
- **全量回填 — 事故紀錄與根因診斷（你指出問題後重新調查，更正先前的錯誤判斷）**：

  先前這裡寫「process 被中斷、不是程式碼問題」，**這個判斷是錯的**，重新診斷後更正如下：

  1. **確認一個嚴重 bug**：`download_supplementary.py` 的 `_fetch()` 對「FinMind 回傳 200
     但資料是空的（真的沒有資料）」和「重試 3 次後 402 依然失敗」回傳**同一種結果**（空
     DataFrame）。`download_margin_one()` 拿到空 DataFrame 一律標記 `status="no_data"`，
     而外層迴圈把 `no_data` 跟 `up_to_date` 一起算進 ⊘ skip，不會進 ✗ fail。結果就是
     **402 配額用盡被靜默吞成「正常跳過」**，log 完全看不出實際上什麼都沒抓到。
  2. **402 的真正原因**：不是 `TaiwanStockMarginPurchaseShortSale` 需要付費方案——查證過
     FinMind 官方文件，這個 dataset 用「單一股票代號查詢」（我們的用法）屬於免費方案，
     只有「不指定股票、一次抓全市場某一天」才需要 Backer/Sponsor。也不是這次的
     `FINMIND_TOKEN` 被降級或有問題——用同一個 token、對照剛才在 402 期間失敗的股票代號
     即時重打 API，全部正常回傳 200（見下方即時驗證）。真正原因是**配額被同時跑的多個
     process 瓜分**：這個免費/註冊 token 帳號等級的上限是 600 次/小時，`run.py --step 1b`
     用的動態限速器與 `download_supplementary.py` 用的固定節奏限速器**都只在自己的 process
     記憶體裡計數**，互相不知道對方的存在——今天稍早已經因為這個原因撞過一次雙 process
     衝突（見「環境注意事項」），這次的「take2」重啟又是同一個模式：在法人追趕階段可能還沒
     完全把配額還給系統、或別的 process 仍在使用同一個 token 的情況下，直接又啟動了一個新的
     融資融券下載，一啟動就立刻連環撞 402（`logs/margin_backfill_take2.log` 顯示幾乎每一檔
     從第 40 檔左右開始就 402）。**「take2」這個重啟本身就是問題的一部分，不是修復**——
     而且它每檔重試 3 次、每次都在配額耗盡時硬等，等於在已經沒有配額的狀況下持續消耗僅剩的
     配額，讓恢復更慢。已經確認並終止這個背景 process（PID 70553）。
  3. **已寫入的 91 檔資料是否可信**：抽查過（含最舊掛牌股與最新掛牌股），每一檔的日期範圍
     都完整、連續、跟股票的真實掛牌/下市時間吻合（例如 1294/1295 只從 2025 年底開始是因為
     新股上市，1262 只到 2020-09-24 是該股當時的真實下市日），**不是截斷或損毀的資料，
     不需要回滾**。但有一個重要風險要記錄：`margin_squeeze_market()`（Task 3 機制偵測會用）
     是把全市場的 `margin_balance` **加總**成一個全市場數字——這種「加總」在只有 91/2095
     （約 4%）覆蓋率時，不會像個股因子一樣自然變成 NaN，而是會**安靜地算出一個嚴重低估、
     看起來卻很正常的數字**。Task 3 開工前必須先確認 `margin_trading` 覆蓋率夠高，
     不能在目前這種低覆蓋率狀態下就拿去用。
  4. **即時驗證（診斷用，未寫入資料庫）**：用同一個 `FINMIND_TOKEN` 對 402 期間失敗過的
     `1107`／`1304`／`1500`／`1600` 即時重打 API，全部 HTTP 200（`1107`／`1500`／`1600`
     其實是真的沒有融資融券資料，例如可能是 ETF 或當時未開放信用交易的股票；`1304` 有正常
     資料）。這證實配額現在是通的，402 是暫時性的配額衝突，不是永久性限制。

- **最終驗收結果（bug 修復 + 重啟 backfill 之後，真實查詢，2026-08-17 23:xx 完成）**：
  - `institutional_investors` distinct stock_id ✅ **通過**（2056 ≥ 1500）
  - `institutional_investors` 資料新鮮度 ✅ **1921/2056（93.4%）真的追到 2026-08**；
    剩餘 135 檔複查了它們各自的 `MAX(date)`，分布在 2015~2020 年間（例如 2833 停在
    2015-10-01、2361 停在 2015-12-09），**不是這次的 bug 造成**——時間點分散在好幾年前，
    不是集中在「上次回填中斷的那個時間點附近」，判斷是這些股票已下市/併購，FinMind 本來
    就沒有更新的資料，屬於合法的資料邊界，不是 402 誤判的殘留
  - `market_index` TAIEX 覆蓋 2015-2026 ✅ **通過**（2015-01-05~2026-08-17，2831 筆）
  - `margin_trading` 覆蓋 ≥1500 檔 ✅ **通過**（**1957** 檔，總筆數 **4,489,452**，
    遠超過 1500 門檻；耗時約 4 小時 38 分，過程中真的觸發過一次 hibernate——log 顯示
    22:35 額度用盡、23:00 自動醒來從 checkpoint 繼續，全程無人工介入）

**Task 1 三項驗收全部通過。**

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
| `python run.py --step 2` 跑通 | ✅ 通過 | 無錯誤，`reports/equity_curve.csv` 正常產出，backfill 前後結果一致（82.4% 總報酬／Sharpe 0.34），無回歸 |
| `pytest` 全過 | ✅ 通過 | 22 passed（`test_factors.py` 11、`test_download_lock.py` 6、`test_download_supplementary.py` 5） |
| inst_flow IC > 0.03 | ❌ **未通過** | 實測 IC 均值 = **0.0034**（ICIR 0.037，IC>0 比例 52.1%） |
| margin_usage IC > 0.03（任務書沒有明訂這項，但既然算得出來就一併誠實列上） | ❌ **未通過** | 實測 IC 均值 = **-0.0039**（ICIR -0.047，IC>0 比例 47.9%），用完整回填後的 1957 檔真實 margin_trading 資料算出，不是估計值 |

兩項都來自 `python run.py --step 2` 真實資料的因子診斷報告，backfill 完成後重跑確認。

**診斷（沒有跳過，照你的規則先診斷）**：另外寫了一段對照測試，分別算 inst_flow「正規化前」
（v5 舊版原始金額）與「正規化後」（Task 2b 規格）在全樣本（無流動性篩選）下的 IC，兩者都在
0 附近（分別 -0.0003 與 -0.0024），代表**正規化本身不是造成 IC 偏低的原因**，兩個版本表現
相近，都偏弱。margin_usage 現在也一樣：IC 幾乎是 0（甚至略負），沒有隨資料補齊而出現預期中的
訊號。目前的判斷是這兩個因子的定義（60 日累積買超、20 日融資變化率），用簡單 Spearman IC
（20 日前瞻報酬、逐日重疊窗口）量測，本來就沒有很強的線性/單調預測力——**不是實作 bug**，
也不是資料不足造成的（margin_usage 現在已經用近乎完整的真實資料算過）。**我沒有調整因子定義
或篩選方式去湊過 0.03**——那樣做等於在用同一份資料反覆調參數直到通過，本身就是這個專案在
打擊的那種資料窺探偏誤。兩個誠實的負面結果都照實記錄，需要你決定下一步（例如：接受這兩個
因子目前不具備線性/單調的獨立預測力、換一種評估方式如月度 IC、或留給 Task 5 的 ML 版本重新
評估非線性/交互作用關係）。

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

8. ~~**【嚴重】402 配額用盡被誤判為「無資料」跳過**~~ **已修復**——完整根因與修法見下方
   「§5 402 事故 — 根因、修法、驗證」。`download_institutional.py` 有完全一樣的 bug，
   也一併修了（複查發現「institutional 已更新到 2026-08-17」的說法也是錯的，見 §5）。

---

## 5. 402 事故 — 根因、修法、驗證

（本節記錄一次真實事故的完整處理過程：發現 → 根因診斷 → 修法 → 驗證 → 重啟，
供之後遇到類似問題時參考，也是誠實記錄「中間一度診斷錯誤」的過程，不是事後美化。）

### 現況確認（你要求先如實回答，不要假設）

**Q1：連續撞到多次 402，程式的行為？** 會一路撞到底，跑完全部 2095 檔清單才結束，
沒有提早停止機制。

**Q2：process 是直接結束還是會等待重試？** 正常結束（不是 crash），但不會等待恢復、
不會自動重試——因為下面這個 bug，原本設計的「等待恢復」邏輯根本沒被執行到。

### 根因

`_fetch()` 裡 `resp.raise_for_status()` 在檢查 JSON `status` 欄位**之前**就先對 HTTP
402 拋出 `HTTPError`（FinMind 對配額用盡回傳的是真正的 HTTP 402 狀態碼，不是「HTTP 200 +
JSON 裡包 status:402」）。原本寫的「偵測到 402 就休眠等待」那段邏輯因此**從未被執行過，
是死碼**；實際執行的是 generic 的 `except requests.exceptions.HTTPError`，只做幾秒鐘的
指數退避重試，3 次都失敗就放棄、把空 DataFrame 回傳給呼叫端，被誤判成 `"no_data"`（正常
跳過），跟「FinMind 明確回應真的沒有資料」混在一起，統計數字完全看不出實際上發生了什麼。
`download_institutional.py` 有一模一樣的 bug。

**402 本身不是付費方案限制**：查證過 FinMind 官方文件，`TaiwanStockMarginPurchaseShortSale`
用單一股票代號查詢（我們的用法）屬免費方案；且對 402 期間失敗過的股票代號即時重打 API 全部
正常回傳 200。真正原因是**配額被同時執行的多個 process 瓜分**——這個帳號等級上限
600 次/小時，但兩支下載器的限速器都只在自己的 process 記憶體裡計數，互相看不到對方。

**複查 `download_institutional.py` 的結論（你要求的第 4 點）**：「2056 檔已更新到
2026-08-17」的說法**是錯的**。2056 這個數字本身沒錯（DB 裡真的有這麼多檔股票的資料，
`COUNT(DISTINCT stock_id)` 是直接查詢，不受這個 bug 影響），但實際查詢每檔股票的
`MAX(date)`，只有 **1271/2056（62%）真的追到 8 月，其餘 768 檔（37%）卡在 2026-05 或
更早**——跟 margin_trading 是同一種 bug 造成的同一種災情，只是沒那麼極端（91 vs 1271，
差異推測是 institutional 的動態限速器本來就比 supplementary 的固定節奏更保守，加上
institutional 每天都在增量更新、缺口比從零開始的 margin backfill 小）。

### 修法（三項都已完成）

1. **互斥鎖**（`data_pipeline/download_lock.py`）：lock file 寫 PID + 啟動時間 + 腳本
   名稱，`os.kill(pid, 0)` 判斷是否存活，死掉的 PID（stale lock）自動清除。
   `download_institutional.py`、`download_supplementary.py` 的 `download_all()` /
   `download_all_supplementary()` 都用 `with download_lock(...)` 包住整個下載流程。
   （`run.py --step 1` 沒有加鎖：它只是把本機 CSV 載進 SQLite，不打 FinMind API，
   不會有配額衝突問題，加鎖沒有意義。）
2. **402 判定修正**：改成直接檢查 `resp.status_code == 402`，在
   `resp.raise_for_status()` 之前，用專屬的 `QuotaExhaustedError`（定義在共用模組
   `data_pipeline/finmind_common.py`，兩支下載器一起用，不再各自維護一份不同步的邏輯）
   往上傳，跟「200 但真的沒資料」的空 DataFrame 明確分開。**確認這行判斷式真的會被執行到
   （不是第二個死碼）**：是純屬性檢查（`if resp.status_code == 402:`），不依賴任何
   exception 的觸發時機，寫在 `resp.raise_for_status()` 呼叫之前的第一行，兩支下載器
   分別在 `download_supplementary.py:197`、`download_institutional.py:283`；
   `tests/test_download_supplementary.py::test_fetch_raises_quota_exhausted_on_http_402`
   直接對這行斷言，pytest 通過即證明它有被執行到。
3. **休眠 → 自動恢復，不再傻等重試**：402 不重試，直接記錄 checkpoint、計算距離下個
   時間窗口還有多久、`sleep` 到那個時間點自動醒來從同一檔繼續，期間每 10 分鐘印一次心跳
   log。連續 3 個時間窗口醒來後立刻又 402，才視為異常，正常結束 process 並提示
   「執行 `python run.py --step 1b` 可從 checkpoint 續跑」。
   **範疇限制（誠實列出）**：`download_institutional.py` 支援 `--workers` 並行下載，
   多執行緒協調休眠比順序模式複雜很多；目前並行模式只做了正確性修復（402 不會再被
   誤判成 skip）+ 簡易 circuit breaker（配額用盡比例 >30% 提早中止），**沒有**完整的
   自動休眠/恢復。無人值守的長時間背景回填請用 `workers=1`（順序模式，兩支下載器現在
   都是這個模式，也是目前唯一實際會用到的模式）。

### 驗證

- **pytest 全部 22 項通過**（`tests/test_download_lock.py` 6 項、
  `tests/test_download_supplementary.py` 5 項、`tests/test_factors.py` 11 項），
  包含：
  - 用縮小的時間窗口（2 秒）模擬完整的「撞 402 → 休眠 → 自動恢復 → 從 checkpoint
    繼續」流程，驗證 402 的那一檔最終真的抓到資料，不是被跳過（不用真的等一小時）
  - circuit breaker：全程都是 402 時，確認最多重試 `MAX_QUOTA_RETRY_CYCLES=3` 次後
    正常結束、不寫入任何資料、不會無限循環
- **lock file 手動實測**（真的啟動兩個獨立 process，不是 mock）：process A 取得鎖後
  持有 6 秒，process B 在這期間嘗試取得鎖，**立刻被拒絕**（exit code 1，印出
  A 的 PID/啟動時間/腳本名稱），process A 結束後 lock file 正確清除。
- **`.gitignore` 補上 `data/.download_lock`（runtime 狀態，不該進版控）**，順便補上
  之前漏掉的 `data/*.db-shm`、`data/*.db-wal`。

### 重啟結果（完成）

`python run.py --step 1b` 用修好的程式碼跑完（單一 process，耗時約 4 小時 51 分鐘，
16:23 開始、23:07 結束）：institutional 階段新增 659 檔真的追到最新（失敗 0），
margin_trading 階段新增 1866 檔（失敗 0，跳過 229 檔含真無資料），過程中真的觸發過一次
配額用盡 → 自動休眠 25 分鐘 → 整點自動恢復，全程無人工介入。最終數字見上方 Task 1 細節，
三項驗收全部通過。

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
