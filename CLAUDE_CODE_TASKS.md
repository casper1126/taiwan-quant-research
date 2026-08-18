# CLAUDE_CODE_TASKS.md

台股量化系統開發任務書（純執行版 — 給 Claude Code 在 VS Code 使用）

使用方式：在專案根目錄啟動 `claude`，說：
「請讀取 CLAUDE_CODE_TASKS.md，理解專案背景後開始執行 Task 1。
每個 Task 做完先暫停向我報告驗收結果，不要自己跳到下一個 Task。」

> **這份檔案是 `CLAUDE_CODE_TASKS.md.pdf` 的可編輯 markdown 版本。**
> 原始 PDF 是唯讀的，沒辦法把新規則寫進去，所以建這份檔案作為之後
> 持續更新的正本（原始 Task 1-9 內容照 PDF 逐字轉錄，最高原則區塊
> 新增了 Task 進行過程中補上的規則 9-11）。

## 角色與最高原則

你是資深量化開發工程師，協助我完成台股量化交易系統（MFE 申請作品集）。

違反任何一條即為失敗：

1. 任何訊號在時間點 t 只能使用 t 以前可得的資料（禁止 Look-Ahead Bias）
2. API Token 一律從環境變數讀取，絕不 hardcode
3. 回測含完整成本：手續費 0.1425%×2 + 證交稅 0.3%（賣出）+ 滑價 0.1%
4. 年化報酬用幾何平均：(1+total_return)^(252/n_days) − 1
5. shift(1) 只在部位建構層做一次，回測引擎不再 shift
6. HMM / ML 模型一律 expanding window 或 walk-forward，禁止全樣本 fit 後 predict
7. 每個 Task 完成後跑驗收 → 報告 → 等我確認 → 才進下一個 Task
8. 驗收未過先診斷修復，不准跳過；結果不顯著照實報告

**以下三條是開發過程中新增的永久性規則（2026-08-17 補上，之後每個 Task 都適用）：**

9. **白話優先**：每份 Task 交付報告，標準的 (a)-(e) 技術格式之前，先加一段
   「白話摘要」（3-5 句話，不用任何縮寫或術語，假設讀者是完全不懂程式的人）：
   發生了什麼事、為什麼這件事重要、現在需要做什麼決定（如果有的話）。技術表格
   留給想深入看的時候用，但白話摘要是必須的，不能省略。

10. **不能悄悄放棄任務**：如果一個正在進行的排查/任務被更緊急的事情打斷，回來
    繼續時必須明確列出「原本答應要做的事情清單，哪些做了、哪些還沒做」，不能用
    「都測過了」這種含糊句子帶過去。沒做完的部分要嘛補做，要嘛明確問清楚要不要
    補做——不能默默跳過。

11. **決定紀錄**：每次卡在需要使用者決定的地方，在 `docs/DECISIONS.md` 寫一筆
    記錄：問題描述、可選方案、各方案的後果、最後選了什麼、為什麼。這份檔案讓
    使用者不用回頭翻聊天紀錄就能看懂整個專案做過的關鍵抉擇。

## 專案現況

路徑：`/Users/xiaoyuchen/Downloads/Quant_Trading_System/`　Python：3.13（venv）

現有結構：
```
Quant_Trading_System/
├── data/taiwan_stock.db        # SQLite：2056 檔，2015-2026
├── raw_data/                   # 原始 CSV（唯讀，勿改）
├── signals/ reports/
├── data_pipeline/
│   ├── schema.py cleaner.py loader.py
├── strategy/
│   └── quant_layer2.py         # 多因子策略 v5
├── automation/
│   ├── daily_update.py notifier.py
├── .github/workflows/daily_quant.yml
├── run.py requirements1.txt .env
```

現有資料表：`daily_price` / `daily_valuation` / `monthly_revenue` / `data_quality_log` / `load_manifest`

quant_layer2.py v5 現況：
- 因子：52週新高動能、1/PER、月營收YoY（+40天延遲）、低波動IVOL、籌碼（佔位，無資料）
- 宇宙：日均成交金額前 300；再平衡 120 日 + 緩衝區（進 top30/出 top45）
- 最新回測：總報酬 +8.6%、Sharpe -0.07、換手 321%
- 已知問題：value IC 偏低（0.014）、無籌碼資料、無機制偵測、有 survivorship bias

環境變數（.env 已存在）：`FINMIND_TOKEN` / `LINE_NOTIFY_TOKEN` / `NOTION_TOKEN` / `NOTION_DATABASE_ID`

