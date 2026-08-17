import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import discord
import requests
from discord import Embed, app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# 讀取 .env
BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")
NOTIFICATION_CHANNEL_ID = os.getenv("NOTIFICATION_CHANNEL_ID", "")

PORTFOLIO_PATH = BASE_DIR / "portfolio.json"
CONFIG_PATH = BASE_DIR / "config.json"
PREDICT_MODEL_PATH = BASE_DIR / "predict_model.py"
SIGNALS_DIR = BASE_DIR / "signals"
LATEST_SIGNAL_PATH = SIGNALS_DIR / "latest.json"

DEFAULT_CONFIG = {
    "threshold": 0.05,
    "notification_channel_id": NOTIFICATION_CHANNEL_ID or "",
}

# 防止同一檔股票在 1 小時內重複發送同一訊號
recent_alerts = {}

intents = discord.Intents.default()
# intents.message_content = True  # 移除 Privileged Intent，使用 Slash Commands 替代
bot = commands.Bot(command_prefix="!", intents=intents)


def load_json(path: Path, default):
    """讀取 JSON，若不存在則建立預設內容。"""
    if not path.exists():
        save_json(path, default)
        return default.copy() if isinstance(default, dict) else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default.copy() if isinstance(default, dict) else default
    except Exception:
        return default.copy() if isinstance(default, dict) else default


