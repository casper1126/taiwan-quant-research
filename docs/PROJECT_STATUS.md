# PROJECT_STATUS.md

盤點日期基準：以當前 repo 實際檔案內容為準（非任務文件描述）。
比對對象：`CLAUDE_CODE_TASKS.md`（Task 1-9 內容逐字轉錄自
`CLAUDE_CODE_TASKS.md.pdf`，另外加了規則 9-11，見該檔案開頭說明）。

**最後更新：2026-08-23（Task 1-6 完成後）**——本文件前半段（§0、環境注意
事項、Task 1/2 細節、402 事故報告）是 2026-08-17～18 寫的，反映當時的
狀態；Task 3、4、5、6 完成後的最新狀態記錄在下方「Task 1–9 完成度總表」與
新增的 Task 3／Task 4／Task 5／Task 6 細節小節裡。決策過程的完整記錄在
[docs/DECISIONS.md](DECISIONS.md)，這裡只總結結論。

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

**（已解決，2026-08-17）**：確認 Task 3-4 的機制偵測接到任務書指定的
`quant_layer2.py`，不接 Layer 3 / N1 / N2 組合（那些分支明確排除在這次
Task 1-9 的範圍外，維持獨立、不整合）。Task 3 的 `regime/` 模組刻意設計成
單向依賴：只被 `quant_layer2.py` 呼叫，不 import `quant_layer2.py` 或
任何 N1/N2/`predict_model.py` 的東西，這個決定至今沒有改變。

---

## 1. Task 1–9 完成度總表

| Task | 標題 | 完成度 | 判斷依據 |
|---|---|---|---|
| 1 | 資料補完 | **✅ 全部三項驗收通過**（institutional 2056 檔 / margin_trading 1957 檔 / TAIEX 完整覆蓋，見 §5 事故報告的重啟結果） | 見下方細節 |
| 2 | 因子庫模組化重構 | **✅ 完成**（程式碼/測試/無回歸全部完成；inst_flow、margin_usage 因子 IC 排查後確認無線性預測力，已正式移出主策略複合，見「決定 C」，不是待決事項） | 見下方細節 |
| 3 | 機制偵測模組 | **✅ 完成**（`regime/` 6 個檔案全部建立；8 項驗收 6 項通過，2 項確認為系統誠實特性，非 bug） | 見下方「Task 3 細節」 |
| 4 | 機制整合到策略 | **✅ 完成**（`REGIME_FACTOR_WEIGHTS`／曝險整合／`strategy/portfolio.py` 全部完成；3 項驗收全過；發現機制版績效遠低於基準版，已記錄非阻斷） | 見下方「Task 4 細節」 |
| 5 | ML 因子合成 | **✅ 完成**（`strategy/ml_composite.py` 全部建立；9 個 walk-forward fold 全部訓練成功；SHAP／逐年 importance／因子穩定性分析三份報告全部真實產出） | 見下方「Task 5 細節」 |
| 6 | 驗證框架 | **✅ 完成**（Walk-Forward／Ablation A-D／統計顯著性／績效歸因全部真實跑完；3/4 驗收項目通過，1 項延續 Task 4 已知取捨誠實未通過） | 見下方「Task 6 細節」 |
| 7 | 研究誠信模組 | **✅ 完成**（三份報告/圖全部真實產出；survivorship 含量化估計，且誠實揭露了「無法精確量化」這個發現本身） | 見下方「Task 7 細節」 |
| 8 | 自動化整合 | **部分完成，且目前壞掉** | 見下方（`daily_update.py` 有 SyntaxError） |
| 9 | 文件與發布 | **未開始** | README 現況以 Discord bot 為敘事核心，非作品集規格 |

**整體完成度：7/9 Task（約 78%）**

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
打擊的那種資料窺探偏誤。

**（後續更新，2026-08-18，已決定，不再是待決事項）**：又補做了三輪排查（SQL 逐筆核對真實
API、5/10 日短窗口重測、分年份 2015-2026 IC 拆解），結論一致：兩個因子在任何窗口、任何年份
都沒有穩定訊號，確認是誠實的負面結果。**選擇了選項 C**：`inst_flow` 正式移出
`quant_layer2.py` 的主策略複合權重（`margin_usage` 本來就沒進複合，維持排除），剩餘四個
核心因子（momentum/value/rev_yoy/low_vol）依實測 IC 相對強弱重新正規化權重
（momentum 0.34／value 0.18／rev_yoy 0.18／low_vol 0.30）。兩個因子的計算邏輯與資料表都
完整保留，移交 Task 5 的 LightGBM 版本重新評估是否有非線性/條件式訊號。移除後回測績效不降
反升（總報酬 82.4%→89.1%，Sharpe 0.34→0.38）。正式研究發現報告見
`reports/factor_negative_findings.md`，決策過程見 `docs/DECISIONS.md`
（「inst_flow／margin_usage IC 偏低」那筆）。

### Task 3 細節（機制偵測模組）—— ✅ 完成（2026-08-21）

