# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_notifier.py

驗證 automation/notifier.py（Task 8）的邏輯正確性：機制/健康分數格式化、
降級警示、Notion 新欄位、dry-run（無 token 時組內容+log+存檔）行為。
全部用合成資料，不打真實 LINE/Notion API。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "automation"))

import notifier


SIGNALS = [
    {"stock_id": "2330", "stock_name": "台積電", "action": "BUY", "weight": 0.05},
    {"stock_id": "2317", "stock_name": "鴻海", "action": "HOLD", "weight": 0.04},
    {"stock_id": "1234", "stock_name": "測試股", "action": "SELL", "weight": 0.0},
]
STATS = {"annual_return": "6.5%", "max_drawdown": "-24.9%", "sharpe": "0.41"}


def test_format_line_message_includes_regime_and_health_score():
    regime_info = {"regime": "BULL", "health_score": 72.3, "exposure": 1.0, "downgraded": False}
    msg = notifier.format_line_message(SIGNALS, STATS, regime_info, "2026-08-28")
    assert "BULL" in msg
    assert "72.3" in msg
    assert "台積電" in msg
    assert "測試股" in msg  # SELL 清單裡


def test_format_line_message_downgrade_warning_only_when_downgraded():
    regime_info_down = {"regime": "BEAR", "health_score": 20.0, "exposure": 0.1,
                        "downgraded": True, "previous_regime": "WARNING"}
    msg_down = notifier.format_line_message(SIGNALS, STATS, regime_info_down, "2026-08-28")
    assert "機制警示" in msg_down
    assert "WARNING" in msg_down and "BEAR" in msg_down

    regime_info_ok = {"regime": "BULL", "health_score": 80.0, "exposure": 1.0,
                      "downgraded": False, "previous_regime": "BULL"}
    msg_ok = notifier.format_line_message(SIGNALS, STATS, regime_info_ok, "2026-08-28")
    assert "機制警示" not in msg_ok


def test_format_line_message_handles_missing_regime_info():
    # regime_info 為空字典（機制偵測沒有輸出的情境）不應該噴例外
    msg = notifier.format_line_message(SIGNALS, STATS, {}, "2026-08-28")
    assert "台股量化訊號" in msg


def test_build_notion_properties_includes_regime_select_and_health_number():
    regime_info = {"regime": "WARNING", "health_score": 45.5}
    props = notifier._build_notion_properties(SIGNALS, STATS, regime_info, "2026-08-28")
    assert props["市場機制"]["select"]["name"] == "WARNING"
    assert props["健康分數"]["number"] == 45.5


def test_build_notion_properties_omits_regime_fields_when_absent():
    props = notifier._build_notion_properties(SIGNALS, STATS, {}, "2026-08-28")
    assert "市場機制" not in props
    assert "健康分數" not in props


def test_send_line_dry_run_when_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("LINE_NOTIFY_TOKEN", raising=False)
    monkeypatch.setattr(notifier, "SIGNALS_DIR", tmp_path)

    ok = notifier.send_line("測試訊息", "2026-08-28")
    assert ok is True

    saved = tmp_path / "2026-08-28_notify_line.json"
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["content"] == "測試訊息"


def test_send_line_real_send_when_token_present(monkeypatch):
    calls = {}

    class FakeResp:
        status_code = 200
        text = ""

    def fake_post(url, headers=None, data=None, timeout=None):
        calls["url"] = url
        calls["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    ok = notifier.send_line("測試訊息", "2026-08-28", token="fake-token")
    assert ok is True
    assert calls["headers"]["Authorization"] == "Bearer fake-token"


def test_create_notion_record_dry_run_when_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    monkeypatch.setattr(notifier, "SIGNALS_DIR", tmp_path)

    ok = notifier.create_notion_record(SIGNALS, STATS, {"regime": "BULL", "health_score": 70.0},
                                       "2026-08-28")
    assert ok is True

    saved = tmp_path / "2026-08-28_notify_notion.json"
    assert saved.exists()
    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["content"]["市場機制"]["select"]["name"] == "BULL"


def test_notify_all_runs_both_channels_without_error(tmp_path, monkeypatch):
    monkeypatch.delenv("LINE_NOTIFY_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    monkeypatch.setattr(notifier, "SIGNALS_DIR", tmp_path)

    notifier.notify_all(SIGNALS, STATS, {"regime": "NEUTRAL", "health_score": 50.0},
                        {"momentum": {"decayed": False}}, today="2026-08-28")

    assert (tmp_path / "2026-08-28_notify_line.json").exists()
    assert (tmp_path / "2026-08-28_notify_notion.json").exists()
