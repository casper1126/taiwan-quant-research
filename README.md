# Taiwan Equity Quantitative Research System

![Python](https://img.shields.io/badge/Python-3.13-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Last Updated](https://img.shields.io/badge/Updated-2026--04-orange)

A systematic multi-factor equity research system for the Taiwan stock market, built as a portfolio project for MFE applications. Covers the full quant workflow: data engineering → factor research → portfolio construction → walk-forward validation → automated daily signals.

---

## Architecture

| Layer | Module | Description |
|-------|--------|-------------|
| **1 — Data Engineering** | `data_pipeline/` | CSV ingestion, cleaning, SQLite storage |
| **1b — Live Data** | `data_pipeline/download_institutional.py` | Incremental FinMind API fetch |
| **2 — Factor Research** | `strategy/quant_layer2.py` | 5-factor model + vectorized backtest |
| **3 — Validation** | `strategy/walk_forward.py` | Walk-Forward OOS validation |
| **4 — Automation** | `automation/daily_update.py` | Daily signal generation |
| **5 — Notification** | `automation/notifier.py` | LINE Notify + Notion logging |

---

## Performance Results

### In-Sample Backtest (2015–2026)

| Metric | Value |
|--------|-------|
| Total Return | +19.6% |
| Annual Return | +1.7% |
| Sharpe Ratio | 0.02 |
| Max Drawdown | -34.7% |
| Annual Turnover | 376% |

> **Note**: In-sample results are optimistic by nature. Walk-forward OOS results are the honest measure of strategy viability. See `reports/walk_forward_results.md`.

### Factor IC Analysis

| Factor | IC Mean | ICIR | IC > 0 Rate | Description |
|--------|---------|------|-------------|-------------|
| momentum | 0.038 | 0.199 | 61.7% | 52-week high ratio (George & Hwang 2004) |
| value | 0.023 | 0.167 | 56.1% | 1/PER — cheap vs. expensive |
| rev_yoy | 0.020 | 0.170 | 58.6% | Monthly revenue YoY, 40-day lag |
| low_vol | 0.029 | 0.131 | 54.6% | Inverse 60-day volatility (Frazzini & Pedersen 2014) |
| inst_flow | — | — | — | Foreign + trust cumulative net buy (pending data) |

---

## Data Sources

- **[FinMind](https://github.com/FinMind/FinMind)** — Open-source Taiwan financial data API. Used for daily prices, valuation ratios, monthly revenue, and institutional investor flows. Free tier: 600 requests/hour.
- **[TEJ API](https://www.tejwin.com/)** — Premium alternative for institutional-grade historical data (see `test_tej.py`).

Raw CSV data from FinMind is stored locally in `raw_data/` and ingested into SQLite (`data/taiwan_stock.db`). The database is **not committed to Git** (see `.gitignore`).

---

## Data Cleaning

The pipeline (`data_pipeline/`) applies rule-based cleaning defined in `schema.py`:

| Rule | Dataset | Action | Rationale |
|------|---------|--------|-----------|
| `price_zero_close` | price | drop row | Halt days / missing data |
| `price_zero_volume` | price | drop row | Unexecutable trades |
| `price_ohlc_check` | price | drop row | Data source errors |
| `val_per_zero` | valuation | set NaN | Loss-making firms (EPS ≤ 0) |
| `val_negative_yield` | valuation | set NaN | Data error |
| `rev_negative` | revenue | set NaN | Data error |

All cleaning actions are logged to `data_quality_log` in SQLite for auditability.

---

## Factor Design

### Universe
Top 300 stocks by 252-day average dollar volume (`volume × close`), updated daily. This correctly proxies liquidity — price-based proxies conflate price level with tradability (TSMC at NT$900 is liquid; a NT$900 micro-cap is not).

### Factor Definitions
- **Momentum (52W High)**: `close / rolling_252_max`. Captures trend-following without standard momentum's crash risk. Effective in Asian markets (George & Hwang 2004).
- **Value (1/PER)**: Earnings yield. PER = 0 (loss-making) set to NaN to avoid distortion.
- **Revenue YoY**: Month-on-month revenue growth with 40-day publication delay — prevents look-ahead bias from announcement timing in FinMind data.
- **Low-Vol (IVOL)**: `1 / rolling_60_std(daily_return)`. The low-volatility anomaly is amplified in Taiwan by retail preference for high-volatility lottery stocks (Frazzini & Pedersen 2014).
- **Institutional Flow**: 60-day cumulative net buy by foreign investors + investment trusts. Informed-money signal specific to Taiwan market structure.

### Composite Score
ICIR-proportional weights, recalculated each walk-forward fold:

```
composite = Σ (ICIR_i / Σ ICIR_j) × cross_zscore(factor_i)
```

---

## Portfolio Construction

- **Rebalancing**: Every 120 trading days (~semi-annual) with a 1.5× buffer zone (enter at rank ≤ 30, exit at rank > 45) to reduce turnover from boundary churn.
- **Market timing**: Proxy index (liquid universe median) vs. 60-day MA. In bear regime: stop buying new positions, still exit losers. Weights stay fixed at 1/N — avoids the turnover spike that arises from scaling all positions simultaneously.
- **Risk parity** (optional): `use_risk_parity=True` in `build_positions()` — weights proportional to inverse 60-day volatility.

---

## Walk-Forward Validation

`strategy/walk_forward.py` implements expanding-window out-of-sample testing:

- Train on 2015–(year-1), compute per-factor ICIR
- Apply ICIR weights on `year` (OOS), no look-ahead
- Repeat for 2020–2025, concatenate OOS equity curves
- Output: `reports/walk_forward_results.json` + `reports/walk_forward_results.md`

---

## Installation

```bash
git clone https://github.com/casper1126/taiwan-quant-research.git
cd taiwan-quant-research
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements1.txt
```

Set environment variables:

```bash
export FINMIND_TOKEN=<your_token>       # finmindtrade.com — free registration
export LINE_NOTIFY_TOKEN=<your_token>   # notify-bot.line.me — optional
export NOTION_TOKEN=<your_token>        # notion.so/my-integrations — optional
export NOTION_DATABASE_ID=<id>          # optional
```

---

## Usage

```bash
# Step 1: Load raw CSVs into SQLite
python run.py --step 1

# Step 1b: Download institutional investor data from FinMind
python run.py --step 1b

# Step 2: Run multi-factor backtest
python run.py --step 2

# Step 3: Walk-Forward OOS validation
python run.py --step 3

# Utilities
python run.py --stats              # Database row counts
python run.py --verify 2330        # Single-stock data integrity check

# Daily signal update (also triggered by GitHub Actions at 14:30 TWN)
python automation/daily_update.py
```

---

## Automation

GitHub Actions (`.github/workflows/daily_quant.yml`) runs every trading day at 14:30 Taiwan time:
1. Restores SQLite DB from Actions cache
2. Fetches incremental price + valuation data from FinMind
3. Computes today's factor scores and selects holdings
4. Saves `signals/YYYY-MM-DD.json` and commits to repo
5. Sends LINE Notify push and logs to Notion database

Required GitHub Secrets: `FINMIND_TOKEN`, `LINE_NOTIFY_TOKEN`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`.

---

## Limitations & Future Work

- **Survivorship bias**: Universe derived from currently available tickers in FinMind. Delisted stocks may be partially missing, slightly inflating historical returns.
- **Market capacity**: At ~376% annual turnover with 30 stocks, realistic capacity is limited before market impact degrades returns.
- **Factor strength**: No factor achieves ICIR > 0.5, indicating modest predictive power. Adding institutional flow data and ML-based combination (LightGBM / Qlib) are natural next steps.
- **Transaction cost model**: Slippage fixed at 0.1% — understates impact for less liquid names near the boundary of the top-300 universe.

---

## Academic References

1. Fama, E. F., & French, K. R. (1992). The cross-section of expected stock returns. *Journal of Finance*, 47(2), 427–465.
2. Jegadeesh, N., & Titman, S. (1993). Returns to buying winners and selling losers. *Journal of Finance*, 48(1), 65–91.
3. George, T. J., & Hwang, C. Y. (2004). The 52-week high and momentum investing. *Journal of Finance*, 59(5), 2145–2176.
4. Frazzini, A., & Pedersen, L. H. (2014). Betting against beta. *Journal of Financial Economics*, 111(1), 1–23.
5. Asness, C. S., Moskowitz, T. J., & Pedersen, L. H. (2013). Value and momentum everywhere. *Journal of Finance*, 68(3), 929–985.
6. Hou, K., Xue, C., & Zhang, L. (2020). Replicating anomalies. *Review of Financial Studies*, 33(5), 2019–2133.
7. Harvey, C. R., Liu, Y., & Zhu, H. (2016). … and the cross-section of expected returns. *Review of Financial Studies*, 29(1), 5–68.
8. Stambaugh, R. F., Yu, J., & Yuan, Y. (2012). The short of it: Investor sentiment and anomalies. *Journal of Financial Economics*, 104(2), 288–302.
9. Lin, H., & Yeh, C. (2021). Institutional herding and momentum in the Taiwan stock market. *Pacific-Basin Finance Journal*, 67, 101549.
10. Qlib Team, Microsoft (2021). Qlib: An AI-oriented quantitative investment platform. *arXiv:2009.11189*.