`regime/` 模組全部建立：`data_loader.py`（獨立讀取 SQLite，不依賴 `quant_layer2.py`，維持
上方「單向依賴」的決定）、`indicators.py`（11 個指標）、`health_score.py`（0-100 健康分數，
六項成分：波動18/寬度22/高低差13/相關性13/不對稱13/squeeze12 + capitulation 加分 9）、
`hmm_detector.py`（GaussianHMM 3 狀態，expanding window 每月重 fit，504 天最小歷史）、
`ml_alert.py`（LightGBM 崩盤預警，walk-forward 每年重訓）、`regime_engine.py`（分類規則 +
遲滯整合）、`plot_regimes.py`（`reports/regime_history.png`）。`tests/test_regime.py`
11 項測試全過，含任務書要求的 HMM 無 look-ahead 斷言。

**資料範圍延伸（2026-08-21 執行）**：原本用真實 API 查證發現三大法人資料的真實可得起點是
**2012-05-02**（不是原本以為的 2005-01-01，這個更正記錄在
[docs/DATA_AVAILABILITY.md](DATA_AVAILABILITY.md)），經你批准後執行延伸：
`institutional_investors`／`margin_trading`／`market_index` 三張表全部往回補到
2012-05-02（單一 process、沿用 Task 1 的鎖檔+額度自動休眠機制，觸發 5 次配額耗盡休眠、
全部自動恢復、零失敗，耗時約 10.3 小時）。`data_pipeline/download_institutional.py`／
`download_supplementary.py` 新增 `--backfill-to` 模式支援這種「只補既有資料更早的區間」
的下載型態。

**驗收結果（8 項，2026-08-21 最終跑，唯一一次完整驗收）**：

| 項目 | 標準 | 結果 | 通過 |
|---|---|---|---|
| 2020/02-03 WARNING/BEAR | ≥15 天 | 41/41 天 | ✅ |
| 2022 WARNING+BEAR | >80 天 | 246 天 | ✅ |
| 2026/04、2026/07 大跌標記 | WARNING/BEAR | 全部標記 | ✅ |
| 2026/07 底 squeeze_ratio | 有值且 <1.0 | 0.83-0.96 | ✅ |
| 全期機制切換次數 | <40 次 | 33 次 | ✅ |
| HMM 無 look-ahead | 斷言通過 | 通過 | ✅ |
| 2016-17 BULL 比例 | >60% | 0% | ❌（誠實特性，見下方） |
| 2023-24 BULL 比例 | >60% | 5.2% | ❌（誠實特性，見下方） |

**兩項未通過的根因（已排查，不是資料或程式問題）**：切換次數原本 63 次超標，拆解後發現
只有 14% 是無意義邊界抖動，改用不對稱 hysteresis 調整（升級確認天數 10→18 天，降級維持
3 天）解決，對危機偵測速度零影響。但這個調整讓 BULL 這個最高等級狀態變得很難連續確認滿
18 天——攤開 2012-2026 全期看，BULL 全期只出現在 1.9% 的交易日，2016-17／2023-24 只是
剛好落在這個全期都很罕見的模式裡，不是特別異常。實測證實「切換次數 <40」跟「BULL 比例
>60%」在目前設計下互斥，選擇優先滿足切換次數（更貼近系統能不能實際使用的工程需求）。
完整分析見 `docs/DECISIONS.md`（「Task 3 機制偵測」那筆）與
`reports/health_score_breadth_divergence.md`。

**存活者偏誤提醒**：資料延伸後回測可能涵蓋期間從 11 年（2015-2026）拉長為 14 年
（2012-2026），存活者偏誤曝險時間跟著拉長，Task 7 的優先度因此提高，README 的
Limitations 需要雙重警語（見 `docs/DATA_AVAILABILITY.md` 第 4 節）。

### Task 4 細節（機制整合到策略）—— ✅ 完成（2026-08-22）

`strategy/quant_layer2.py` 新增：
- `REGIME_FACTOR_WEIGHTS`：四個機制狀態（BULL/NEUTRAL/WARNING/BEAR）對應的靜態因子權重。
  任務書原始規格含 `inst_flow`（5 因子），因為 Task 2 已經把 `inst_flow` 移出主策略複合
  （見上方），這裡延續同一個決定，拿掉 `inst_flow` 欄位、剩餘四因子依原始任務書給的相對
  比例重新正規化（不是重新設計權重）。
- `build_positions()` 新增 `regime_df`／`use_regime_weights` 參數：啟用後每個再平衡日查
  當日機制，用對應權重合成因子分數（取代動態 IC 加權），並且用 `位置 = 選股結果 ×
  當日建議曝險比例`（BULL 1.0／NEUTRAL 0.7／WARNING 0.4／BEAR 0.1，再平衡日鎖定）取代
  原本的二元擇時開關。

新建 `strategy/portfolio.py`（Task 4c）：`equal_weight`／`risk_parity_weight`（從
`quant_layer2.py` 移出的共用邏輯）／`apply_buffer`（緩衝區進出場規則抽出）／
`decompose_turnover`＋`write_turnover_report`（換手率分解成換股/權重調整/擇時貢獻三類，
輸出 `reports/turnover_decomposition.md`）。`build_positions()` 新增
`weighting='equal'|'risk_parity'` 字串參數（既有的 `use_risk_parity` bool 參數仍保留，
向後相容）。`tests/test_portfolio.py` 7 項測試全過。

**驗收結果**：機制動態版跑通 ✅；risk_parity 換手 64.4% ≤ equal 版 64.9% × 1.2 = 77.9% ✅；
`reports/turnover_decomposition.md` 已產出（換股30.1%／權重調整39.8%／擇時貢獻30.1%）✅。

