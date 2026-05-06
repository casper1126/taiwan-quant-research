# Daily Automation Setup Guide

## Overview

The system can **automatically download and process data every day at 14:30 Taiwan time (06:30 UTC)** using GitHub Actions. This includes:

1. **Step 1a**: Incremental price & valuation data from FinMind
2. **Step 1b**: Incremental institutional investor data (籌碼) with quota-aware rate limiting
3. **Step 2**: Calculate daily trading signals
4. **Step 3**: Save signals to `signals/YYYY-MM-DD.json`
5. **Step 4**: Send notifications via LINE + Notion

---

## Option 1: GitHub Actions (Fully Automatic) ✅ RECOMMENDED

### Prerequisites

1. Push this repo to GitHub
2. Set up GitHub Secrets in your repo settings:
   - `FINMIND_TOKEN`: Your FinMind API token
   - `LINE_NOTIFY_TOKEN`: (Optional) LINE Notify token for alerts
   - `NOTION_TOKEN`: (Optional) Notion API token for logging
   - `NOTION_DATABASE_ID`: (Optional) Notion database ID

### How It Works

- **Trigger**: Every weekday at 14:30 Taiwan time (06:30 UTC)
- **Database Persistence**: SQLite DB is cached between runs
- **Signal Storage**: `signals/YYYY-MM-DD.json` is automatically committed back to the repo
- **Failure Notifications**: If something fails, LINE alert is sent (if token configured)

### Configuration

The workflow file is already configured in `.github/workflows/daily_quant.yml` with:
- ✅ Automatic cron schedule (14:30 Mon-Fri Taiwan time)
- ✅ Python 3.11 setup
- ✅ Dependencies installation
- ✅ Database cache persistence
- ✅ Steps 1a, 1b, 2, 3, 4 in sequence
- ✅ Results commit back to repo

**Nothing else to configure!** Just set the GitHub Secrets and you're done.

---

## Option 2: Local Cron Job (For Your Own Server)

If you prefer to run this on your local machine or server:

### Step 1: Set Environment Variable

```bash
export FINMIND_TOKEN="your_actual_token_here"
export LINE_NOTIFY_TOKEN="your_token"  # Optional
export NOTION_TOKEN="your_token"       # Optional
export NOTION_DATABASE_ID="your_db_id" # Optional
```

Save to `~/.bashrc` or `~/.zshrc` to make it permanent:

```bash
echo 'export FINMIND_TOKEN="your_token"' >> ~/.bashrc
source ~/.bashrc
```

### Step 2: Create Cron Job

Open crontab:

```bash
crontab -e
```

Add this line for daily updates at 14:30 Taiwan time:

```bash
30 6 * * 1-5 /path/to/venv/bin/python /path/to/Quant_Trading_System/automation/daily_update.py
```

**Explanation**:
- `30 6`: 06:30 UTC = 14:30 Taiwan time (UTC+8)
- `* * 1-5`: Every Monday-Friday
- `/path/to/venv/bin/python`: Full path to Python in your venv
- `/path/to/daily_update.py`: Full path to the script

### Step 3: Verify

List your cron jobs:

```bash
crontab -l
```

Check the system log for execution:

```bash
log stream --predicate 'eventMessage contains "daily_update"' --level debug
```

---

## Option 3: Manual Execution

You can also run the daily update manually anytime:

```bash
cd /path/to/Quant_Trading_System
source venv/bin/activate
export FINMIND_TOKEN="your_token"
python automation/daily_update.py
```

---

## Monitoring & Troubleshooting

### Check Latest Signals

```bash
cat signals/$(date +%Y-%m-%d).json | jq .
```

### View Database Status

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/taiwan_stock.db')
cursor = conn.cursor()
cursor.execute('SELECT name, COUNT(*) FROM sqlite_master WHERE type=\"table\" GROUP BY name')
for table, count in cursor.fetchall():
    print(f'{table}: {count} rows')
"
```

### Check Institutional Investor Data

```bash
python -c "
import sqlite3
conn = sqlite3.connect('data/taiwan_stock.db')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(DISTINCT date) FROM institutional_investors')
dates = cursor.fetchone()[0]
cursor.execute('SELECT MAX(date) FROM institutional_investors')
latest = cursor.fetchone()[0]
print(f'Institutional investor data: {dates} days, latest: {latest}')
"
```

### Quota Status

The rate limiter automatically logs quota status. Check for log messages like:

```
2026-05-02 14:35:12 | INFO | API 配額已滿，自動暫停 3600 秒
```

If you see this, the system is working correctly — it paused to wait for quota recovery.

---

## Advanced Options

### Parallel Download (For Full Re-download)

If you want faster initial setup, use parallel workers:

```bash
export FINMIND_TOKEN="your_token"
python run.py --step 1b --force --workers 4
```

This is **not** recommended for daily cron (use 1 worker to be safe), but useful for:
- Initial setup/backfilling
- Weekend offline catch-up

### Filter by Stock

Download only specific stocks:

```bash
python run.py --step 1b --sid 2330,2454  # TSMC, MediaTek only
```

---

## API Quota Management

**FinMind Free Tier**: 600 API calls/hour

**Current System Usage**:
- **Step 1a** (price/valuation): ~2,100 calls/day for incremental update (~10 minutes)
- **Step 1b** (institutional): ~2 calls/day for incremental update (~1 second)

**Total Daily**: ~2,100 API calls = 3.5x hourly quota

✅ **Design ensures we never exceed hourly limits** through smart queuing:
- Requests are rate-limited via sliding window
- Dynamic delays increase as we approach 95% quota
- When quota is exceeded, system automatically waits until it recovers

---

## FAQ

**Q: What if my FINMIND_TOKEN is invalid?**
A: Step 1b will fail gracefully (log warning) and not halt the rest of the pipeline. Other steps continue normally.

**Q: Can I disable Step 1b?**
A: Yes, edit `automation/daily_update.py` and comment out the `incremental_institutional_update()` call.

**Q: How much disk space does the DB grow per day?**
A: ~5-10 MB/day depending on number of stocks and market activity.

**Q: Can I change the daily run time?**
A: Yes:
- GitHub Actions: Edit `.github/workflows/daily_quant.yml`, change `cron` line
- Local cron: Edit your crontab (e.g., `30 8 * * 1-5` for 16:30 Taiwan time)

**Q: What happens if the script crashes?**
A: 
- GitHub Actions: Failed action is logged, LINE alert sent (if configured)
- Local cron: Check `crontab` output file (usually mailed to you)
- Database is persisted — next run will resume from checkpoint

---

## Next Steps

1. **For GitHub Actions**: Set secrets, then watch Actions tab for automatic runs
2. **For Local Cron**: Add cron job and monitor via system logs
3. **Monitor**: Check `signals/` folder daily and database growth

Enjoy hands-free daily automation! 🚀