def save_json(path: Path, data):
    """將資料寫入 JSON 檔案。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def initialize_files():
    """初始化 portfolio.json 與 config.json。"""
    load_json(PORTFOLIO_PATH, [])
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    if NOTIFICATION_CHANNEL_ID and not cfg.get("notification_channel_id"):
        cfg["notification_channel_id"] = NOTIFICATION_CHANNEL_ID
        save_json(CONFIG_PATH, cfg)
    return cfg


async def notify_admin(message: str):
    """將錯誤或警告發送給管理員。"""
    if not ADMIN_ID:
        return
    try:
        admin = await bot.fetch_user(int(ADMIN_ID))
        if admin:
            await admin.send(f"[Bot Alert] {message}")
    except Exception:
        pass


def is_admin():
    """自訂檢查：是否為管理員。"""
    async def predicate(ctx):
        if not ADMIN_ID:
            return False
        return str(ctx.author.id) == str(ADMIN_ID)
    return commands.check(predicate)


def check_model_signals():
    """
    [DEPRECATED] 舊版「逐檔通知」邏輯。

    本策略是月頻組合策略（25 檔一起 rebalance），不適合逐檔推送。
    改用 /run_model 一次推完整清單（透過 post_signals_summary）。

    保留此函式只為相容既有 monitor_market loop（永遠回 None 不通知）。
    """
    return None


def normalize_twse_symbol(symbol: str) -> str:
    """將輸入格式標準化成證交所股票代號，例如 2330 或 2330.TW。"""
    return str(symbol).strip().upper().replace(".TW", "").replace("TSE_", "").replace("OTC_", "")


def fetch_twse_price(symbol: str) -> float:
    """從台灣證交所即時 API 抓取最新價格。"""
    normalized = normalize_twse_symbol(symbol)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{normalized}.tw&json=1&delay=0"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        quote = data.get("msgArray", [])[0] if data.get("msgArray") else None
        if not quote:
            raise ValueError("無效回傳資料")
        price_str = quote.get("z") or quote.get("z0") or quote.get("z1")
        if not price_str or price_str in ["0", "-", ""]:
            raise ValueError("無法取得即時價格")
        return float(price_str.replace(",", ""))
    except Exception as e:
        raise RuntimeError(f"TWSE 即時價格抓取失敗: {e}")


async def get_notification_channel():
    """取得通報頻道物件。"""
    cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    channel_id = cfg.get("notification_channel_id") or NOTIFICATION_CHANNEL_ID
    if not channel_id:
        return None
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None


@tasks.loop(minutes=5)
async def monitor_market():
    """每 5 分鐘監控模型訊號並主動通知。"""
    try:
        cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        threshold = float(cfg.get("threshold", 0.05))
        result = await asyncio.to_thread(check_model_signals)

        if not result:
            return

        symbol = result.get("symbol")
        signal = result.get("signal")
        confidence = float(result.get("confidence", 0))
        expected_return = float(result.get("expected_return", 0))

        # 超過阈值才通知
        if signal not in ("BUY", "SELL") or expected_return < threshold:
            return

        # 防洗版：同一檔股票同一訊號 1 小時內不重複發送
        now = datetime.utcnow()
        last = recent_alerts.get(symbol)
        if last and last["signal"] == signal and now - last["time"] < timedelta(hours=1):
            return

        channel = await get_notification_channel()
        if channel is None:
            await notify_admin("通知頻道未設定或無效，無法發送市場監控通知。")
            return

        embed = Embed(
            title=f"市場預測交易訊號：{symbol}",
            color=0x00FF00 if signal == "BUY" else 0xFF4500,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="交易建議", value=signal, inline=True)
        embed.add_field(name="預測漲幅", value=f"{expected_return*100:.2f}%", inline=True)
        embed.add_field(name="信心水準", value=f"{confidence*100:.2f}%", inline=True)
        embed.add_field(name="觸發門檻", value=f"{threshold*100:.2f}%", inline=True)
        embed.set_footer(text="Stock Model Alert")

        await channel.send(embed=embed)
        recent_alerts[symbol] = {"signal": signal, "time": now}
    except Exception as e:
        await notify_admin(f"monitor_market 發生錯誤：{e}")


@monitor_market.before_loop
async def before_monitor_market():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")
    initialize_files()
    try:
        synced = await bot.tree.sync()
        print(f"已同步 {len(synced)} 個 Slash Command")
    except Exception as e:
        print(f"同步 Slash Command 失敗：{e}")
    if not monitor_market.is_running():
        monitor_market.start()


@bot.tree.command(name="report", description="回傳持股報告及即時損益")
async def report(interaction: discord.Interaction):
    """回傳持股報告及即時損益。"""
    try:
        portfolio = load_json(PORTFOLIO_PATH, [])
        if not portfolio:
            await interaction.response.send_message("📌 目前投資組合為空，請先使用 /update_portfolio 新增持倉。")
            return

        report_lines = []
        total_pl = 0.0
        total_cost = 0.0
        details = []

        for item in portfolio:
            symbol = item.get("symbol")
            quantity = float(item.get("quantity", 0))
            cost = float(item.get("cost", 0))
            if not symbol or quantity <= 0:
                continue

            # 使用 TWSE 即時 API 抓取價格
            try:
                current_price = await asyncio.to_thread(fetch_twse_price, symbol)
            except Exception as e:
                await notify_admin(f"報告時抓取 {symbol} 即時價格失敗：{e}")
                await interaction.response.send_message(f"⚠️ 無法取得 {symbol} 的即時價格，請稍後再試。")
                return

            value = current_price * quantity
            cost_value = cost * quantity
            profit = value - cost_value
            roi = profit / cost_value if cost_value != 0 else 0.0
            total_pl += profit
            total_cost += cost_value
            details.append((symbol, quantity, cost, current_price, profit, roi))

        embed = Embed(
            title="投資組合報告",
            color=0x0099FF,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="持股檔數", value=str(len(details)), inline=True)
        embed.add_field(name="總未實現損益", value=f"{total_pl:,.2f}", inline=True)
        embed.add_field(
            name="總報酬率", value=f"{(total_pl/total_cost*100 if total_cost else 0):.2f}%", inline=True
        )

        for symbol, quantity, cost, current_price, profit, roi in details:
            embed.add_field(
                name=f"{symbol}",
                value=(
                    f"數量：{quantity}\n"
                    f"成本：{cost:.2f}\n"
                    f"現價：{current_price:.2f}\n"
                    f"未實現損益：{profit:.2f}\n"
                    f"報酬率：{roi*100:.2f}%"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await notify_admin(f"report 指令發生錯誤：{e}")
        await interaction.response.send_message("⚠️ 報告產生失敗，管理員已收到通知。")


@bot.tree.command(name="update_portfolio", description="更新或新增持倉紀錄")
@app_commands.describe(symbol="股票代號", quantity="數量", cost="成本價")
@is_admin()
async def update_portfolio(interaction: discord.Interaction, symbol: str, quantity: float, cost: float):
    """更新或新增持倉紀錄。"""
    try:
        symbol = symbol.upper()
        portfolio = load_json(PORTFOLIO_PATH, [])
        updated = False
        for item in portfolio:
            if item.get("symbol") == symbol:
                item["quantity"] = quantity
                item["cost"] = cost
                updated = True
                break
        if not updated:
            portfolio.append({"symbol": symbol, "quantity": quantity, "cost": cost})
        save_json(PORTFOLIO_PATH, portfolio)
        await interaction.response.send_message(f"✅ 已更新持倉：{symbol} 數量={quantity} 成本價={cost:.2f}")
    except Exception as e:
        await notify_admin(f"update_portfolio 指令發生錯誤：{e}")
        await interaction.response.send_message("⚠️ 更新持倉失敗，管理員已收到通知。")


async def post_full_signals(channel):
    """讀 signals/latest.json，把 25 檔完整持股 + 動作分批推到 channel。"""
    if not LATEST_SIGNAL_PATH.exists():
        return
    with open(LATEST_SIGNAL_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    positions = data.get("positions", [])
    actions   = data.get("actions", [])
    industry  = data.get("industry", {})

    # ── Embed 1：總覽 ─────────────────────────────────────
    overview = Embed(
        title=f"📊 月度 Rebalance 訊號（{data.get('rebalance_date', '')}）",
        description=(f"**策略**：{data.get('strategy', '')}\n"
                     f"**帳戶資金**：{data.get('account_value', 0):,.0f} 元\n"
                     f"**曝險**：{data.get('exposure', 0)*100:.0f}%   "
                     f"**持股**：{data.get('n_holdings', 0)} 檔\n"
                     f"**換股頻率**：每 21 個交易日（約 1 個月）一次"),
        color=0x00BFFF,
        timestamp=datetime.utcnow(),
    )
    ind_lines = [f"`{k:<10}` {v*100:5.1f}%" for k, v in industry.items()]
    overview.add_field(name="🏭 產業分布", value="\n".join(ind_lines) or "—",
                        inline=False)
    counts = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    summary = (f"🟢 BUY {counts.get('BUY', 0)}   "
               f"🟡 TRIM {counts.get('TRIM', 0)}   "
               f"⚪ HOLD {counts.get('HOLD', 0)}   "
               f"🔴 SELL {counts.get('SELL', 0)}")
    overview.add_field(name="🛠️ 動作摘要", value=summary, inline=False)
    overview.set_footer(text="完整清單見後續訊息")
    await channel.send(embed=overview)

    # ── Embed 2-3：完整 25 檔（拆兩個 embed，每個 13 檔，避開 Discord 長度限制）──
    for chunk_idx, start in enumerate([0, 13]):
        chunk = positions[start:start + 13]
        if not chunk:
            continue
        lines = []
        for p in chunk:
            sid = p["symbol"]
            name = p.get("name", "") or ""
            display = f"`{sid}`" + (f" {name}" if name else "")
            weight = p.get("weight", 0) * 100
            dollars = p.get("target_dollars", 0)
            price = p.get("price_ref", 0)
            ind = p.get("industry", "")
            lines.append(
                f"**#{p['rank']:>2}** {display}\n"
                f"  目標權重 `{weight:5.2f}%` ・ "
                f"投入 `{dollars:>9,.0f}` 元 ・ "
                f"參考價 `{price:>6.2f}` ・ {ind}"
            )

        embed = Embed(
            title=f"📋 持股清單（{start + 1}-{start + len(chunk)}）",
            description="\n\n".join(lines),
            color=0x00BFFF,
        )
        await channel.send(embed=embed)

    # ── Embed 4：BUY 動作詳細（第一次跑時 25 檔都是 BUY）──────
    buys = [a for a in actions if a["action"] == "BUY"]
    trims = [a for a in actions if a["action"] == "TRIM"]
    sells = [a for a in actions if a["action"] == "SELL"]

    if buys or trims or sells:
        action_embed = Embed(
            title="🛠️ 交易動作",
            color=0xFFA500,
        )
        if buys:
            buy_lines = []
            for a in buys[:25]:
                sid = a["symbol"]
                name = a.get("name", "") or ""
                display = f"`{sid}`" + (f" {name}" if name else "")
                lots = a.get("target_lots", a.get("delta_lots", 0))
                price = a.get("price_ref", 0)
                buy_lines.append(f"🟢 {display} — 買 **{lots} 張** @ {price:.2f}")
            action_embed.add_field(
                name=f"🟢 BUY ({len(buys)} 檔)",
                value="\n".join(buy_lines),
                inline=False,
            )
        if trims:
            trim_lines = [
                f"🟡 `{a['symbol']}` "
                f"{a.get('name','')} — 賣 **{abs(a.get('delta_lots', 0))} 張** "
                f"@ {a.get('price_ref', 0):.2f}"
                for a in trims[:10]
            ]
            action_embed.add_field(
                name=f"🟡 TRIM ({len(trims)} 檔)",
                value="\n".join(trim_lines),
                inline=False,
            )
        if sells:
            sell_lines = [
                f"🔴 `{a['symbol']}` "
                f"{a.get('name','')} — 全賣 **{a.get('current_shares', 0)} 股**"
                for a in sells[:10]
            ]
            action_embed.add_field(
                name=f"🔴 SELL ({len(sells)} 檔)",
                value="\n".join(sell_lines),
                inline=False,
            )
        await channel.send(embed=action_embed)


@bot.tree.command(name="run_model", description="跑 N1 v2 ML 並推送月度交易清單到頻道")
@is_admin()
async def run_model(interaction: discord.Interaction):
    """執行 predict_model.py，跑完自動推「全 25 檔持股 + BUY/SELL 清單」。"""
    try:
        if not PREDICT_MODEL_PATH.exists():
            await interaction.response.send_message(
                "⚠️ 找不到 predict_model.py。"
            )
            return

        await interaction.response.send_message(
            "🧠 模型執行中（約 5-7 分鐘），跑完會自動推訊號到頻道..."
        )
        result = await asyncio.to_thread(
            subprocess.run,
            ["python", str(PREDICT_MODEL_PATH)],
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            await interaction.followup.send("⚠️ 模型執行失敗，請查看 log。")
            await notify_admin(f"run_model 失敗：\n{result.stderr[-500:]}")
            return

        # 自動推全清單到通知頻道
        channel = await get_notification_channel()
        if channel is None:
            await interaction.followup.send("✅ 模型執行完成（但通知頻道未設定）。")
            return

        await post_full_signals(channel)
        await interaction.followup.send("✅ 模型執行完成，訊號已推送到頻道。")
    except Exception as e:
        await notify_admin(f"run_model 指令發生錯誤：{e}")
        try:
            await interaction.followup.send("⚠️ 模型執行時發生錯誤，管理員已收到通知。")
        except Exception:
            pass


async def _run_subprocess_async(script_path,
                                  channel=None,
                                  label: str = "",
                                  heartbeat_interval: int = 300,
                                  extra_args: list = None) -> tuple:
    """
    用 asyncio.create_subprocess_exec 跑 script（非同步，無 15 分鐘綁定）。

    串流 stdout/stderr 並：
      ① 抓重要進度行（含「進度」「✅」「❌」「⚠️」「完成」「Error」「📥」「📊」）
      ② 每 heartbeat_interval 秒（預設 5 分鐘）發 heartbeat 到 channel
      ③ 全部 stdout/stderr 收集起來給最後的 result embed

    Args:
      script_path:        要跑的 Python 檔
      channel:            Discord channel（None 不發 heartbeat）
      label:              heartbeat 訊息中的標籤
      heartbeat_interval: 秒（預設 300 = 5 分鐘）

    Returns: (returncode, stdout_str, stderr_str)
    """
    import time as _t

    if not Path(script_path).exists():
        return -1, "", f"找不到 script: {script_path}"

    # -u: 關 Python stdout buffer，讓 progress 即時看得到
    cmd = ["python", "-u", str(script_path)]
    if extra_args:
        cmd.extend(extra_args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(BASE_DIR),
    )

    stdout_lines: list = []
    stderr_lines: list = []
    progress_lines: list = []

    PROGRESS_KEYWORDS = ("進度", "✅", "❌", "⚠️", "完成",
                          "Error", "📥", "📊", "✗", "✓", "Progress")

    async def _read_stream(stream, line_buffer):
        while True:
            try:
                line_bytes = await stream.readline()
            except Exception:
                break
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            line_buffer.append(line)
            if any(kw in line for kw in PROGRESS_KEYWORDS):
                progress_lines.append(line)

    stdout_task = asyncio.create_task(_read_stream(proc.stdout, stdout_lines))
    stderr_task = asyncio.create_task(_read_stream(proc.stderr, stderr_lines))

    start_t = _t.time()
    last_heartbeat = _t.time()

    # 主迴圈：定時送 heartbeat 直到 process 結束
    while True:
        try:
            # 短暫等 process 結束（順便當 sleep 用）
            await asyncio.wait_for(proc.wait(), timeout=15)
            break    # process 已結束
        except asyncio.TimeoutError:
            pass     # 還在跑，繼續

        now = _t.time()
        if channel and (now - last_heartbeat) >= heartbeat_interval:
            elapsed = now - start_t
            recent = progress_lines[-8:] if progress_lines else []
            recent_text = "\n".join(recent)[:1200] if recent else "(尚無 stdout 進度輸出)"
            try:
                msg = (f"💓 **{label}** 仍在執行 ({elapsed:.0f}s 已過)\n"
                       f"```\n{recent_text}\n```")
                await channel.send(msg[:1900])
            except Exception:
                pass
            last_heartbeat = now
            # 保留最近 50 行避免無限增長
            if len(progress_lines) > 50:
                progress_lines[:] = progress_lines[-50:]

    # process 結束，等 stream 讀取也完成
    try:
        await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task,
                                                return_exceptions=True),
                                 timeout=5)
    except asyncio.TimeoutError:
        pass

    return (proc.returncode,
            "\n".join(stdout_lines),
            "\n".join(stderr_lines))


def _build_result_embed(scripts: list, labels: list,
                         results: list, elapsed: float, title: str) -> Embed:
    """把 subprocess 結果包成 Embed。"""
    all_ok = all(r is not None and r[0] == 0 for r in results)
    embed = Embed(
        title=title,
        color=0x00FF00 if all_ok else 0xFFA500,
        timestamp=datetime.utcnow(),
    )
    for label, r in zip(labels, results):
        if r is None:
            embed.add_field(name=label, value="⚠️ 找不到 script", inline=True)
        else:
            rc = r[0]
            embed.add_field(
                name=label,
                value="✅ 成功" if rc == 0 else f"❌ 失敗 ({rc})",
                inline=True,
            )
    embed.add_field(name="耗時", value=f"{elapsed:.0f} 秒", inline=True)

    for label, r in zip(labels, results):
        if r is None:
            continue
        rc, out, err = r
        if rc != 0 and err:
            embed.add_field(name=f"⚠️ {label} 錯誤",
                            value=f"```\n{err[-400:]}\n```", inline=False)
        else:
            tail = "\n".join((out or "").strip().split("\n")[-6:])
            if tail:
                embed.add_field(name=f"📋 {label} 摘要",
                                value=f"```\n{tail[:500]}\n```", inline=False)
    return embed


async def _run_update_background(channel, scripts: list, labels: list, title: str,
                                   heartbeat_interval: int = 300,
                                   script_args: list = None):
    """
    背景任務：跑 scripts，跑完發 embed 到 channel。
    每 heartbeat_interval 秒會發一次「💓 still running」進度訊息。

    script_args: 與 scripts 對齊的 extra args list（例如 [["--top","300"], None]）
    """
    try:
        start_time = datetime.utcnow()
        results = []
        for i, (script, label) in enumerate(zip(scripts, labels)):
            await channel.send(f"▶️ **{label}** 開始執行...")
            extras = (script_args[i] if script_args and i < len(script_args) else None)
            r = await _run_subprocess_async(
                script,
                channel=channel,
                label=label,
                heartbeat_interval=heartbeat_interval,
                extra_args=extras,
            )
            results.append(r)
            rc = r[0]
            status = "✅ 完成" if rc == 0 else f"❌ 失敗 (code={rc})"
            await channel.send(f"⏹️ **{label}** {status}")

        elapsed = (datetime.utcnow() - start_time).total_seconds()
        embed = _build_result_embed(scripts, labels, results, elapsed, title)
        embed.set_footer(text="跑完後產生最新訊號用 /run_model")
        await channel.send(embed=embed)
    except Exception as e:
        try:
            await channel.send(f"⚠️ 背景更新失敗：{e}")
        except Exception:
            pass
        await notify_admin(f"_run_update_background 失敗：{e}")


@bot.tree.command(
    name="update_data",
    description="更新股票資料（背景跑，跑完到頻道通知）",
)
@app_commands.describe(source="價格資料來源")
@app_commands.choices(source=[
    app_commands.Choice(name="Fugle (推薦) — 股價 + FinMind 籌碼", value="fugle"),
    app_commands.Choice(name="FinMind 全包（舊行為）", value="finmind"),
])
@is_admin()
async def update_data(interaction: discord.Interaction,
                       source: app_commands.Choice[str] = None):
    """
    啟動背景任務跑資料更新。立刻 acknowledge 不卡 Discord 15 分鐘 token。
    跑完直接發 embed 到 NOTIFICATION_CHANNEL_ID。
    """
    try:
        mode = source.value if source else "fugle"

        channel = await get_notification_channel()
        if channel is None:
            await interaction.response.send_message(
                "⚠️ NOTIFICATION_CHANNEL_ID 未設定，跑完無處可推。"
            )
            return

        if mode == "fugle":
            if not os.getenv("FUGLE_API_KEY", "").strip():
                await interaction.response.send_message(
                    "⚠️ FUGLE_API_KEY 未設定（.env 找不到）。\n"
                    "申請：https://developer.fugle.tw/"
                )
                return
            if not os.getenv("FINMIND_TOKEN", "").strip():
                await interaction.response.send_message(
                    "⚠️ FINMIND_TOKEN 未設定（即使用 Fugle，籌碼仍需 FinMind）。"
                )
                return

            await interaction.response.send_message(
                "📡 **已啟動背景更新（Fugle + FinMind, --top 300）**\n"
                "  ① Fugle 抓最新股價（OHLCV，前 300 活躍股）\n"
                "  ② FinMind 抓最新籌碼（前 300 活躍股）\n"
                "預計 30-60 分鐘（之前抓全部 2,056 檔要 9 hr，現在只抓投資宇宙）。\n"
                "**跑完會直接在頻道發結果**，這個訊息可以無視。"
            )
            asyncio.create_task(_run_update_background(
                channel,
                scripts=[
                    BASE_DIR / "data_pipeline" / "update_prices_fugle.py",
                    BASE_DIR / "data_pipeline" / "download_institutional.py",
                ],
                labels=["Fugle 股價", "FinMind 籌碼"],
                title="📡 股票資料更新完成（Fugle + FinMind，top 300）",
                script_args=[
                    ["--top", "300"],   # Fugle
                    ["--top", "300", "--workers", "2"],  # FinMind
                ],
            ))
            return

        if not os.getenv("FINMIND_TOKEN", "").strip():
            await interaction.response.send_message("⚠️ FINMIND_TOKEN 未設定。")
            return

        await interaction.response.send_message(
            "📡 **已啟動背景更新（FinMind 全包）**\n"
            "可能需 5-30 分鐘。**跑完會直接發到頻道**。"
        )
        asyncio.create_task(_run_update_background(
            channel,
            scripts=[BASE_DIR / "automation" / "daily_update.py"],
            labels=["FinMind 全包"],
            title="📡 股票資料更新完成（FinMind）",
        ))

    except Exception as e:
        await notify_admin(f"update_data 指令發生錯誤：{e}")
        try:
            await interaction.response.send_message(f"⚠️ 啟動失敗：{e}")
        except Exception:
            pass



@bot.tree.command(name="post_signals",
                   description="把目前 signals/latest.json 重推一次到頻道（不重跑模型）")
@is_admin()
async def post_signals(interaction: discord.Interaction):
    """重推訊號（適合你想再看一次完整清單）。"""
    try:
        if not LATEST_SIGNAL_PATH.exists():
            await interaction.response.send_message(
                "⚠️ 還沒有 signals/latest.json，請先 `/run_model`。"
            )
            return
        channel = await get_notification_channel()
        if channel is None:
            await interaction.response.send_message("⚠️ 通知頻道未設定。")
            return
        await interaction.response.send_message("📤 重推訊號中...")
        await post_full_signals(channel)
        await interaction.followup.send("✅ 已推送。")
    except Exception as e:
        await notify_admin(f"post_signals 指令發生錯誤：{e}")
        try:
            await interaction.followup.send("⚠️ 推送失敗，管理員已收到通知。")
        except Exception:
            pass


@bot.tree.command(name="set_threshold", description="設定預測通知門檻")
@app_commands.describe(value="門檻值 (例如 0.05)")
@is_admin()
async def set_threshold(interaction: discord.Interaction, value: float):
    """設定預測通知門檻。"""
    try:
        cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        cfg["threshold"] = value
        save_json(CONFIG_PATH, cfg)
        await interaction.response.send_message(f"✅ 已更新 threshold 為 {value:.4f}。")
    except Exception as e:
        await notify_admin(f"set_threshold 指令發生錯誤：{e}")
        await interaction.response.send_message("⚠️ 更新 threshold 失敗，管理員已收到通知。")


@bot.tree.command(name="signals", description="顯示 N1 v2 ML 最新目標持股清單")
async def signals(interaction: discord.Interaction):
    """讀 signals/latest.json，顯示前 10 大持股 + 產業分布。"""
    try:
        if not LATEST_SIGNAL_PATH.exists():
            await interaction.response.send_message(
                "⚠️ 還沒有 signals/latest.json，請先執行 `/run_model`。"
            )
            return

        with open(LATEST_SIGNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        positions = data.get("positions", [])[:10]   # 顯示前 10 大
        industry  = data.get("industry", {})

        embed = Embed(
            title=f"📊 N1 v2 ML 目標持股（{data.get('rebalance_date', '')}）",
            description=f"**策略**：{data.get('strategy', '')}\n"
                        f"**帳戶資金**：{data.get('account_value', 0):,.0f} NTD\n"
                        f"**曝險**：{data.get('exposure', 0)*100:.1f}%\n"
                        f"**持股檔數**：{data.get('n_holdings', 0)}",
            color=0x00BFFF,
            timestamp=datetime.utcnow(),
        )

        # Top 10 持股
        lines = []
        for p in positions:
            name = p.get("name", "")
            display = f"{p['symbol']} {name}" if name else p["symbol"]
            lines.append(
                f"`{p['rank']:>2}.` **{display}** — "
                f"{p['weight']*100:5.2f}% / {p['target_dollars']:,.0f} 元"
            )
        embed.add_field(name="🥇 前 10 大", value="\n".join(lines), inline=False)

        # 產業分布
        ind_lines = [f"**{k}**: {v*100:.1f}%" for k, v in industry.items()]
        embed.add_field(name="🏭 產業分布", value="\n".join(ind_lines) or "—",
                        inline=False)

        embed.set_footer(text=f"完整清單見 signals/latest.json，共 {data.get('n_holdings', 0)} 檔")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await notify_admin(f"signals 指令發生錯誤：{e}")
        await interaction.response.send_message("⚠️ 顯示訊號失敗，管理員已收到通知。")


@bot.tree.command(name="actions", description="顯示與目前持倉的買/賣/維持差異")
async def actions(interaction: discord.Interaction):
    """讀 signals/latest.json 的 actions 陣列，顯示 BUY/SELL/TRIM/HOLD。"""
    try:
        if not LATEST_SIGNAL_PATH.exists():
            await interaction.response.send_message(
                "⚠️ 還沒有 signals/latest.json，請先執行 `/run_model`。"
            )
            return

        with open(LATEST_SIGNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        actions_list = data.get("actions", [])
        if not actions_list:
            await interaction.response.send_message(
                "📌 沒有任何 actions（你的 portfolio.json 可能是空的）。"
                "先用 `/update_portfolio` 加入現有持倉，再 `/run_model`。"
            )
            return

        # 分類
        buys  = [a for a in actions_list if a["action"] == "BUY"]
        trims = [a for a in actions_list if a["action"] == "TRIM"]
        holds = [a for a in actions_list if a["action"] == "HOLD"]
        sells = [a for a in actions_list if a["action"] == "SELL"]

        embed = Embed(
            title=f"🛠️ 交易動作（{data.get('rebalance_date', '')}）",
            description=f"**目標 vs 現有持倉**\n"
                        f"BUY {len(buys)} | TRIM {len(trims)} | "
                        f"HOLD {len(holds)} | SELL {len(sells)}",
            color=0xFFA500,
            timestamp=datetime.utcnow(),
        )

        def fmt(a, action_type):
            sid = a.get("symbol", "")
            name = a.get("name", "")
            display = f"{sid} {name}" if name else sid
            if action_type == "BUY":
                lots = a.get('target_lots', a.get('delta_lots', 0))
                return f"🟢 **{display}** — 買 {lots} 張 @ {a.get('price_ref', 0):.2f}"
            if action_type == "TRIM":
                return f"🟡 **{display}** — 賣 {abs(a.get('delta_lots', 0))} 張 @ {a.get('price_ref', 0):.2f}"
            if action_type == "HOLD":
                return f"⚪ **{display}** — 維持 {a.get('current_shares', 0)} 股"
            if action_type == "SELL":
                return f"🔴 **{display}** — 全賣 {a.get('current_shares', 0)} 股"
            return display

        if buys:
            embed.add_field(name="🟢 BUY",
                            value="\n".join(fmt(a, "BUY") for a in buys[:10]),
                            inline=False)
        if trims:
            embed.add_field(name="🟡 TRIM",
                            value="\n".join(fmt(a, "TRIM") for a in trims[:10]),
                            inline=False)
        if sells:
            embed.add_field(name="🔴 SELL",
                            value="\n".join(fmt(a, "SELL") for a in sells[:10]),
                            inline=False)

        embed.set_footer(text="顯示前 10，完整清單見 signals/latest.json")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        await notify_admin(f"actions 指令發生錯誤：{e}")
        await interaction.response.send_message("⚠️ 顯示動作失敗，管理員已收到通知。")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("⚠️ 你沒有權限執行此指令。")
        return
    await notify_admin(f"指令 {ctx.command} 發生錯誤：{error}")
    await ctx.send("⚠️ 執行指令時發生錯誤，管理員已收到通知。")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN 尚未設定，請在 .env 中加入。")
    bot.run(DISCORD_TOKEN)