**重要發現（已記錄，非阻斷）**：機制動態版的績效遠低於 Task 2 的基準版——同一段回測期間
（2015-01～2026-04），基準版總報酬 92.1%／Sharpe 0.39，機制動態版只有 13.6%／Sharpe -0.11。
根因延續 Task 3 已經記錄的特性：BULL 全期只出現 1.9% 的交易日，這段回測期間台股經歷史詩級
大多頭（TAIEX 9000→47000 點），機制系統因為太保守、大部分時間只用 10-40% 曝險參與，錯過
大部分漲幅——換來的是波動度與回撤都小很多（年化波動度 2.4% vs 12.6%、MDD -7.6% vs
-24.9%）。這是風險/報酬的真實取捨，不是 bug，完整記錄在 `docs/DECISIONS.md`
（「Task 4：機制動態版績效遠低於基準版」那筆），供你之後評估是否要調整機制校準方式。

**順手修的一個小 bug**：串接過程中發現 `ml_alert.py` 的 walk-forward 會讓最新 ~20 個交易日
（還沒有未來 20 日報酬可以驗證）永遠拿不到 `crash_prob`，已修正成「訓練/AUC 評估用標籤，
預測不需要標籤」分開處理，讓即時/最新資料也能正常產出預測值，這對 Task 8 的每日自動化很重要。

### Task 5 細節（ML 因子合成）—— ✅ 完成（2026-08-22）

`strategy/ml_composite.py` 全部建立。跟 `strategy/quant_pure_ml.py`（M1，任務書未提及的
獨立研究分支）刻意分開，沒有回收共用程式碼：M1 的特徵集（30+ 個）、訓練窗口（rolling
4 年、90 天 refit）、投組建構方式都不符合 Task 5 的規格（expanding window／每年重訓／
特定的 5 因子+機制+波動分位特徵集），硬套用共用反而增加耦合風險。

**特徵**（10 欄，見 `strategy/ml_composite.py` 的 `ALL_FEATURES`）：
- 五個核心因子（`FACTOR_FEATURES`）：momentum／value／rev_yoy／low_vol／margin_usage——
  跟任務書「5 因子」對照：任務書原始規格的 5 因子是 momentum/value/rev_yoy/low_vol/
  inst_flow，Task 2 已經把 inst_flow 移出（見上方決定），這裡延續同一個決定不重新加回來，
  改用「4 個核心因子 + margin_usage」湊成規格說的「5 因子 + margin_usage」，跟因子穩定性
  分析報告要求的「五個因子（含 margin_usage）」對得上
- 機制 one-hot（4 欄）：BULL/NEUTRAL/WARNING/BEAR，來自 `regime/regime_engine.py`；
  機制尚未暖機完成的早期日期全部填 0（代表「當下沒有機制資訊」，不是猜一個假狀態）
- 大盤波動分位（1 欄）：重用 `regime/indicators.py` 的 `realized_vol_percentile`，
  跟健康分數用的是同一個指標，不重算第二個不一致的版本

**訓練方式**：LightGBM 迴歸（n_estimators 500／max_depth 5／learning_rate 0.01／
early stopping 50），walk-forward 每年 1/1 重訓一次，訓練資料是該年以前的「全部」歷史
（expanding window），且用全市場所有股票（不限流動性前 300、不限最終選中的 30 檔）——
刻意跟 Task 2 的線性 IC 排查方法完全不同（不同樣本範圍＋不同模型類別），是真正獨立的
第二次檢驗，不是換個名字重測一次。訓練樣本抽樣頻率：全市場每 5 個交易日取一天（計算量
考量，明確記錄在模組說明裡；預測仍逐日進行，不受影響）。

**真實跑出的結果（9 個 walk-forward fold，2018-2026，2013-2017 因暖機不足自動跳過）**：
訓練樣本數從 2018 年的 19.7 萬筆成長到 2026 年的 59.2 萬筆，`best_iteration`（early
stopping 觸發點）在 51~485 之間跳動，沒有固定卡在 500 的上限（代表 early stopping
真的有在運作，不是擺設）。

**因子穩定性分析結論**（`reports/factor_stability_analysis.md`）：
- momentum、low_vol 持續是最重要的兩個因子（low_vol 9 年中 5 年排名第一）
- **margin_usage 連續 9 年都排名最後（排名標準差 = 0）**——跟 Task 2 線性 IC 排查的
  結論一致（IC -0.0039），這次換了完全不同的方法（非線性模型＋全市場樣本）重新驗證，
  結論相同：margin_usage 不是「線性方法測不出來的隱藏訊號」，是真的沒有訊號
- rev_yoy 排名標準差最高（0.93），比較看市況，不穩定
- 額外實驗（只用最近一年 vs 最近三年加權平均）：最近三年加權平均平均 IC 較高
  （0.0629 vs 0.0532）但年度間標準差沒有比較小，是取捨不是完勝，結果只供之後參考，
  不會現在就改動主模型的 expanding-window 訓練方式

**端到端回測比較**（誠實列出：不是完全公平的逐年對照，因為 ML 版的預測值只從 2018 年
才開始有值，2015-2017 是 walk-forward 暖機期，那三年 ML 版等於空手／低配置）：

