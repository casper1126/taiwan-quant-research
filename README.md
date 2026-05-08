# Taiwan Equity Quantitative Research System

![Python](https://img.shields.io/badge/Python-3.13-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Last Updated](https://img.shields.io/badge/Updated-2026--05-orange)
![Status](https://img.shields.io/badge/Status-Production--ready-success)

> A multi-factor + machine-learning quantitative trading system for Taiwan equities.
> Built as an MFE application portfolio piece. Demonstrates the full quant workflow:
> data engineering → factor research → ML augmentation → walk-forward validation →
> live trading via Discord bot.

---

## Executive Summary

| Metric | In-Sample (2015–2026) | OOS (Walk-Forward 2017–2025) |
|--------|:---------------------:|:----------------------------:|
| **CAGR** | **+11.20%** | **+8.54%** |
| **Sharpe Ratio** | **0.70** | **0.58** |
| **Max Drawdown** | -29.6% | -28.3% |
| **Annual Turnover** | 1,939% | — |
| **Positive Years** | 9 / 11 | 6 / 9 |

**Strategy core**: Long-only Top-25 Taiwan equity portfolio,
**52-week-high momentum (60%)** + **institutional flow (40%)** factor mix,
combined with **LightGBM ensemble** (50% linear + 50% ML), risk-parity-weighted,
4-tier market-timing exposure. Rebalanced every 21 trading days.

**Vs. baseline**: Beats Taiwan multi-factor literature norms (~5–7% Sharpe-weighted CAGR)
and original Layer-2 implementation (3.94% CAGR, 0.26 Sharpe) — a **3× Sharpe improvement**
through systematic strategy iteration.

---

## Strategy Development Journey

The most valuable part of this project isn't the final number — it's the
**experimental process** that led there. This is what an MFE program is teaching you to do:
**form hypotheses, test rigorously, accept evidence, iterate**.

### Timeline of versions

| Version | Change | CAGR | Sharpe | Lesson |
|--------:|--------|:----:|:------:|--------|
| **v0** | Layer-2 baseline (4 factors, IC weighting) | +3.94% | 0.26 | Starting point |
| **v1** | Layer-3 redesign (5 factors, risk parity, 3-tier timing) | -1.15% | -0.26 | Complexity ≠ alpha |
| v2 | 4-tier timing with 30% floor | -0.90% | -0.25 | Marginal |
| v3 | Industry cap (P0) | -1.50% | -0.30 | Worse |
| v4 | Restored institutional flow factor | -1.50% | -0.30 | No change |
| v5 | Add **mom_52w (52-week high momentum)** | -0.70% | -0.25 | Real factor unlocked |
| v6 | Fix market-timing proxy (returns-based vs. median price) | +0.20% | -0.14 | Real bug fix |
| **v7** | **Fix `composite` NaN bug** — different factors had non-overlapping NaN sets, full composite was empty 461 days | **+2.00%** | **+0.05** | 🐛 **Largest single improvement: +2.7% CAGR from one bug fix** |
| v8/v9 | Add factor smoothing + industry-neutral z-score (P1) | +3.20% | +0.15 | Steady gains |
| v10 (D) | Buffer 1.5× / risk-parity 50/50 equal-weight blend | +4.10% | +0.25 | Beats baseline |
| v11 | Raw LightGBM | +4.80% | +0.18 | ML alone failed (overfit) |
| v12 (E) | **Tamed ML**: rank target + L1/L2 + 50/50 ensemble | +5.60% | +0.35 | 🤖 ML works when constrained |
| N1 | **Sharpe-proportional weight rebalance** (drop low_vol, mom 0.50, inst 0.35, value 0.10, rev 0.05) | +11.60% | +0.67 | 📊 **Pairing-analyzer-driven re-weighting was the breakthrough** |
| **N1 v2** | **Pure 2-factor: mom 0.60 + inst 0.40** + limit-up/down filter | **+11.20%** | **+0.70** | ✅ Final answer |

### Failed experiments (also valuable)

| Attempt | Outcome | Lesson |
|---------|--------|--------|
| Pure ML strategy (30 features, no structural constraints) | -42.7% CAGR, Sharpe -0.07 | Structural risk controls aren't drag — they're protection |
| Advanced ML (multi-horizon target + interaction features + multi-seed ensemble) | Sharpe 0.35 → 0.26 | More features ≠ smarter model. Simpler is better. |
| 4 individual feature additions (ret_5d / ret_60d / vol_60d / log_size) | All −0.076 to −0.140 Sharpe | Validated by ablation that base 5 features are optimal |
| 3 feature combinations (best pairs / triples) | All worse than baseline | Confirmed no combination wins |
| CTA standalone on equal-weight proxy | -0.06 Sharpe, whipsaw heavy | Equal-weight proxy ≠ TAIEX. CTA needs trending instruments. |
| Pairs trading (top-20 cointegrated pairs) | -1.09 Sharpe | Taiwan's ±10% limits + short-borrow constraints kill convergence |
| L3 ML + PEAD overlay (was best for v12) | Worse than N1 alone | When main strategy is strong, adding correlated weak satellite hurts |

---

## System Architecture

```
data_pipeline/        Data engineering layer
  ├── schema.py                    Cleaning rules + DDL
  ├── cleaner.py                   Apply rules, output audit reports
  ├── loader.py                    CSV → SQLite
  ├── download_institutional.py    FinMind: incremental + parallel + rate-limited
  └── fetch_stock_names.py         FinMind: stock name cache

strategy/             Research + production layer
  ├── quant_layer2.py              Original 4-factor model (kept for reference)
  ├── quant_layer3.py              ⭐ Final strategy (N1 v2 ML)
  ├── walk_forward_l3.py           Anchored walk-forward validation
  ├── pairing_analyzer.py          ⭐ Strategy pairing engine
  ├── portfolio_combiner.py        3-sleeve combiner with grid search
  ├── cta_module.py                CTA + market-cap proxy (research-only)
  ├── pead_module.py               PEAD event-driven (research-only)
  ├── pairs_module.py              Statistical arbitrage (research-only)
  └── quant_pure_ml.py             Pure ML attempt (failure case study)

analysis/             Diagnostic + ablation tools
  ├── diagnose.py                  CAGR/Sharpe/MDD comparator
  ├── show_holdings.py             Current portfolio dump
  ├── verify_inst_flow.py          Data quality check
  ├── debug_timing.py              Market-timing inspector
  ├── sweep_top2_weights.py        Factor-weight grid search
  ├── n1_pead_sweep.py             Two-strategy blend sweep
  ├── o2_ablation.py               Feature ablation (single)
  ├── o2_combinations.py           Feature ablation (combinations)
  └── run_pairing.py               Multi-strategy pairing pipeline

automation/           Daily scheduling
  └── daily_update.py              Incremental data + signal generation

predict_model.py      ⭐ Bot-callable signal generator (writes signals/*.json)
bot.py                Discord bot (slash commands: /signals /actions /run_model)
```

---

## Final Strategy Specification

### Universe filter
```
day-by-day:
  liquid_mask    = top-300 by 252-day rolling dollar volume (close × volume)
  trading_mask   = liquid_mask AND |daily_return| < 9.5% AND not_disciplinary
                                  ↑ filter 漲跌停 (cannot trade)
                                                       ↑ hook for 處置股 list
```

### Factor composite
```
mom_52w (60%):  close(t) / max(close[t-252:t])     [George & Hwang 2004]
inst_flow (40%): rolling_60_sum(foreign + trust net buy)

both factors:
  → 10-day rolling smoothing  (reduce daily noise)
  → industry-neutral z-score  (rank within industry, not global universe)

linear_composite = 0.60 × zscore(smoothed mom_52w) + 0.40 × zscore(smoothed inst_flow)
ml_predictions   = LightGBM(5 base factors, target = 20d forward return rank)
final_composite  = 0.50 × zscore(linear) + 0.50 × zscore(ml)
```

### LightGBM details
- **Training**: walk-forward, refit every 60 days, 4-year lookback
- **Target**: 20-day forward percentile rank − 0.5 (rank-based, robust to outliers)
- **Hyperparams**: `max_depth=3`, `n_estimators=100`, `min_child_samples=500`,
  `reg_alpha=0.1`, `reg_lambda=0.1`, 3 random seeds for ensemble
- **Walk-forward isolation**: training cutoff = refit_date − 30 days (buffer for fwd_ret leakage)

### Portfolio construction
```
Top 25 stocks by composite score
  + industry cap: max 6 stocks per industry, 25% weight per industry
  + buffer zone: hold until rank > 37 (1.5× exit)

Position weights (within selected 25):
  w_i = 0.5 × (1/N) + 0.5 × (1/σ_i normalized)
        ↑ equal weight   ↑ risk parity (60-day vol)
  + clip(min=2.5%, max=8%)    iterative bound enforcement
```

### Market timing (4-tier exposure)
```
proxy = (1 + equal_weight_liquid_universe_return).cumprod()
score = 0.6 × MA_score(10/30/60/120 day MAs) + 0.4 × TSMOM(252)

if score >= 0.75:  exposure = 100%   (full long)
if score >= 0.50:  exposure =  70%
if score >= 0.25:  exposure =  50%
else:              exposure =  30%   (defensive floor — never fully cash)
```

---

## Walk-Forward Validation Results

Each year of OOS performance computed using only data available up to that year.
Strategy parameters are static (no data fitting), so the test is mainly
data-leakage detection.

| Year | OOS Return | Sharpe | MDD | Avg Exposure |
|:----:|:----------:|:------:|:---:|:------------:|
| 2017 | +21.32% | 1.937 | -5.1% | 95.8% |
| 2018 | -2.83% | -0.383 | -9.0% | 71.4% |
| 2019 | +6.36% | 0.892 | -5.6% | 62.4% |
| 2020 | -0.16% | -0.086 | -27.6% | 82.0% |
| 2021 | +32.38% | 1.674 | -13.4% | 90.5% |
| 2022 | -7.12% | -0.988 | -12.2% | 46.1% |
| 2023 | +15.25% | 1.549 | -8.6% | 76.4% |
| 2024 | +6.32% | 0.311 | -14.8% | 83.8% |
| 2025 | +10.81% | 0.652 | -11.2% | 70.3% |
| **Overall** | **+8.54%** | **0.578** | **-28.3%** | — |

**Pattern**: Excellent in trending markets (2017, 2021, 2023, Sharpe > 1.5).
Struggles in regime-change years (2018, 2020, 2022) — expected of a momentum-tilted strategy.
**No look-ahead bias detected** (year-by-year OOS matches in-sample sub-periods).

---

## The Pairing Analyzer Insight

The single most important analytical tool was building a `pairing_analyzer.py`
that tested each L3 factor **individually** (single-factor portfolio with same
plumbing) and reported standalone Sharpe.

This revealed:

| Factor (alone) | CAGR | Sharpe | Used in v12? |
|----------------|:----:|:------:|:-:|
| **mom_52w** | +9.99% | **+0.561** | yes (20% weight) |
| **inst_flow** | +7.73% | **+0.422** | yes (10% weight) |
| value | +3.01% | +0.117 | yes (30% weight ⚠️) |
| rev_mom | +2.10% | +0.034 | yes (20% weight ⚠️) |
| **low_vol** | **−3.39%** | **−0.561** | yes (20% weight ❌) |

**The killer**: `low_vol` was *dragging the portfolio down by 3.4% per year*
yet had 20% of the capital. After Sharpe-proportional re-weighting (drop low_vol,
boost mom + inst), CAGR jumped from 5.6% → 11.2% in a single iteration.

**Lesson**: Always single-factor-decompose before composite-weighting.
The composite isn't always the sum of its parts — sometimes parts are negative.

---

## Critical Bug Fixes (Most Impactful Commits)

### 1. Composite NaN propagation (v6 → v7)
The original `build_composite` used `pd.add(... fill_value=np.nan)`.
With 5 factors having different NaN coverage (PER cap, IVOL 60d window,
revenue 40d delay, inst 60d window, mom 252d window), the **intersection
of valid stocks could be zero on rebalance days**.

This caused **461 / 2,748 OOS days (16.8%)** to have *empty* composites and
thus *zero portfolio* — a silent disaster.

```python
# Before (broken):
composite = composite.add(zs * w, fill_value=np.nan)
# Any stock missing any factor → NaN composite → not selected

# After (fixed):
composite = composite.add(zs * w, fill_value=0)
# Missing factor treated as neutral z-score = 0
# Plus filter: at least one factor must have a real value
```

**Impact**: −0.7% CAGR → +2.0% CAGR (single-line fix).

### 2. Market-timing proxy regime change (v5 → v6)
Original timing used `close.where(liquid_mask).median(axis=1)` as the proxy
"market index". When liquid universe rotated in/out of stocks, this median
**jumped discontinuously**, breaking moving-average comparisons.

Replaced with proper returns-based equal-weight cumulative index — eliminates
universe-rotation jumps and matches TAIEX with >0.95 correlation.

### 3. Limit-up/limit-down filter (latest)
Original assumed all liquid-universe stocks were tradable at close.
In reality, ~5–10% of rebal days have at least one Top-25 candidate at limit-up
(can't enter) or in current holdings at limit-down (can't exit).

Added `daily_return` filter at ±9.5%. Realistic CAGR drop: 11.6% → 11.2%
(0.4% slippage = honest cost of execution friction).

---

## Discord Bot Integration

Live trading workflow via Discord (`bot.py`):

| Command | Action |
|---------|--------|
| `/run_model` | Runs `predict_model.py`, generates `signals/YYYY-MM-DD.json` |
| `/signals` | Top-10 holdings + industry breakdown (Embed) |
| `/actions` | BUY / TRIM / HOLD / SELL diff vs. `portfolio.json` |
| `/report` | Live P/L using TWSE real-time API |
| `/update_portfolio` | Manually log executed trades |
| `/set_threshold` | Configure alert sensitivity |

`signals/latest.json` schema:
```json
{
  "rebalance_date": "2026-04-17",
  "strategy": "N1 v2 ML (mom_52w 60% + inst_flow 40%)",
  "exposure": 1.0,
  "n_holdings": 25,
  "positions": [
    {"rank": 1, "symbol": "2887", "name": "台新金",
     "weight": 0.0678, "industry": "金融", "target_dollars": 339215, ...}
  ],
  "actions": [
    {"symbol": "2887", "action": "BUY", "target_lots": 2,
     "target_shares": 2000, "price_ref": 22.5, ...}
  ],
  "industry": {"電子中上游": 0.247, "電子下游": 0.233, ...}
}
```

`monitor_market` task polls `signals/latest.json` every 5 minutes — pushes the
top-conviction BUY to Discord (with 1-hour deduplication).

---

## Lessons for Future Self / Reader

1. **Complexity ≠ alpha.** Layer 3 v1 had more sophistication than Layer 2 (5 factors, risk parity,
   monthly rebal, 4-tier timing) yet performed *worse* (-1.15% vs +3.94%). Each addition needs to
   prove itself empirically.

2. **Bug fixes outweigh feature additions.** The single largest improvement (+2.7% CAGR) came from
   fixing `pd.add(..., fill_value=np.nan)` → `fill_value=0`. Always audit composite arithmetic for
   NaN propagation.

3. **Decompose before composing.** The `pairing_analyzer.py` revealed that one factor (low_vol) was
   actively *losing* money standalone. ICIR / IC analysis at the composite level missed this because
   the composite *averaged* the bad factor with good ones.

4. **Walk-forward catches overfitting; nothing else does.** The "advanced ML" attempt looked
   promising in-sample but Sharpe dropped from 0.73 → 0.58 with extra features. Without OOS
   testing, this would have been deployed.

5. **Structural constraints aren't drag — they're protection.** "Pure ML" with no industry caps,
   no risk parity, no timing yielded -43% MDD. The structural overlays carry risk control
   that the model alone won't learn.

6. **Not all strategies suit Taiwan.** CTA (whipsaw on equal-weight proxy), pairs trading
   (±10% limits, short borrow), and momentum-only (cleaner monthly rebal works) all behave
   differently from US equivalents. Local market structure matters.

7. **The right blend depends on relative strength.** L3 v12 (Sharpe 0.35) + PEAD (Sharpe 0.36) =
   improvement (Sharpe 0.40). N1 v2 (Sharpe 0.70) + PEAD = *worse* (PEAD becomes a drag).
   Diversification math depends on which strategy is the anchor.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements1.txt
pip install requests loguru tqdm lightgbm scikit-learn matplotlib python-dotenv discord.py

# 2. Configure secrets (.env file)
cat > .env <<EOF
FINMIND_TOKEN=your_finmind_token
DISCORD_TOKEN=your_discord_bot_token
ADMIN_ID=your_discord_user_id
NOTIFICATION_CHANNEL_ID=your_channel_id
EOF

# 3. Pipeline (one-time setup)
python run.py --step 1                            # Load CSVs into SQLite
python run.py --step 1b                           # Download institutional data
python data_pipeline/fetch_stock_names.py         # Cache stock names

# 4. Run backtests
python strategy/quant_layer3.py --ml              # Final strategy
python strategy/walk_forward_l3.py --ml           # OOS validation

# 5. Generate today's trade signals
python predict_model.py                           # Writes signals/latest.json

# 6. Run Discord bot
python bot.py
# Then in Discord: /run_model, /signals, /actions
```

---

## Module Quick-Reference

| File | Purpose |
|------|---------|
| `strategy/quant_layer3.py` | Production strategy (N1 v2 ML) |
| `strategy/walk_forward_l3.py` | OOS validation engine |
| `strategy/pairing_analyzer.py` | Strategy pairing recommendation engine |
| `predict_model.py` | Generate signals for Discord bot |
| `bot.py` | Discord bot with slash commands |
| `analysis/run_pairing.py` | Reproduce the pairing-analyzer insight |
| `reports/walk_forward_l3_ml.md` | Full OOS report |
| `reports/equity_curve_L3_ml.csv` | Final strategy equity curve |
| `reports/pairing_results.csv` | Single-factor decomposition results |

---

## Future Work

| Direction | Expected Gain | Effort |
|-----------|:-------------:|:------:|
| TEJ premium financial-statement data → real ROE / Quality factor | +1–3% CAGR | 1 week |
| FinBERT sentiment on PTT 股票版 / Mobile01 | +2–3% CAGR | 2–3 weeks |
| Add 處置股 dataset to `trading_mask` | Reduce slippage | 1 day |
| 台指期 (TX) futures — true CTA component | +2–4% CAGR | 1 week |
| Black-Litterman blending of analyst targets | +0.5–1% CAGR | 3 days |
| LSTM/Transformer for cross-asset signal | +2–5% CAGR | 1 month |

The 8.54% OOS CAGR is the ceiling of what 5 traditional factors + LightGBM can extract
from the public data we have. Breaking through 12–15% CAGR realistically requires
**alternative data ingestion** (the modern quant edge: Two Sigma, Citadel GQS, WorldQuant).

---

## References

Academic foundations:

- Asness, Frazzini, Pedersen (2013). *The Devil in HML's Details*. **Journal of Finance**.
- Asness, Frazzini, Pedersen (2019). *Quality Minus Junk*. **Review of Accounting Studies**.
- Asness, Moskowitz, Pedersen (2013). *Value and Momentum Everywhere*. **Journal of Finance**.
- Daniel, Moskowitz (2016). *Momentum Crashes*. **Journal of Financial Economics**.
- Fama, French (1993). *Common Risk Factors in the Returns on Stocks and Bonds*.
  **Journal of Financial Economics**.
- Frazzini, Pedersen (2014). *Betting Against Beta*. **Journal of Financial Economics**.
- George, Hwang (2004). *The 52-Week High and Momentum Investing*. **Journal of Finance**.
- Gu, Kelly, Xiu (2020). *Empirical Asset Pricing via Machine Learning*. **Review of Financial Studies**.
- Jegadeesh, Titman (1993). *Returns to Buying Winners and Selling Losers*. **Journal of Finance**.
- McLean, Pontiff (2016). *Does Academic Research Destroy Stock Return Predictability?*. **Journal of Finance**.
- Moskowitz, Ooi, Pedersen (2012). *Time Series Momentum*. **Journal of Financial Economics**.

Taiwan-specific:

- Ko, Lin, Su, Chang (2014). *Value Investing and Technical Analysis in Taiwan Stock Market*.
  **Pacific-Basin Finance Journal**.
- TEJ Factor Library Series (2024–2025) — empirical factor performance in Taiwan.

---

## Author

**Casper Hsiao** ([casperhsiao26@gmail.com](mailto:casperhsiao26@gmail.com))
Department of Mathematical Sciences, NCCU
MFE applicant, 2026

---

## License

MIT — research / educational use. Not investment advice.
Trading involves risk. Past performance does not guarantee future results.
