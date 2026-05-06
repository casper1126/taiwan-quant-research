# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Full pipeline: load CSVs into SQLite, then run backtest
python run.py

# Step 1 only: load raw CSV data into SQLite
python run.py --step 1 --dir raw_data

# Re-load and overwrite existing data
python run.py --step 1 --overwrite

# Step 1b: Download institutional investor data (incremental, parallel-aware)
python run.py --step 1b
python run.py --step 1b --workers 4          # Use 4 parallel workers
python run.py --step 1b --force              # Force re-download (ignores checkpoint)
python run.py --step 1b --sid 2330           # Download only TSMC
python run.py --step 1b --sid 2330 --workers 2

# Direct download (same as --step 1b, useful for cron/automation)
export FINMIND_TOKEN="your_token"
python data_pipeline/download_institutional.py
python data_pipeline/download_institutional.py --workers 4
python data_pipeline/download_institutional.py --force

# Step 2 only: run multi-factor backtest
python run.py --step 2

# Step 3 only: run walk-forward validation
python run.py --step 3

# Utility: print database row counts per table
python run.py --stats

# Utility: verify a single stock's data and show quick stats
python run.py --verify 2330

# Run daily update manually (incremental fetch from FinMind + generate signals)
# Includes Step 1a (price/valuation) + Step 1b (institutional investor data, auto quota-managed)
export FINMIND_TOKEN="your_token"
python automation/daily_update.py

# Set up cron for daily updates (e.g., 14:30 Taiwan time)
# 30 6 * * 1-5 /path/to/venv/bin/python /path/to/daily_update.py

# Run backtest engine directly (standalone)
python strategy/quant_layer2.py
```

Install dependencies:
```bash
pip install -r requirements1.txt
pip install requests loguru tqdm
```

## Architecture

The system is a Taiwan equity quant pipeline with five layers:

### 1. Data Pipeline (`data_pipeline/`)
- **`schema.py`** — single source of truth for SQLite DDL, column types (`PRICE_COLS`, `VAL_COLS`, `REV_COLS`), and `CLEANING_RULES`. All cleaning logic is declared here as `CleaningRule` dataclasses; no hardcoded business rules elsewhere.
- **`cleaner.py`** — applies the rules from `schema.py` to raw DataFrames, returning a cleaned DataFrame and a `CleaningReport` with per-rule audit counts. Cleaning order matters: zero-close/zero-volume rows are dropped *before* OHLC logic checks.
- **`loader.py`** — `CSVLoader` class scans `raw_data/` for files matching three naming patterns (`{id}_{start}_{end}.csv`, `{id}_rev.csv`, `{id}_val.csv`), cleans each with `cleaner.py`, and upserts into SQLite. Also writes to `load_manifest` and `data_quality_log` tables. `read_merged()` is the standard input interface for the strategy layer: it joins price + valuation + revenue, forward-filling monthly revenue with a 40-day publication delay to prevent look-ahead bias.

SQLite database lives at `data/taiwan_stock.db`. Tables: `daily_price`, `daily_valuation`, `monthly_revenue`, `load_manifest`, `data_quality_log`.

### 1b. Live Institutional Data (`data_pipeline/download_institutional.py`)
Downloads three major investor categories (foreign investors + investment trusts) from FinMind API with **incremental, parallel, and rate-limited** logic:

- **Incremental checkpoint**: queries `MAX(date)` per stock to resume from the next day — avoids re-downloading the entire history
- **Parallel download**: `ThreadPoolExecutor` with configurable `--workers` (default 1, recommended 2–4) to speed up without hitting rate limits
- **Batch upsert**: validates, deduplicates, and writes only clean rows to `institutional_investors` table
- **Robust error handling**: retries with exponential backoff; graceful handling of 402 rate limits and network timeouts
- **Detailed logging**: tracks per-stock fetch status, total rows inserted, elapsed time, API efficiency

Table: `institutional_investors(date, stock_id, investor_type, buy, sell, net)`. Used by the backtester as a potential factor input (pending feature integration into `quant_layer2.py`).

### 2. Strategy Layer (`strategy/quant_layer2.py`)
A four-factor long-only backtest engine using wide-format matrices (index=date, columns=stock_id):

- **Universe**: top-300 stocks by 252-day avg price (market-cap proxy), profitable only (PER > 0)
- **Factors** (IC-weighted composite):
  - 52-week high ratio (momentum, 20%) — George & Hwang (2004)
  - 1/PER (value, 30%)
  - Revenue YoY with 40-day lag (25%)
  - Inverse 60-day volatility (low-vol anomaly, 25%)
- **Rebalancing**: every 120 trading days with a buffer zone (exit threshold = 1.5× entry threshold) to reduce turnover
- **Market timing**: binary in/out based on proxy index vs. 60-day MA; applied at rebalance time to avoid daily position drift
- **Backtest**: vectorized (matrix multiply), includes commission (0.1425%), tax (0.3% sell-only), and slippage (0.1%)
- Output: `reports/equity_curve.csv`

### 3. Walk-Forward Validation (`strategy/walk_forward.py`)
Out-of-sample cross-validation using rolling training/test windows to verify strategy robustness and detect overfitting.

### 4. Automation (`automation/`)
- **`daily_update.py`** — incremental data fetch from FinMind API (only dates after the DB's latest date), **includes Step 1b institutional investor download (quota-aware, auto-resuming)**, factor calculation for today's cross-section, saves signals to `signals/YYYY-MM-DD.json`, then calls `notifier.py`
- **`notifier.py`** — sends LINE Notify messages and creates Notion database records; tokens read from env vars `LINE_NOTIFY_TOKEN`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`

### 5. GitHub Actions (`.github/workflows/daily_quant.yml` → stored in `github/workflows/`)
Runs `daily_update.py` at 14:30 Taiwan time (06:30 UTC) Mon–Fri. **Flow: Step 1a (price/valuation) → Step 1b (institutional investors) → Step 2 (signals) → Step 3 (save) → Step 4 (notify)**. Persists the SQLite DB across runs using GitHub Actions cache (key rotates daily). Commits `signals/*.json` back to the repo. Secrets required: `FINMIND_TOKEN`, `LINE_NOTIFY_TOKEN`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`.

## Key Design Decisions

**Look-ahead bias prevention**: Monthly revenue dates in FinMind are labeled as the 1st of the reporting month, but announcements happen ~40 days later. All revenue-derived factors shift their index by `DateOffset(days=40)` before forward-filling to daily frequency.

**Wide-format matrices throughout**: All factor computation uses `pivot(index=date, columns=stock_id)` so cross-sectional operations are a single `rank(axis=1)` or `rolling(...).mean()` call.

**Turnover control**: Three mechanisms — binary (not graduated) market timing, larger portfolio (30 stocks), and a buffer zone where stocks only exit when they fall outside 1.5× the entry threshold.

**Incremental 1b layer**: Institutional investor download uses checkpoint-based resumption and parallel workers to minimize API calls and support daily automation without repeatedly re-fetching. **Automatically integrated into `daily_update.py`** as Step 1b, runs daily at 14:30 Taiwan time via GitHub Actions with smart rate-limiting (quota-aware sleep).

**`run.py` path injection**: Uses `sys.path.insert` to add `data_pipeline/`, `strategy/`, and `automation/` so submodules can be imported without package installation.