| 指標 | 基準版（Task 2） | ML 版（Task 5） |
|---|---|---|
| 總報酬 | 92.1% | 64.3%（含 2015-17 空手期拖累） |
| 年化報酬 | 6.5% | 6.8% |
| Sharpe | 0.39 | 0.44 |
| 最大回撤 | -24.9% | -34.0% |
| 年化換手率 | 255.2% | 330.2% |

年化報酬與 Sharpe 略優於基準版，但回撤更大、換手率更高，總報酬較低（主因是暖機期空手，
用年化數字比較更公平）——好壞參半，不建議現在就把 `use_ml_composite` 設成預設值，
維持 Task 2 線性版為預設（`use_ml_composite: bool = False`），ML 版本保留作為可選路徑，
供 Task 6 的 ablation（D：ML＋機制曝險）與之後的策略選擇參考。

**`quant_layer2.py` 整合**：`build_positions()`／`run_pipeline()` 新增 `ml_scores`／
`use_ml_composite` 參數，跟 `use_regime_weights` 互斥（同時開啟丟 `ValueError`——兩者
結合的明確語意留給 Task 6 的 ablation D 再定義）。

**驗收結果**：ML 版跑通 ✅（不影響現有線性版本，`use_ml_composite` 預設 `False`）；
`reports/shap_summary.png` 產出 ✅（1200×825 PNG，用最後一個 fold［2026 年測試集］
抽樣 3000 筆畫出）；`reports/feature_importance_by_year.md` 產出 ✅（含每年比較）；
額外要求的 `reports/factor_stability_analysis.md` 也產出 ✅。`tests/test_ml_composite.py`
新增 10 項測試（合成資料），pytest 全部 51 項通過。

### Task 6 細節（驗證框架）—— ✅ 完成（2026-08-23）

`strategy/walk_forward.py` 依 Task 6a 規格重寫（原本的舊版是任務書規格之外的獨立實作、
用自己手刻的因子公式，跟 `factors/` 模組定義會逐漸分歧——這次重寫改成呼叫
`factors/style.py` 與 `quant_layer2.build_rev_yoy()`，維持因子定義單一事實來源）；
新增 `strategy/ablation.py`（6b）、`strategy/significance.py`（6c）、
`strategy/attribution.py`（6d）。`strategy/walk_forward_l3.py`（Layer 3 anchored 版）
維持獨立、不動，任務書範圍外。

**6a Walk-Forward**（`reports/walk_forward_results.{md,json}`）：訓練
[2015-01-01, y-1] → 測試 [y]，y=2020..2025 共 6 fold，權重只用訓練期資料算、凍結後
套用到測試年。結果：2020 +2.2%／2021 +25.9%／2022 -13.3%／2023 +18.6%／2024 +7.2%／
2025 -1.2%，**4/6 正報酬**。整體 OOS（逐日串接）年化 +5.7%／Sharpe 0.288／MDD -26.3%。

**6b Ablation**（`reports/ablation_results.md`）：A 固定權重無機制／B 固定權重+機制曝險／
C 動態權重+機制曝險／D ML+機制曝險，同條件對照（同回測期間/參數，B/C/D 共用同一次
`regime_engine` 輸出）：

| 版本 | 總報酬 | 年化報酬 | Sharpe | MDD |
|---|---:|---:|---:|---:|
| A | +68.7% | +5.1% | 0.301 | -24.2% |
| B | +14.8% | +1.3% | -0.054 | -11.1% |
| C | +17.2% | +1.5% | 0.011 | -10.8% |
| D | +10.1% | +1.3% | -0.089 | -10.1% |

B vs A：MDD 改善 54.2%（門檻 25% ✅），Sharpe -0.054 vs 門檻 0.201（❌ 未通過，延續 Task 4
已記錄的機制曝險在史詩級多頭期間犧牲報酬換取低波動的取捨，同一個現象在 Task 6 用更嚴謹
的同條件對照又驗證一次）。

**6c 統計顯著性**（`reports/significance_report.md`）：Task 2 基準策略（年化 6.46%／
Sharpe 0.393）—— Deflated Sharpe Ratio（n_trials=8，這個專案真正做過的 8 個策略變體）
0.4190（未達 0.95 顯著門檻）；Sharpe 95% CI [-0.056, 1.132]（含 0，不顯著）；Bootstrap
p-value（vs TAIEX buy-and-hold）0.0272（**顯著，但方向是負面的**——策略顯著跑輸大盤，
年化落後約 7.4 個百分點，這段期間台股是史詩級多頭）。三項檢定裡唯一顯著的結果是
「策略跑輸大盤」，這個發現需要在 README Limitations 如實揭露。

**6d 績效歸因**（`reports/attribution_report.md`）：總報酬恆等式分解（`identity_check`
斷言通過）。完整版（固定權重）淨總報酬 +68.7%：momentum 貢獻最大 +31.0%（權重 0.34），
rev_yoy 最小 +6.5%（權重 0.18）；擇時貢獻 +27.7%；成本拖累 -8.3%；殘差 -10.0%。

**過程中發現並修正一個 bug**：`significance.py` 的 `deflated_sharpe_ratio()` 第一版把
年化 Sharpe 直接代入「單期」公式，DSR 飽和失真在 1.0000，被單元測試的邏輯斷言（n_trials
越多 DSR 應該越低）抓到，修正單位轉換後重新真實跑出 0.4190，完整記錄在
`docs/DECISIONS.md`（「Task 6」那筆）。

