"""
notifier.py
─────────────────────────────────────────────────────────────
通知模組：負責把今日訊號傳送到 LINE 和 Notion。

Task 8 更新：
  - LINE 訊息加入今日機制狀態＋健康分數；機制比昨天降級時，額外插入
    「⚠️ 機制警示」區塊
  - Notion 記錄新增「市場機制」（Select）、「健康分數」（Number）欄位
  - dry-run：LINE_NOTIFY_TOKEN／NOTION_TOKEN／NOTION_DATABASE_ID
    任一沒有設定時，不是靜默跳過——而是照樣把完整內容組好、印到 log，
    並存檔到 signals/YYYY-MM-DD_notify_{line,notion}.json，讓 Task 8
    可以在沒有申請任何 token 的情況下先把整條流程開發、測試完整，
    等 token 補齊後只要重新執行就會自動改成真實發送，不用改程式碼。

使用前置作業：
  LINE  → https://notify-bot.line.me/my/ 申請 token
  Notion→ https://www.notion.so/my-integrations 建立 integration
         → 把 integration 加入你的 Notion 資料庫（含「市場機制」Select、
           「健康分數」Number 兩個新欄位）
"""

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

BASE_DIR = Path(__file__).parent.parent
SIGNALS_DIR = BASE_DIR / "signals"

REGIME_EMOJI = {"BULL": "🟢", "NEUTRAL": "🟡", "WARNING": "🟠", "BEAR": "🔴"}


# ══════════════════════════════════════════════════════════════
# dry-run 存檔共用工具
# ══════════════════════════════════════════════════════════════

def _save_dry_run(channel: str, content, today: str) -> Path:
    SIGNALS_DIR.mkdir(exist_ok=True)
    path = SIGNALS_DIR / f"{today}_notify_{channel}.json"
    payload = {"date": today, "channel": channel, "mode": "dry_run", "content": content}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


# ══════════════════════════════════════════════════════════════
# LINE Notify
# ══════════════════════════════════════════════════════════════

LINE_NOTIFY_URL = "https://notify-api.line.me/api/notify"


def send_line(message: str, today: str, token: Optional[str] = None) -> bool:
    """
    發送 LINE Notify 訊息。token 不存在時走 dry-run：組好的內容一定會被
    印到 log，並存檔到 signals/，不是單純印一句「跳過」就結束。

    Returns
    -------
    bool：True 表示「這次呼叫沒有異常」（真實發送成功，或 dry-run 正常
    完成存檔），跟「有沒有真的送到 LINE」是兩件事——呼叫端要分辨這個，
    請直接看 log 裡有沒有「dry-run」字樣。
    """
    token = token or os.getenv("LINE_NOTIFY_TOKEN", "").strip()

    if not token:
        logger.info(f"📝 [LINE dry-run] token 未設定，組好的訊息內容：\n{message}")
        path = _save_dry_run("line", message, today)
        logger.info(f"💾 [LINE dry-run] 已存檔：{path}")
        return True

    try:
        resp = requests.post(
            LINE_NOTIFY_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"message": message},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("✅ LINE 通知已發送")
            return True
        logger.warning(f"⚠️  LINE 通知失敗：{resp.status_code} {resp.text}")
        return False
    except Exception as e:
        logger.warning(f"⚠️  LINE 通知錯誤：{e}")
        return False


def format_line_message(signals: list, stats: dict, regime_info: dict, today: str) -> str:
    """
    把訊號資料格式化成 LINE 訊息，含今日機制狀態＋健康分數；機制比昨天
    降級時，在最前面插入一段「⚠️ 機制警示」。
    """
    today_fmt = today[5:]   # 2026-04-27 → 04-27

    lines = []

    if regime_info and regime_info.get("downgraded"):
        lines.append("⚠️ 機制警示：市場狀態較昨日降級")
        lines.append(
            f"   {regime_info.get('previous_regime')} → {regime_info.get('regime')}"
        )
        lines.append("─" * 20)

    lines += [
        f"📊 台股量化訊號 {today_fmt}",
        "─" * 20,
    ]

    if regime_info and regime_info.get("regime"):
        emoji = REGIME_EMOJI.get(regime_info["regime"], "⚪")
        lines.append(
            f"{emoji} 市場機制：{regime_info['regime']}"
            f"（健康分數 {regime_info.get('health_score')}，"
            f"建議曝險 {regime_info.get('exposure')}）"
        )
        lines.append("─" * 20)

    # 持倉清單
    holds = [s for s in signals if s.get("action") in ("BUY", "HOLD")]
    sells = [s for s in signals if s.get("action") == "SELL"]
    buys  = [s for s in signals if s.get("action") == "BUY"]

    if holds:
        lines.append(f"🟢 持倉（{len(holds)} 檔）")
        for s in holds:
            flag = "🆕 " if s.get("action") == "BUY" else "   "
            lines.append(
                f"{flag}{s['stock_id']} {s.get('stock_name','')}  "
                f"{s['weight']*100:.1f}%"
            )
    if sells:
        lines.append(f"\n🔴 今日賣出（{len(sells)} 檔）")
        for s in sells:
            lines.append(f"   {s['stock_id']} {s.get('stock_name','')}")
    if buys:
        lines.append(f"\n🆕 今日買入：{len(buys)} 檔")

    # 績效摘要
    lines.append("─" * 20)
    if stats:
        lines.append(f"📈 年化報酬  {stats.get('annual_return','N/A')}")
        lines.append(f"📉 最大回撤  {stats.get('max_drawdown','N/A')}")
        lines.append(f"⚡ Sharpe   {stats.get('sharpe','N/A')}")

    lines.append("─" * 20)
    lines.append("⚠️ 僅供參考，請自行判斷後下單")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# Notion API
