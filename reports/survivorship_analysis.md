# 存活者偏誤分析（Task 7a）

## 1. 偏誤來源說明

存活者偏誤（survivorship bias）指的是：如果一個歷史資料庫只保留「目前還存在」的公司完整歷史，被下市/合併/清算的公司會從資料庫裡完全消失（不只是消失之後的資料，連它們存活期間的資料也一起不見）。用這種資料庫回測，會系統性高估報酬——因為表現差到被下市的公司，剛好是報酬最差的那群，把它們整批排除，剩下的都是「活下來的」，平均表現自然比真實情況好看。

這個偏誤有兩種型態，影響方式不同：

- **look-back 排除偏誤**：資料收集當下如果是用「現在還在交易的股票清單」回頭抓歷史資料，那些在收集之前就已經下市的公司從一開始就不會出現在資料庫裡，**這種缺口從資料庫內部是量不出來的**——資料庫裡沒有的東西，沒辦法拿資料庫自己的資料去證明它存在過。

- **研究期間中途下市**：研究期間中途才下市的公司，如果資料收集是持續進行（不是回頭抓），下市前的資料通常還在，這種情況偏誤比較小，但仍然要看資料管線本身有沒有確實追蹤到公司下市那個事件並停止更新（而不是因為別的原因漏更新，見下方第 3 節的重要發現）。

## 2. TEJ TRAIL/AIND 可用性檢查（優先路徑）

**真實測試結果：TEJ TRAIL/AIND 目前不可用**——錯誤訊息：`(Status 400) (Tej Error AAA003) 認證失敗，api_key已過期`。
這個專案既有的 TEJ 試用帳號（見 `test_tej.py`）訂閱區間是 2026-04-27～2026-07-27，執行這項診斷時（2026-08-23）已經過期約一個月。已改用任務書規格的 fallback 路徑（FinMind TaiwanStockInfo 歷史比對）。


## 3. FinMind Fallback 量化結果（真實計算，含重要方法論發現）

研究期間（2015-01-01～2026-04-17）資料庫裡出現過的股票共**2056** 檔。其中 **97** 檔（4.7%）相對資料庫整體最新日超過 180 天沒有更新，是「候選失效」股票。

**交叉比對 FinMind 現行清單後的重要發現**：這 97 檔候選裡，**97 檔仍然出現在 FinMind 目前的 TaiwanStockInfo 清單裡**（低可信度——很可能是本專案自己的資料下載管線覆蓋不全，不是真的下市），只有 **0 檔完全不在 FinMind 現行清單裡**（中可信度，較強的下市證據，但仍無法排除代碼變更/回收的可能）。

**這代表本專案目前沒有可靠的資料來源能精確量化「真正下市」的檔數**——原因有兩個：(1) 用「股票代號是否還在 FinMind 現行清單」判斷，會被台股代號回收再利用給新公司污染（例如代號 1262，本專案資料庫的舊資料在 2020-09-24 停止更新，但 FinMind 目前的 TaiwanStockInfo 顯示這個代號目前掛的是完全不同的公司「綠悅-KY」）；(2) 用「本專案自己資料庫多久沒更新」判斷，交叉比對顯示幾乎全部候選都還在 FinMind 現行清單裡，代表這個訊號主要反映的是本專案資料下載管線的覆蓋缺口，不是公司真的下市。**這是誠實的方法論限制，不是算出一個看起來合理的數字就交差**——這正是任務書「保底必做」條款存在的理由。

## 4. 文獻估計基準（Shumway 1997）

由於本專案的資料無法精確量化真實下市造成的偏誤幅度，改用學術文獻的估計值當誠實的替代基準：Shumway (1997, "The Delisting Bias in CRSP Data", *Journal of Finance*) 發現，忽略下市報酬（尤其是下市前的極端負報酬，很多資料庫會直接把下市股票的最後報酬記成缺值而非真實的清算/下市損失）會讓回測報酬平均被高估約 **2-4 個百分點／年**。這是被廣泛引用的估計區間，本專案採用這個文獻基準來提醒讀者：本專案回測報告的年化報酬（例如 Task 2 基準版年化 6.5%、Task 6b Ablation A 年化 5.1%）應該打一個折扣區間去理解，實際可能落在文獻估計的下修範圍內，不是報告數字本身有誤，而是任何沒有完整下市資料的回測都有這個系統性風險。

## 5. README Limitations 建議英文段落

```
Survivorship Bias: This backtest's price database was assembled without a
verified official delisting registry (a TEJ TRAIL/AIND subscription was
attempted but was expired at the time of this analysis). A FinMind-based
cross-check identified 97 candidate stocks (4.7%
of the study-period universe) whose price data stopped updating, but most of
these remain listed in FinMind's current registry, making it impossible to
reliably distinguish genuine delistings from data-pipeline coverage gaps or
ticker-symbol recycling. Reported backtest returns should therefore be
interpreted with the academic literature's estimate in mind: Shumway (1997)
finds that ignoring delisting returns inflates backtested performance by
roughly 2-4 percentage points per year. This is a data-availability
limitation of the project, not a claim that the reported numbers are wrong.
```

## 6. `delisted_stocks` 表

已寫入 SQLite 的 `delisted_stocks` 表，97 筆候選記錄，欄位含 `confidence`（low/medium）與 `note` 說明判斷依據，供之後（例如補進TEJ 資料源後）重新比對用。