**驗收結果**：6 fold ≥4 正報酬 ✅／B 版 MDD 改善 ≥25% ✅／B 版 Sharpe ≥ A−0.1 ❌（已知
取捨）／significance＋attribution 報告完整產出 ✅。`tests/test_task6.py` 新增 14 項測試，
pytest 全部 67/67 通過。`quant_layer2.py` 新增 `use_fixed_weights`／`fixed_weights` 參數
（Task 6b/6d 需要的固定權重與單因子版模式）。

### Task 7 細節（研究誠信模組）—— ✅ 完成（2026-08-23）

**7a 存活者偏誤**（`data_pipeline/survivorship.py`，`reports/survivorship_analysis.md`）：
真的用既有 TEJ 試用金鑰測試 `TRAIL/AIND`，證實金鑰已過期（訂閱區間
2026-04-27～2026-07-27，早已過期），改用任務書規格的 FinMind fallback。過程中發現
兩個重要方法論限制：(1) 股票代號會被回收給新公司（例如代號 1262），用「代號是否還在
FinMind 現行清單」判斷下市會被污染 (2) 改用自家資料庫 staleness 訊號抓到 97/2056
（4.7%）候選失效股票，但交叉比對後這 97 檔**全部**仍在 FinMind 現行清單裡——代表這個
訊號主要反映本專案自己的資料管線覆蓋缺口，不是真正下市。**誠實結論：本專案目前沒有
可靠資料源能精確量化真實下市檔數**，改用 Shumway (1997) 文獻估計值（2-4%/年）當替代
基準，附上 README Limitations 建議英文段落。97 筆候選（含 confidence 標記）寫入新增的
`delisted_stocks` SQLite 表。

**7b 因子衰退監控**（`regime/decay_monitor.py`，`reports/factor_decay_history.png`）：
追蹤 6 個因子（momentum/value/rev_yoy/low_vol/margin_usage/inst_flow）的 252 日滾動
|IC|，跟展開式（避免 look-ahead）長期均值比較，<50% 判定衰退。真實結果：**目前 6 個
因子全部正常，沒有衰退**。額外輸出 `reports/factor_decay_alerts_latest.json`，是 Task 8
未來 `signals/*.json` 的 `factor_decay_alerts` 欄位的真實快照範例。

**7c 策略容量分析**（`strategy/capacity.py`，`reports/capacity_analysis.md`）：ADV 5% 規則，
對 Task 2 基準策略 22 次真實換倉逐一反推最大可佈署資金。**最新一次換倉（2025-11-04）
容量估計約 NT$ 2.63 億元**，全期中位數約 NT$ 1.07 億元。

**驗收結果**：三份報告/圖全部真實產出 ✅；survivorship 含量化估計 ✅（自家資料 4.7% 缺口
+ 文獻 2-4%/年，兩者並陳，且誠實記錄了「無法精確量化」本身這個發現）。`tests/test_task7.py`
新增 13 項測試，pytest 全部 85/85 通過。`data_pipeline/schema.py` 新增 `delisted_stocks`
表；`requirements1.txt` 補 `tejapi`。

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
| `survivorship.py` | **新增（Task 7a）**：TEJ 可用性檢查（真實測試，發現金鑰過期）+ FinMind fallback 量化缺口，寫入 `delisted_stocks` 表 |

### `strategy/`

| 檔案 | 角色 |
|---|---|
| `quant_layer2.py` | 多因子回測引擎（任務書鎖定的目標檔案；因子邏輯模組化到 `factors/`，複合權重支援動態 IC 加權／Task 4 機制靜態權重／Task 5 ML 分數／Task 6 固定權重四種排名依據，`use_regime_factor_weights`／`use_regime_exposure` 兩個獨立開關（2026-08-22 從 `use_regime_weights` 拆分），`inst_flow` 已移出複合） |
| `factors/base.py` | **新增（Task 2）**：`cross_zscore`、`winsorize` 共用工具 |
| `factors/style.py` | **新增（Task 2）**：`momentum_52w`、`value_composite`、`low_vol_ivol` |
| `factors/taiwan.py` | **新增（Task 2）**：`inst_flow`、`margin_usage`、`margin_squeeze_market`、`quality`（佔位）——`inst_flow`/`margin_usage` 仍計算，但不進 `quant_layer2.py` 的複合權重（見上方 Task 2 最終決定） |
| `portfolio.py` | **新增（Task 4c）**：`equal_weight`／`risk_parity_weight`／`apply_buffer`／`decompose_turnover`／`write_turnover_report`，`build_positions()` 呼叫這裡的邏輯 |
| `ml_composite.py` | **新增（Task 5）**：LightGBM 迴歸版因子合成，walk-forward 每年重訓，`quant_layer2.py` 用 `ml_scores`/`use_ml_composite` 參數接受它的輸出 |
| `quant_layer3.py` | Layer 3 重新設計版（價值+品質因子+風險平價，任務書未提及） |
| `quant_pure_ml.py` | 純 ML-driven 策略 M1（任務書未提及） |
| `cta_module.py` | CTA 趨勢追蹤策略 I（任務書未提及） |
| `pead_module.py` | PEAD 事件驅動策略 J（任務書未提及） |
| `pairs_module.py` | 配對交易策略 K（任務書未提及） |
| `pairing_analyzer.py` | 策略搭配效果分析工具 |
| `portfolio_combiner.py` | 多 sleeve 組合配置器 N2 |
| `walk_forward.py` | **重寫（Task 6a）**：Layer 2 walk-forward OOS 驗證，訓練[2015,y-1]→測試[y] 共 6 fold，改呼叫 `factors/` 模組（不再手刻公式） |
| `walk_forward_l3.py` | Layer 3 專用 anchored walk-forward 驗證（任務書未提及，獨立分支） |
| `ablation.py` | **新增（Task 6b）**：A/B/C/D 四版本同條件對照 + 機制分段績效 |
| `significance.py` | **新增（Task 6c）**：Deflated Sharpe Ratio／Lo(2002) 信賴區間／Bootstrap p-value |
| `attribution.py` | **新增（Task 6d）**：總報酬恆等式分解（因子邊際貢獻+擇時貢獻-成本+殘差） |
| `benchmark_concentration.py` | **新增（Task 6c 後續診斷）**：台積電對 TAIEX 總報酬的貢獻（移除法）、策略 vs 排除台積電/等權重基準的顯著性檢定 |
| `capacity.py` | **新增（Task 7c）**：ADV 5% 規則反推策略最大可佈署資金 |