程式碼品質：type hints + 中文 docstring（含設計理由）、loguru 日誌、API 呼叫含重試、每 Task 一個 git commit（格式 `feat(task3): ...`）

## Task 1：資料補完

**1a.** schema.py 的 TABLE_DDL 新增三張表：

```sql
CREATE TABLE IF NOT EXISTS institutional_investors (
  date TEXT NOT NULL, stock_id TEXT NOT NULL, investor_type TEXT NOT NULL,
  buy REAL, sell REAL, net REAL,
  PRIMARY KEY (date, stock_id, investor_type));
CREATE TABLE IF NOT EXISTS margin_trading (
  date TEXT NOT NULL, stock_id TEXT NOT NULL,
  margin_balance REAL, short_balance REAL, margin_change REAL,
  PRIMARY KEY (date, stock_id));
CREATE TABLE IF NOT EXISTS market_index (
  date TEXT NOT NULL, index_id TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY (date, index_id));
```

加對應索引（stock_id、date）。

**1b.** 建立 `data_pipeline/download_supplementary.py`：從 FinMind 下載（2015-01-01 起，所有 load_manifest 內股票）：
- TaiwanStockInstitutionalInvestorsBuySell → institutional_investors
- TaiwanStockMarginPurchaseShortSale → margin_trading
- TaiwanStockPrice, data_id="TAIEX" → market_index

必備：斷點續傳（查 DB 最新日期只抓增量）、速率控制（0.5s/call，每小時 550 次後 sleep 到整點）、3 次重試、tqdm 進度條。

**1c.** run.py 加指令 `--step 1b`。

**驗收**：
- `SELECT COUNT(DISTINCT stock_id) FROM institutional_investors` ≥ 1500
- margin_trading 覆蓋 ≥ 1500 檔
- TAIEX 完整覆蓋 2015-2026
- 額度不足中斷時記錄進度，我說「繼續」後從斷點續跑

## Task 2：因子庫模組化重構

**2a.** 建立 `strategy/factors/` 模組：
- `factors/base.py` — cross_zscore, winsorize(1%/99%)，共用工具
- `factors/style.py` — momentum_52w, value_composite(0.5/PE + 0.5/PB), low_vol_ivol(60d)
- `factors/taiwan.py` — inst_flow, margin_usage，（quality 佔位函式回傳 NaN + log warning）

統一簽名：`def factor_name(data: dict) -> pd.DataFrame`（寬格式 index=date, columns=stock_id）。

**2b.** taiwan.py 三個因子的規格：

```python
def inst_flow(data) -> pd.DataFrame:
    """外資+投信 60 日累積淨買超 / 60 日平均成交金額（正規化）。
    investor_type 取 Foreign_Investor / Foreign_Dealer_Self / Investment_Trust。"""

def margin_usage(data) -> pd.DataFrame:
    """個股融資餘額 20 日變化率 × (−1)。
    融資快速增加 = 散戶追高 = 負向訊號，故取負號。"""

def margin_squeeze_market(data) -> pd.Series:
    """全市場訊號（非選股因子，給 Task 3 用）：
    squeeze_ratio = 融資總餘額回檔幅度 / TAIEX 回檔幅度（皆從 252 日高點起算）。
    大盤回檔 < 3% 時回傳 NaN。"""
```

**2c.** quant_layer2.py 改為 import factors 模組，複合權重更新：
momentum 0.15 / value 0.20 / rev_yoy 0.20 / low_vol 0.15 / inst_flow 0.30。
margin_usage 暫不進複合（Task 5 的 ML 版才用）。

**2d.** 建 `tests/test_factors.py`：合成資料驗證 (a) 融資暴增股 margin_usage 為負
(b) squeeze_ratio 在大盤跌 20%、融資跌 10% 時 = 0.5 (c) cross_zscore 對 NaN 的處理。

**驗收**：`python run.py --step 2` 跑通；inst_flow IC > 0.03；pytest 全過。

## Task 3：機制偵測模組（核心）

建立 `regime/` 資料夾：

**3a.** `regime/indicators.py` — 11 個指標，全部輸出 `pd.Series(index=date)`：

