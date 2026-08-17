# ⏸ 暫停使用中 — Discord 通知功能

這個資料夾存放暫時停用的 Discord bot 程式碼。停用原因：開發 `CLAUDE_CODE_TASKS.md`
的 Task 1–9 期間先不需要 Discord 通道，之後開發完成再重新串接。程式碼原封不動搬過來，
沒有刪減任何功能。

## 搬移紀錄

| 檔案 | 原始位置 | 現在位置 | 搬移方式 |
|---|---|---|---|
| `bot.py` | repo 根目錄 `./bot.py` | `_disabled/discord/bot.py` | 整檔搬移（887 行，全部都是 Discord 專屬程式碼，沒有部分保留在原地） |

**沒有搬動的東西**（確認過這些不是 Discord 專屬，是 `predict_model.py` 也在用的共用狀態）：
- `predict_model.py` — 留在根目錄。它是實際的訊號產生器（N1 v2 ML 策略），`bot.py` 的
  `/run_model` 指令只是用 `subprocess` 呼叫它；它本身不 import discord，也能獨立執行
  （`python predict_model.py`）。
- `config.json` — 留在根目錄。`account_value`、`strategy` 兩個欄位是 `predict_model.py`
  在用；`notification_channel_id` 欄位是 Discord 專屬但目前留著不影響任何東西（`predict_model.py`
  不會讀這個欄位）。
- `portfolio.json` — 留在根目錄。`predict_model.py` 用它來 diff 出買賣清單，`bot.py` 只是
  順帶讀取顯示，不是 Discord 專屬狀態。
- `.env` 裡的 `DISCORD_TOKEN` / `ADMIN_ID` / `NOTIFICATION_CHANNEL_ID` — 沒有清空或移除，
  本次隔離沒有動 `.env`（`.env` 是本地檔案、不進版控，且清空後之後恢復還要重打一次 token，
  沒有必要）。
- `automation/notifier.py`、`automation/daily_update.py` — 檢查過完全沒有 Discord 相關
  程式碼（只有 LINE + Notion），本來就跟 Discord 無關，不需要動。

## 重新啟用步驟

1. 把檔案搬回根目錄：
   ```bash
   mv _disabled/discord/bot.py ./bot.py
   ```
   `bot.py` 內部用 `BASE_DIR = Path(__file__).parent` 抓相對路徑，搬回根目錄後會自動
   正確指向 `predict_model.py`、`config.json`、`portfolio.json`、`signals/`，不用改路徑。

2. 確認 `requirements1.txt` 有 Discord 套件（隔離前這行本來就沒被正式宣告在
   `requirements1.txt` 裡，只存在於 venv 中，建議順便補上）：
   ```
   discord.py>=2.7.1
   ```
   （venv 裡目前裝的是 `discord.py 2.7.1`，可用 `pip show discord.py` 確認。）

3. 確認 `.env` 有以下三個變數（本次隔離沒有動它們，理論上應該還在）：
   ```
   DISCORD_TOKEN=<你的 bot token>
   ADMIN_ID=<你的 Discord 使用者 ID>
   NOTIFICATION_CHANNEL_ID=<要推播的頻道 ID>
   ```

4. 啟動：
   ```bash
   python bot.py
   ```
   Discord 裡測試 `/signals`、`/actions`、`/run_model` 三個 slash command。

5. 如果 `README.md` 屆時也要恢復 Discord 相關敘述（目前 README 其實還留著 Discord 的
   Overview/安裝說明，本次隔離沒有動 README），對照 `docs/PROJECT_STATUS.md` 的
   Task 9 章節一併處理，因為 Task 9 本來就要整份重寫 README。