### `regime/`（**新增，Task 3**，機制偵測模組，只被 `quant_layer2.py` 呼叫，不反向依賴它）

| 檔案 | 角色 |
|---|---|
| `data_loader.py` | 獨立讀取 SQLite（close/volume/returns、TAIEX、法人市場層級流量、融資餘額加總） |
| `indicators.py` | 11 個機制指標（3a） |
| `health_score.py` | 0-100 健康分數合成（3b） |
| `hmm_detector.py` | GaussianHMM 3 狀態偵測，expanding window（3c） |
| `ml_alert.py` | LightGBM 崩盤預警，walk-forward（3d） |
| `regime_engine.py` | 分類規則 + hysteresis 整合（3e），`REGIME_FACTOR_WEIGHTS` 命名易混淆處見檔案內註解 |
| `plot_regimes.py` | 產出 `reports/regime_history.png`（3f） |
| `decay_monitor.py` | **新增（Task 7b）**：6 個因子 252 日滾動 \|IC\| vs 展開式長期均值，衰退判定，輸出 `factor_decay_history.png` |

### `tests/`

| 檔案 | 角色 |
|---|---|
| `test_factors.py` | **新增（Task 2）**：`factors/` 模組的單元測試（合成資料，11 項全過） |
| `test_download_lock.py` | **新增（Task 1 402 事故修復）**：鎖檔機制測試（6 項） |
| `test_download_supplementary.py` | **新增（Task 1 402 事故修復）**：配額耗盡/休眠/恢復測試（5 項） |
| `test_regime.py` | **新增（Task 3）**：`regime/` 模組單元測試，含 HMM 無 look-ahead 斷言（11 項） |
| `test_portfolio.py` | **新增（Task 4）**：`strategy/portfolio.py` 單元測試（7 項） |
| `test_ml_composite.py` | **新增（Task 5，2026-08-22 補充 2 項）**：`strategy/ml_composite.py` 單元測試 + `quant_layer2.py` 的 `use_ml_composite`／`use_regime_factor_weights`／`use_regime_exposure` 整合測試（12 項） |
| `test_task6.py` | **新增（Task 6）**：`walk_forward.py`／`ablation.py`／`significance.py`／`attribution.py` 純邏輯函式測試，合成資料，含 DSR 單位換算 bug 的迴歸測試（14 項） |
| `test_benchmark_concentration.py` | **新增（Task 6c 後續診斷）**：代理市值權重、移除法貢獻計算的邏輯測試（5 項） |
| `test_task7.py` | **新增（Task 7）**：`survivorship.py`／`decay_monitor.py`／`capacity.py` 純邏輯函式測試，合成資料（13 項） |

**pytest 現況：85/85 全過。**

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

**新增**：`DECISIONS.md`（規則 11 決定紀錄，每次卡在需要你決定的地方都記一筆：問題/選項/
後果/最後選擇/為什麼——目前已有 inst_flow IC 偏低、402 bug、Task 3 機制偵測驗收、
Task 4 機制版績效落差、Task 5 ML 因子合成結果 共 5 筆）、`DATA_AVAILABILITY.md`
（FinMind 各資料集真實最早可得日期偵查，2012-05-02 資料延伸的完整評估與執行記錄）。

**新增（reports/）**：`factor_negative_findings.md`（inst_flow/margin_usage 負面研究
發現正式報告）、`health_score_breadth_divergence.md`（BULL 狀態全期罕見的根因分析）、
`regime_history.png`（Task 3 機制歷史圖）、`ml_alert_auc.json`（ML 崩盤預警各年 AUC）、
`turnover_decomposition.md`（Task 4 換手率分解報告）、`shap_summary.png`（Task 5 SHAP
summary，最後一個 fold 的特徵貢獻分佈）、`feature_importance_by_year.md`（Task 5 逐年
feature importance）、`factor_stability_analysis.md`（Task 5 因子穩定性分析＋訓練策略
比較實驗）、`walk_forward_results.{md,json}`（Task 6a walk-forward OOS 驗證）、
`ablation_results.md`（Task 6b A/B/C/D 同條件對照＋機制分段績效）、
`significance_report.md`（Task 6c 統計顯著性檢定）、`attribution_report.md`
（Task 6d 績效歸因恆等式分解）、`benchmark_concentration_analysis.md`
（Task 6c 後續診斷：台積電集中度對「策略跑輸大盤」發現的補充脈絡）、
`survivorship_analysis.md`（Task 7a 存活者偏誤，含 README Limitations 建議英文段落）、
`factor_decay_history.png`／`factor_decay_alerts_latest.json`（Task 7b 因子衰退監控）、
`capacity_analysis.md`（Task 7c 策略容量分析）。

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