```python
realized_vol_percentile(index_close, window=20, lookback=252)
vol_of_vol(index_close, window=20)
market_breadth(close_matrix, ma_window=60)            # 站上60MA比例
new_highs_minus_lows(close_matrix, window=252)         # (新高-新低)/總數
avg_pairwise_correlation(returns, window=60, sample_n=100)  # 抽樣算
downside_asymmetry(index_returns, window=60)           # 下跌波動/上漲波動
foreign_flow_pressure(inst_data, window=20)            # 外資20日累積賣超佔比
margin_stress(margin_total, window=5)                  # 融資5日變化率
margin_squeeze_ratio(margin_total, index_close)        # Task 2 同款
margin_capitulation(margin_total, volume)              # 布林：急縮(>5%/5d)後
                                                        # 已穩(3d變化<0.5%)且量縮(<20MA)
turnover_structure(index_volume, window=20)            # 量能相對20日均
```

**3b.** `regime/health_score.py`：每個成分先轉 rolling-252 分位數（0-1），再加權合成 0-100：
波動(反)18、寬度22、高低差13、相關性(反)13、不對稱(反)13、squeeze 12、capitulation 加分 9。
NaN 成分該日跳過並重新正規化權重。

**3c.** `regime/hmm_detector.py`：GaussianHMM 3 狀態，特徵=[TAIEX日報酬, 20日已實現波動]。
Expanding window：每月初用該時點前全部資料 fit，只 predict 當月。
自動標記：均值最高=bull、波動最高且均值負=bear、其餘=neutral。
最少 504 天資料才輸出，之前 NaN。

**3d.** `regime/ml_alert.py`：LightGBM 分類器。特徵=3a 全部指標+大盤 5/20/60 日報酬。
標籤=未來 20 日 TAIEX 報酬 < −5%。Walk-forward 每年重訓。
class_weight='balanced'。輸出 crash_prob + 每 fold AUC 存 reports/ml_alert_auc.json。

**3e.** `regime/regime_engine.py`：

```python
REGIME_FACTOR_WEIGHTS 規則：
health>=60 且 p_bear<0.3 且 crash<0.3 → BULL, exposure 1.0
health>=45 且 p_bear<0.5              → NEUTRAL, 0.7
health>=30 或 p_bear<0.7              → WARNING, 0.4
其餘                                  → BEAR, 0.1
遲滯：升級需連續 10 日滿足、降級 3 日即觸發。NaN 日沿用前值。
```

**3f.** `regime/plot_regimes.py`：`reports/regime_history.png` — TAIEX 走勢，背景依機制著色
（綠BULL/黃NEUTRAL/橘WARNING/紅BEAR），下方子圖健康分數。

**驗收**：
- 2020/02-03 至少 15 個交易日為 WARNING/BEAR
- 2022 年 WARNING+BEAR 天數 > 80
- 2026/04 與 2026/07 的大跌被標記 WARNING/BEAR
- 2026/07 底融資急縮期間 squeeze_ratio 有值且 < 1.0
- 2016-17、2023-24 多頭 BULL 比例 > 60%
- 全期機制切換 < 40 次
- `tests/test_regime.py`：斷言 HMM 在截止日前的輸出不因加入未來資料而改變

## Task 4：機制整合到策略

**4a.** 機制動態因子權重（quant_layer2.py）：

```python
REGIME_FACTOR_WEIGHTS = {
"BULL":    {"momentum":.30,"value":.10,"rev_yoy":.25,"low_vol":.05,"inst_flow":.30},
"NEUTRAL": {"momentum":.15,"value":.20,"rev_yoy":.20,"low_vol":.15,"inst_flow":.30},
"WARNING": {"momentum":.05,"value":.25,"rev_yoy":.15,"low_vol":.35,"inst_flow":.20},
"BEAR":    {"momentum":.00,"value":.30,"rev_yoy":.10,"low_vol":.50,"inst_flow":.10}}
```

每個再平衡日查當日機制，用對應權重合成。

**4b.** 曝險整合：部位 = 選股結果 × recommended_exposure（再平衡日鎖定，期間不動）。

**4c.** `strategy/portfolio.py`：equal_weight / risk_parity_weight（∝1/60日波動）/
apply_buffer(30,45) / turnover_report（分解：換股 vs 權重調整 vs 擇時貢獻 →
reports/turnover_decomposition.md）。quant_layer2 加參數 weighting='equal'|'risk_parity'。

**驗收**：機制動態版跑通；risk_parity 換手 ≤ equal 版 × 1.2；換手分解報告產出。

## Task 5：ML 因子合成

`strategy/ml_composite.py`：
- 特徵：5 因子 + margin_usage + regime one-hot + 大盤波動分位
- 標籤：個股未來 20 日報酬的橫截面分位數
- LightGBM 迴歸（n_est 500, depth 5, lr 0.01, early stop 50），walk-forward 每年重訓
- 輸出 SHAP summary → reports/shap_summary.png
- 每 fold 的 feature importance → reports/feature_importance_by_year.md
- quant_layer2 加參數 use_ml_composite: bool = False

