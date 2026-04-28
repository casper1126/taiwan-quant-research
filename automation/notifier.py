"""
notifier.py
─────────────────────────────────────────────────────────────
通知模組：負責把今日訊號傳送到 LINE 和 Notion

LINE Notify：每日推播持倉清單 + 績效摘要
Notion API ：自動在資料庫新增一頁，記錄每日完整結果

使用前置作業：
  LINE  → https://notify-bot.line.me/my/ 申請 token
  Notion→ https://www.notion.so/my-integrations 建立 integration
         → 把 integration 加入你的 Notion 資料庫
"""

import os
import json
import requests
from datetime import date
from typing import Optional


# ══════════════════════════════════════════════════════════════
# LINE Notify
# ══════════════════════════════════════════════════════════════

LINE_NOTIFY_URL = "https://notify-api.line.me/api/notify"


def send_line(message: str, token: Optional[str] = None) -> bool:
    """
    發送 LINE Notify 訊息。

    Parameters
    ----------
    message : 要發送的訊息（最多 1000 字元）
    token   : LINE Notify token，None 則從環境變數讀取

    Returns
    -------
    bool：成功 True，失敗 False
    """
    token = token or os.getenv("LINE_NOTIFY_TOKEN", "")
    if not token:
        print("⚠️  LINE_NOTIFY_TOKEN 未設定，跳過 LINE 通知")
        return False

    try:
        resp = requests.post(
            LINE_NOTIFY_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={"message": message},
            timeout=10,
        )
        if resp.status_code == 200:
            print("✅ LINE 通知已發送")
            return True
        else:
            print(f"⚠️  LINE 通知失敗：{resp.status_code} {resp.text}")
            return False
    except Exception as e:
        print(f"⚠️  LINE 通知錯誤：{e}")
        return False


def format_line_message(signals: list, stats: dict, today: str) -> str:
    """
    把訊號資料格式化成 LINE 訊息。

    訊息格式：
    ─────────────────
    📊 台股量化訊號 04/27
    ─────────────────
    🟢 持倉（10 檔）
    2330 台積電  10.0%
    2317 鴻海    10.0%
    ...
    ─────────────────
    📈 年化報酬  +23.5%
    📉 最大回撤  -12.3%
    ⚡ Sharpe   1.85
    """
    today_fmt = today[5:]   # 2026-04-27 → 04-27

    lines = [
        f"\n📊 台股量化訊號 {today_fmt}",
        "─" * 20,
    ]

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


def create_notion_record(
    signals: list,
    stats: dict,
    today: str,
    token: Optional[str] = None,
    database_id: Optional[str] = None,
) -> bool:
    """
    在 Notion 資料庫新增今日交易紀錄。

    Notion 資料庫需要有以下欄位（在建立時設定）：
      日期        → Date 類型
      持倉清單     → Rich Text 類型
      年化報酬     → Rich Text 類型
      最大回撤     → Rich Text 類型
      Sharpe      → Number 類型
      買入訊號     → Rich Text 類型
      賣出訊號     → Rich Text 類型
      完整 JSON   → Rich Text 類型

    Parameters
    ----------
    signals     : 今日訊號清單
    stats       : 績效指標 dict
    today       : 日期字串 YYYY-MM-DD
    token       : Notion Integration token
    database_id : Notion 資料庫 ID
    """
    token       = token       or os.getenv("NOTION_TOKEN", "")
    database_id = database_id or os.getenv("NOTION_DATABASE_ID", "")

    if not token or not database_id:
        print("⚠️  NOTION_TOKEN 或 NOTION_DATABASE_ID 未設定，跳過 Notion")
        return False

    # 整理持倉字串
    holds = [s for s in signals if s.get("action") in ("BUY", "HOLD")]
    sells = [s for s in signals if s.get("action") == "SELL"]
    buys  = [s for s in signals if s.get("action") == "BUY"]

    holdings_str = ", ".join(
        f"{s['stock_id']}({s['weight']*100:.0f}%)" for s in holds
    )
    buy_str  = ", ".join(s["stock_id"] for s in buys)  or "無"
    sell_str = ", ".join(s["stock_id"] for s in sells) or "無"

    # Notion page properties
    properties = {
        "日期": {
            "date": {"start": today}
        },
        "持倉清單": {
            "rich_text": [{"text": {"content": holdings_str[:2000]}}]
        },
        "年化報酬": {
            "rich_text": [{"text": {"content": stats.get("annual_return", "")}}]
        },
        "最大回撤": {
            "rich_text": [{"text": {"content": stats.get("max_drawdown", "")}}]
        },
        "Sharpe": {
            "rich_text": [{"text": {"content": str(stats.get("sharpe", ""))}}]
        },
        "買入訊號": {
            "rich_text": [{"text": {"content": buy_str}}]
        },
        "賣出訊號": {
            "rich_text": [{"text": {"content": sell_str}}]
        },
        "完整JSON": {
            "rich_text": [{
                "text": {
                    "content": json.dumps(
                        {"signals": signals, "stats": stats},
                        ensure_ascii=False
                    )[:2000]
                }
            }]
        },
    }

    # 頁面標題（Name 欄）
    properties["Name"] = {
        "title": [{"text": {"content": f"台股訊號 {today}"}}]
    }

    payload = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }

    try:
        resp = requests.post(
            f"{NOTION_API_URL}/pages",
            headers=_notion_headers(token),
            json=payload,
            timeout=15,
        )
        if resp.status_code == 200:
            print("✅ Notion 記錄已建立")
            return True
        else:
            print(f"⚠️  Notion 失敗：{resp.status_code}\n{resp.text[:300]}")
            return False
    except Exception as e:
        print(f"⚠️  Notion 錯誤：{e}")
        return False


# ══════════════════════════════════════════════════════════════
# 快捷函式：一次發送兩個管道
# ══════════════════════════════════════════════════════════════

def notify_all(signals: list, stats: dict, today: Optional[str] = None):
    """
    同時發送 LINE + Notion。
    在 daily_update.py 的最後呼叫這個函式就好。
    """
    today = today or str(date.today())

    print(f"\n📡 發送通知（{today}）...")

    # LINE
    msg = format_line_message(signals, stats, today)
    send_line(msg)

    # Notion
    create_notion_record(signals, stats, today)