4. ~~**requirements1.txt 缺依賴**~~ **已修復**——`matplotlib`、`pytest` 於 Task 2 期間補進
   （venv 被整個重建後，這兩個套件連同其他非 `requirements1.txt` 套件全部消失，見上方
   「環境注意事項」第三輪）；`hmmlearn` 於 Task 3 開工前補進；`shap` 於 Task 5 補進。
   `bot.py` 用到的 `discord.py` 還沒補回去，因為 `bot.py` 目前停用中，
   見 `_disabled/discord/README_RESTORE.md`（這個不影響 Task 1-9 的範圍，維持現狀）。

5. **`.env` 實際變數與兩份任務文件的假設不符** — 任務文件都寫「`.env` 已存在
   `FINMIND_TOKEN / LINE_NOTIFY_TOKEN / NOTION_TOKEN / NOTION_DATABASE_ID`」，但實際 `.env`
   只有 `DISCORD_TOKEN / ADMIN_ID / NOTIFICATION_CHANNEL_ID / FINMIND_TOKEN / FUGLE_API_KEY`。
   `LINE_NOTIFY_TOKEN`、`NOTION_TOKEN`、`NOTION_DATABASE_ID` 目前**都沒有設定**。程式碼有做
   graceful degradation（`notifier.py` 缺 token 時會印警告並跳過，不會 crash），但代表就算
   `daily_update.py` 的 SyntaxError 修好，LINE/Notion 通知現在也發不出去 —— 這是 Task 8
   驗收「LINE 收到通知」的另一個獨立阻礙，需要你之後補上這三個環境變數。

6. ~~**`quant_layer2.py` 的 `inst_flow` 權重（0.24，動態 IC 加權下的預設值）與 Task 2c
   指定的固定權重（0.30）不一致**~~ **已解決（2026-08-18）**——`inst_flow` 經四輪真實資料
   IC 排查後確認無穩定預測力，已正式移出複合權重（決定 C），這個權重不一致的問題已經不存在，
   因為 `inst_flow` 根本不再參與複合計算。詳見 `docs/DECISIONS.md`。

7. ~~**任務文件基準過時**~~ **已解決（2026-08-17）**——見上方「0. 先看這個」段落的更新，
   Task 3-4 接 `quant_layer2.py`（不接 Layer 3/N 系列）已經確認並執行完畢。

8. ~~**【嚴重】402 配額用盡被誤判為「無資料」跳過**~~ **已修復**——完整根因與修法見下方
   「§5 402 事故 — 根因、修法、驗證」。`download_institutional.py` 有完全一樣的 bug，
   也一併修了（複查發現「institutional 已更新到 2026-08-17」的說法也是錯的，見 §5）。

9. **（Task 3-4 期間發現）機制動態版策略績效遠低於基準版** —— BULL 狀態全期只出現 1.9%
   交易日，這段回測期間台股經歷史詩級大多頭，機制系統太保守、大部分時間曝險只有
   10-40%，總報酬 13.6% vs 基準版 92.1%。不是 bug，是風險/報酬的真實取捨，完整記錄在
   `docs/DECISIONS.md`（「Task 4」那筆），需要你之後評估是否調整機制校準方式。

10. **（Task 3 期間發現）ml_alert.py 原本會讓最新 ~20 個交易日永遠拿不到 crash_prob**——
    已修復（訓練/評估用標籤、預測只需要特徵，兩者分開處理），對 Task 8 每日自動化很重要。

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

## 4. 建議的執行起點（2026-08-23 更新：Task 1-7 已完成，這裡是給 Task 8 開始前的提醒）

- **Task 1-7 全部完成**，本節以下內容是歷史記錄（Task 1 剛開始時寫的），保留給未來回顧
  時參考當時的判斷依據。
- **下一步是 Task 8（自動化整合）**：`automation/daily_update.py` 升級（增量更新加入
  三大法人/融資/TAIEX；每日算機制訊號寫入 signals JSON 的 `"regime"` 欄位；LINE 通知
  加當日機制+健康分數，機制降級時發警示）、Notion 欄位（市場機制/健康分數）、
  `requirements1.txt` 確認完整（已補齊 hmmlearn/lightgbm/scipy/matplotlib/shap/pytest/
  tejapi）、GitHub Actions workflow 確認正常。
- **Task 8 可以直接掛上 Task 7b 的因子衰退警示**：`regime/decay_monitor.py` 的
  `latest_alerts()` 已經是可以直接呼叫的函式，回傳格式已經對齊 `factor_decay_alerts`
  這個 JSON 欄位需要的結構（見 `reports/factor_decay_alerts_latest.json` 的真實範例），
  Task 8 升級 `daily_update.py` 時可以直接呼叫，不用重新設計格式。