**驗收**：ML 版跑通；SHAP 圖產出；importance 報告含每年比較。

## Task 6：驗證框架

**6a.** `strategy/walk_forward.py`：訓練 [2015, y−1] → 測試 [y]，y=2020..2025 共 6 fold。
輸出 `reports/walk_forward_results.{md,json}`：每年 OOS 年化/Sharpe/MDD/換手 + 全期串接。
md 開頭加「閱讀指南」段落解釋 IS vs OOS。

**6b.** `strategy/ablation.py` — 四版本同條件對照：
A 固定權重無機制 / B 固定權重+機制曝險 / C 動態權重+機制曝險 / D ML+機制曝險 →
`reports/ablation_results.md` 並列表 + 各機制分段績效（BULL/NEUTRAL/WARNING/BEAR 下的年化與 MDD）。

**6c.** `strategy/significance.py`：

```python
deflated_sharpe_ratio(sharpe, n_trials>=8, n_days, skew, kurt)  # Bailey & LdP 2014
sharpe_confidence_interval(daily_returns)                       # Lo 2002
bootstrap_pvalue(strategy_ret, taiex_buyhold_ret, n=10000)
```

→ `reports/significance_report.md`（不顯著照實寫）。

**6d.** `strategy/attribution.py`：總報酬 = Σ各因子邊際貢獻（單因子版回測比較）+ 擇時貢獻 −
成本 + 殘差 → `reports/attribution_report.md`。

**驗收**：
- B 版 MDD 比 A 改善 ≥ 25%；B 版 OOS Sharpe ≥ A − 0.1
- 6 fold 中 ≥ 4 個正報酬
- significance 與 attribution 報告完整產出

## Task 7：研究誠信模組

**7a.** `data_pipeline/survivorship.py`：檢查 TEJ TRAIL/AIND 是否含下市註記 → 有則建
delisted_stocks 表；否則從 FinMind TaiwanStockInfo 歷史比對量化缺口。
保底必做：`reports/survivorship_analysis.md`（偏誤來源、文獻估計 2-4%/年 引 Shumway 1997、
README Limitations 英文建議段落）。

**7b.** `regime/decay_monitor.py`：每因子 252 日滾動 IC；近期 IC < 長期均值 50% → 寫入每日
JSON 的 factor_decay_alerts。產出 `reports/factor_decay_history.png`。

**7c.** `strategy/capacity.py`：ADV 5% 規則 → 每再平衡日最大可部署資金 →
`reports/capacity_analysis.md`（含 NT$ 數字）。

**驗收**：三份報告/圖全部產出，survivorship 含量化估計。

## Task 8：自動化整合

**8a.** `automation/daily_update.py` 升級：增量更新加入三大法人/融資/TAIEX；每日算機制
訊號寫入 signals JSON 的 "regime" 欄位；LINE 通知加當日機制+健康分數，機制降級時發
「⚠️ 機制警示」。

**8b.** Notion 欄位：市場機制（Select）、健康分數（Number）。

**8c.** `requirements1.txt` 補：hmmlearn lightgbm scipy matplotlib shap pytest。
GitHub Actions workflow 確認正常。

**驗收**：本機 `python automation/daily_update.py` 全流程跑通，LINE 收到含機制的通知。

## Task 9：文件與發布

README.md 完整改寫（英文，作品集等級）：Overview / 8 層架構 / Regime 方法論 /
Performance（OOS 表+Ablation 表，只准填真實數字）/ Statistical Significance /
Attribution 摘要 / regime_history.png + factor_decay_history.png /
**Limitations 必列全部**：survivorship（引 7a 數字）、multiple testing（迭代 8+ 版）、
單一市場、機制規則樣本內設計、容量（引 7c）、paper trading 未完成 / References ≥ 15 篇。

`.gitignore` 確認：`*.db`、`.env`、`__pycache__`、`venv/`、`raw_data/`。

Paper Trading 條款：發布日起每日 signals JSON 即模擬紀錄，3 個月後產
`reports/paper_trading_3m.md` 對照回測預期。

**驗收**：README 完成且所有數字可溯源到 reports/ 的檔案；git push 成功。

## 執行順序

Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9

每 Task 完成：跑驗收 → 印摘要 → 暫停等我確認。FinMind 額度中斷：記錄進度，等我說「繼續」。
DSR 不顯著：照實寫，誠實的不顯著比造假的顯著有價值一百倍。
