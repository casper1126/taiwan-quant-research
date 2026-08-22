# Walk-Forward Out-of-Sample 驗證（Task 6a）

## 閱讀指南：IS（樣本內）vs OOS（樣本外）是什麼意思

`reports/equity_curve.csv`（`python run.py --step 2` 的輸出）是**樣本內（In-Sample,
IS）**回測：因子權重是用全樣本（含測試期本身）的滾動 IC 算出來的，等於「用未來
已經發生的結果，去驗證這個方法在過去有沒有用」——這種回測結構性地偏樂觀，因為
任何策略多少都會對它看過的資料「合身」。

這份報告是**樣本外（Out-of-Sample, OOS）**驗證：每一個 fold 的因子權重只用
「測試年之前」的資料算出來，算完就凍結，完全不再用測試年（或更之後）的任何資料
去調整——測試年的績效是模型「沒看過」這段資料、純粹用過去學到的權重去賭出來的
結果。OOS 數字通常會比 IS 差（這是正常且健康的現象，代表沒有嚴重過擬合）；如果
OOS 數字反而比 IS 好，反而要懷疑是不是哪裡的因子計算不小心洩漏了未來資訊。

**這裡驗證的是目前 quant_layer2.py 生產環境實際在用的策略**（動態 IC 加權、
四個核心因子 momentum/value/rev_yoy/low_vol，不含 inst_flow/margin_usage——
這兩個因子已經在 Task 2 被排查確認沒有穩定訊號，見
`reports/factor_negative_findings.md`），不含 Task 3-5 的機制/ML 功能——
那些版本的同條件對照在 `reports/ablation_results.md`（Task 6b）。

**Strategy**: Taiwan Multi-Factor (Momentum / Value / Revenue YoY / Low-Vol)
**Training start**: 2015-01-01 (expanding window)
**OOS period**: 2020–2025

## Per-Year OOS Results

| Year | Annual Return | Sharpe | Max Drawdown | Turnover |
|------|:-------------:|:------:|:------------:|:--------:|
| 2020 | +2.2% | 0.040 | -26.3% | 343% |
| 2021 | +25.9% | 1.684 | -11.8% | 317% |
| 2022 | -13.3% | -1.103 | -23.6% | 342% |
| 2023 | +18.6% | 1.969 | -7.9% | 366% |
| 2024 | +7.2% | 0.438 | -9.1% | 222% |
| 2025 | -1.2% | -0.156 | -18.5% | 249% |

## Overall OOS Performance（逐日串接，非每年獨立複利重置）

| Total Return | Annual Return | Sharpe | Max Drawdown | 正報酬年數 |
|:------------:|:-------------:|:------:|:------------:|:----------:|
| +37.8% | +5.7% | 0.288 | -26.3% | 4/6 |

> OOS results use ICIR-proportional factor weights estimated on the training window
> only（該年之前的資料），frozen and applied unchanged to the test year — this is what
> makes it a genuine walk-forward test rather than a re-fit-every-day rolling backtest.
> Each fold's weights are recalculated independently from scratch to avoid look-ahead bias.
