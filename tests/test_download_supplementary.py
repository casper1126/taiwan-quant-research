"""
tests/test_download_supplementary.py
─────────────────────────────────────────────────────────────
本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，
使用構造資料屬正常做法。

驗證 data_pipeline/download_supplementary.py 的配額處理邏輯：
真正的 HTTP 402 會被辨識成 QuotaExhaustedError（不是誤判成「無資料」），
以及完整的休眠 → 自動恢復 → 從 checkpoint 繼續流程（用縮小的時間窗口，
幾秒內驗證完，不必真的等一小時，也不會打到真實的 FinMind API）。
"""

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "data_pipeline"))

import download_supplementary as ds


def _make_response(status_code, json_body):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    if status_code == 200:
        resp.raise_for_status.return_value = None
    else:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


@contextmanager
def _noop_lock(script_name):
    """測試不需要重複驗證 lock file 本身（test_download_lock.py 已經測過），
    這裡繞過它，避免測試去動到真實的 data/.download_lock。"""
    yield


# ══════════════════════════════════════════════════════════════
# _fetch() 402 辨識
# ══════════════════════════════════════════════════════════════

def test_fetch_raises_quota_exhausted_on_http_402(monkeypatch):
    monkeypatch.setattr(ds, "_rate_limiter", ds.RateLimiter(per_call_sleep=0, limit=999))
    resp_402 = _make_response(402, {"status": 402, "msg": "quota exceeded"})
    with patch("download_supplementary.requests.get", return_value=resp_402):
        with pytest.raises(ds.QuotaExhaustedError):
            ds._fetch("TaiwanStockMarginPurchaseShortSale", "9999",
                      "2015-01-01", "2015-01-02", "fake_token")


def test_fetch_returns_empty_df_for_genuine_no_data(monkeypatch):
    """HTTP 200 但 data 是空陣列，代表真的沒有資料，應該回傳空 DataFrame，不是拋例外。"""
    monkeypatch.setattr(ds, "_rate_limiter", ds.RateLimiter(per_call_sleep=0, limit=999))
    resp_empty = _make_response(200, {"status": 200, "data": []})
    with patch("download_supplementary.requests.get", return_value=resp_empty):
        df = ds._fetch("TaiwanStockMarginPurchaseShortSale", "1107",
                       "2015-01-01", "2015-01-02", "fake_token")
    assert df.empty


def test_fetch_returns_data_on_success(monkeypatch):
    monkeypatch.setattr(ds, "_rate_limiter", ds.RateLimiter(per_call_sleep=0, limit=999))
    resp_ok = _make_response(200, {"status": 200, "data": [{"date": "2015-01-05", "stock_id": "2330"}]})
    with patch("download_supplementary.requests.get", return_value=resp_ok):
        df = ds._fetch("TaiwanStockMarginPurchaseShortSale", "2330",
                       "2015-01-01", "2015-01-02", "fake_token")
    assert len(df) == 1


# ══════════════════════════════════════════════════════════════
# 完整休眠 → 自動恢復 → 從 checkpoint 繼續（縮小時間窗口，幾秒內驗證）
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(ds.TABLE_DDL["margin_trading"])
    conn.execute(ds.TABLE_DDL["market_index"])
    conn.execute("""
        CREATE TABLE load_manifest (
            stock_id TEXT, dataset TEXT, row_count INTEGER,
            date_min TEXT, date_max TEXT, loaded_at TEXT,
            PRIMARY KEY (stock_id, dataset)
        )
    """)
    for sid in ["1101", "1102", "1103"]:
        conn.execute("INSERT INTO load_manifest (stock_id, dataset) VALUES (?, 'price')", (sid,))
    conn.commit()
    conn.close()
    return str(db_path)


def test_hibernate_and_resume_from_checkpoint(temp_db, monkeypatch):
    """
    模擬：1101 第一次呼叫撞 402（配額用盡）→ 應該休眠 → 醒來後重試 1101 成功
    → 繼續 1102、1103。驗證：
      (a) 1101 沒有因為 402 被跳過，最後有真的抓到資料（不是被誤判成 no_data）
      (b) 三檔股票最終都成功寫入
      (c) 從 checkpoint 繼續，不是整批重跑
    用縮小的時間窗口（2 秒）驗證，不必真的等一小時，也不打真實 API。
    """
    monkeypatch.setattr(ds, "_rate_limiter", ds.RateLimiter(per_call_sleep=0, limit=999, window_seconds=2))
    monkeypatch.setattr(ds, "download_lock", _noop_lock)

    call_log = []

    def fake_get(url, params, timeout):
        sid = params["data_id"]
        dataset = params["dataset"]
        call_log.append(sid)

        if dataset == ds.DATASET_PRICE:  # TAIEX，這個測試不關心，回沒有新資料
            return _make_response(200, {"status": 200, "data": []})

        if sid == "1101" and call_log.count("1101") == 1:
            return _make_response(402, {"status": 402})

        return _make_response(200, {"status": 200, "data": [
            {"date": "2015-01-05", "stock_id": sid,
             "MarginPurchaseTodayBalance": 100, "MarginPurchaseYesterdayBalance": 90,
             "ShortSaleTodayBalance": 10}
        ]})

    with patch("download_supplementary.requests.get", side_effect=fake_get):
        ds.download_all_supplementary(
            db_path=temp_db, token="fake_token", start="2015-01-01",
            force=True, skip_index=True,
        )

    conn = sqlite3.connect(temp_db)
    stocks_with_data = [r[0] for r in conn.execute(
        "SELECT DISTINCT stock_id FROM margin_trading ORDER BY stock_id"
    ).fetchall()]
    conn.close()

    assert stocks_with_data == ["1101", "1102", "1103"], (
        "1101 應該在休眠恢復後重試成功，三檔最終都應該有資料——"
        "如果 1101 缺席，代表 402 又被誤判成「沒有資料」跳過了"
    )
    assert call_log.count("1101") >= 2, "1101 應該至少被呼叫兩次（第一次 402，恢復後重試成功）"


def test_circuit_breaker_stops_after_max_cycles(temp_db, monkeypatch):
    """
    如果每次醒來後又立刻撞 402（模擬配額估計錯誤或一直被別的來源搶走），
    最多重試 MAX_QUOTA_RETRY_CYCLES 個週期就該放棄並正常結束，不能無限循環。
    """
    monkeypatch.setattr(ds, "_rate_limiter", ds.RateLimiter(per_call_sleep=0, limit=999, window_seconds=1))
    monkeypatch.setattr(ds, "download_lock", _noop_lock)

    call_log = []

    def always_402(url, params, timeout):
        call_log.append(params["data_id"])
        return _make_response(402, {"status": 402})

    with patch("download_supplementary.requests.get", side_effect=always_402):
        # 不應該丟出例外或無限跑，應該正常 return
        ds.download_all_supplementary(
            db_path=temp_db, token="fake_token", start="2015-01-01",
            force=True, skip_index=True, sid_filter="1101",
        )

    # 1101 應該被嘗試 MAX_QUOTA_RETRY_CYCLES 次左右就放棄，不是無限次
    assert 1 <= call_log.count("1101") <= ds.MAX_QUOTA_RETRY_CYCLES + 1

    conn = sqlite3.connect(temp_db)
    n = conn.execute("SELECT COUNT(*) FROM margin_trading").fetchone()[0]
    conn.close()
    assert n == 0, "全程都是 402，不應該有任何資料被寫入"