# ══════════════════════════════════════════════════════════════

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _build_notion_properties(signals: list, stats: dict, regime_info: dict, today: str) -> dict:
    holds = [s for s in signals if s.get("action") in ("BUY", "HOLD")]
    sells = [s for s in signals if s.get("action") == "SELL"]
    buys  = [s for s in signals if s.get("action") == "BUY"]

    holdings_str = ", ".join(f"{s['stock_id']}({s['weight']*100:.0f}%)" for s in holds)
    buy_str  = ", ".join(s["stock_id"] for s in buys)  or "無"
    sell_str = ", ".join(s["stock_id"] for s in sells) or "無"

    properties = {
        "Name": {"title": [{"text": {"content": f"台股訊號 {today}"}}]},
        "日期": {"date": {"start": today}},
        "持倉清單": {"rich_text": [{"text": {"content": holdings_str[:2000]}}]},
        "年化報酬": {"rich_text": [{"text": {"content": str(stats.get("annual_return", ""))}}]},
        "最大回撤": {"rich_text": [{"text": {"content": str(stats.get("max_drawdown", ""))}}]},
        "Sharpe": {"rich_text": [{"text": {"content": str(stats.get("sharpe", ""))}}]},
        "買入訊號": {"rich_text": [{"text": {"content": buy_str}}]},
        "賣出訊號": {"rich_text": [{"text": {"content": sell_str}}]},
        "完整JSON": {"rich_text": [{"text": {
            "content": json.dumps({"signals": signals, "stats": stats}, ensure_ascii=False)[:2000]
        }}]},
    }

    if regime_info and regime_info.get("regime"):
        properties["市場機制"] = {"select": {"name": regime_info["regime"]}}
    if regime_info and regime_info.get("health_score") is not None:
        properties["健康分數"] = {"number": regime_info["health_score"]}

    return properties


def create_notion_record(
    signals: list,
    stats: dict,
    regime_info: dict,
    today: str,
    token: Optional[str] = None,
    database_id: Optional[str] = None,
) -> bool:
    """
    在 Notion 資料庫新增今日交易紀錄。

    Notion 資料庫需要有以下欄位（在建立時設定）：
      日期        → Date
      持倉清單     → Rich Text
      年化報酬     → Rich Text
      最大回撤     → Rich Text
      Sharpe      → Rich Text
      買入訊號     → Rich Text
      賣出訊號     → Rich Text
      完整 JSON   → Rich Text
      市場機制     → Select（Task 8 新增：BULL/NEUTRAL/WARNING/BEAR）
      健康分數     → Number（Task 8 新增：0-100）

    token／database_id 沒設定時走 dry-run：組好的 properties payload
    一樣會印到 log 並存檔到 signals/，不是單純跳過。
    """
    token       = token       or os.getenv("NOTION_TOKEN", "").strip()
    database_id = database_id or os.getenv("NOTION_DATABASE_ID", "").strip()

    properties = _build_notion_properties(signals, stats, regime_info, today)

    if not token or not database_id:
        logger.info(f"📝 [Notion dry-run] token/database_id 未設定，組好的 properties：\n"
                   f"{json.dumps(properties, ensure_ascii=False, indent=2)}")
        path = _save_dry_run("notion", properties, today)
        logger.info(f"💾 [Notion dry-run] 已存檔：{path}")
        return True

    payload = {"parent": {"database_id": database_id}, "properties": properties}

    try:
        resp = requests.post(
            f"{NOTION_API_URL}/pages",
            headers=_notion_headers(token),
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("✅ Notion 記錄已建立")
            return True
        logger.warning(f"⚠️  Notion 失敗：{resp.status_code}\n{resp.text[:300]}")
        return False
    except Exception as e:
        logger.warning(f"⚠️  Notion 錯誤：{e}")
        return False


# ══════════════════════════════════════════════════════════════
# 快捷函式：一次發送兩個管道
# ══════════════════════════════════════════════════════════════

def notify_all(signals: list, stats: dict, regime_info: Optional[dict] = None,
              factor_decay_alerts: Optional[dict] = None,
              today: Optional[str] = None) -> None:
    """
    同時發送 LINE + Notion。在 daily_update.py 的最後呼叫這個函式就好。

    factor_decay_alerts 目前不放進通知內容本身（LINE 訊息已經很長了），
    只確保它有被傳進來、跟 regime_info 一起走同一套 dry-run／真實發送
    判斷路徑，供之後要把衰退警示也加進通知文字時使用。
    """
    today = today or str(date.today())
    regime_info = regime_info or {}

    logger.info(f"📡 發送通知（{today}）...")

    msg = format_line_message(signals, stats, regime_info, today)
    send_line(msg, today)

    create_notion_record(signals, stats, regime_info, today)