- **`automation/daily_update.py` 目前可以正常編譯**（2026-08-23 重新用 `python -m py_compile`
  確認過，之前 Task 1 期間發現的 SyntaxError 確實已修復，不是過時的舊筆記——但 Task 8
  升級增量更新/機制訊號/LINE 通知這些新功能時，仍然要跑一次端到端的真實執行，不能只看
  編譯通過就當作完成）。
- **LINE_NOTIFY_TOKEN / NOTION_TOKEN / NOTION_DATABASE_ID 仍未設定**，Task 8 驗收前需要你
  另外申請並補進 `.env`，這不是我能代勞的部分（Task 8 通知功能設計成 token 不存在時自動
  降級成 dry-run，不會因此卡住其他 Task，但沒有這些 token 就無法驗收真實通知有沒有送達）。
- **Task 7a 的存活者偏誤發現需要在 Task 9 README 撰寫時直接引用**：本專案目前沒有可靠
  資料源能精確量化真實下市檔數（TEJ 金鑰過期、FinMind 現行清單比對會被股票代號回收
  污染），已經準備好一段可以直接貼進 README Limitations 的英文段落（見
  `reports/survivorship_analysis.md` 第 5 節），Task 9 不用重新寫。
- **Task 6c 統計顯著性檢定發現一個重要的誠實結果，需要在 Task 9 README 撰寫時如實揭露**：
  Task 2 基準策略在 2015-2026 這段回測期間，逐日報酬「顯著地」跑輸 TAIEX 買進持有
  （bootstrap p-value 0.0272，年化落後約 7.4 個百分點），這段期間台股經歷史詩級大多頭。
  Deflated Sharpe Ratio（0.4190）與 Sharpe 95% 信賴區間（含 0）都顯示策略的風險調整後
  優勢也**不具統計顯著性**。Task 9 撰寫 README 的 Limitations 章節時，不能只挑對策略
  有利的指標（例如波動度更低、回撤更小）來寫，這個「絕對報酬跑輸大盤」的結果要並列
  說明，這是本專案研究誠信原則的核心測試——不能因為結果不好看就選擇性引用。
  **後續已補充診斷**（2026-08-23，`strategy/benchmark_concentration.py`、
  `reports/benchmark_concentration_analysis.md`）：這個負面結果排查後發現主要是「大盤
  被台積電這一檔巨型股撐起來」的集中度效應（台積電貢獻代理指數總報酬至少 18.9%，
  且這個數字有方法論限制、可能是低估值）——策略對排除台積電／等權重大盤都不再顯著
  跑輸。**但對真實市值加權 TAIEX（README 應該採用的標準基準）仍然顯著跑輸這個原始
  發現完全沒有改變**，Task 9 寫 README 時兩個發現（原始負面結果 + 補充脈絡）都要寫，
  不能只挑對策略有利的半句話，完整決策記錄見 `docs/DECISIONS.md`
  「Task 6c 後續診斷」那筆。
- **Task 4 發現的機制版績效落差**、**Task 5 的 ML 版好壞參半結果**、**Task 6b Ablation
  的同條件對照**三者現在都有完整的真實數字可以互相對照（見上方 Task 4/5/6 細節），
  一致指向同一個結論：機制曝險在這段史詩級多頭期間會系統性犧牲報酬換取低波動與低回撤，
  是否要調整機制校準方式（Task 3 的健康分數/hysteresis 設計）或 ML 訓練策略（因子穩定性
  分析報告裡「最近三年加權平均」備選方案），目前累積的證據已經足夠支持你做這個決定，
  不需要更多實驗；如果決定要調整，建議在 Task 8（自動化整合，機制訊號會寫進每日通知）
  之前定案，避免調整後還要重新驗證自動化流程。
- **LINE_NOTIFY_TOKEN / NOTION_TOKEN / NOTION_DATABASE_ID 仍未設定**，Task 8 驗收前需要你
  另外申請並補進 `.env`，這不是我能代勞的部分（Task 8 通知功能設計成 token 不存在時自動
  降級成 dry-run，不會因此卡住其他 Task，但沒有這些 token 就無法驗收真實通知有沒有送達）。

---

### 以下為 Task 1 執行前的原始判斷（歷史記錄）

- **Task 1 可以直接開始**，且工作量比任務書描述的小：法人資料已達標，只需要新增
  `margin_trading` / `market_index` 兩張表 + 擴充下載邏輯（融資融券 + TAIEX），不用整個重寫。
- **在動 Task 2 之前，強烈建議先解決第 0 節的範疇問題**（機制偵測要接 `quant_layer2.py` 還是
  更成熟的 Layer 3/N 系列），否則 Task 2 的模組化重構可能做錯目標檔案。
- **`automation/daily_update.py` 的 SyntaxError 建議盡快修掉**，不一定要現在做（不在 Task 1
  範圍內），但它會持續擋住任何「本機跑通 daily_update.py」的驗收，越晚修代價不變、只是一直卡著。
  是否現在順手修掉這一行，還是留到 Task 8 一起處理，請你決定。
- Task 3（機制偵測）是任務書裡最重的一塊，且完全空白，一旦 Task 1/2 的資料與因子基礎確定，
  應該是下一個重點。
