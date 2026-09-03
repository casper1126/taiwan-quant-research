# 本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，使用構造資料屬正常做法
"""
tests/test_daily_update.py

驗證 automation/daily_update.py（Task 8）的邏輯正確性：
  - 每股票／每表獨立的 checkpoint 計算（_get_last_date）
  - 402 判定必須在 raise_for_status() 之前（跟 Task 1 402 事故同一種測法）
  - INSERT OR REPLACE 讓同一區間可以安全重跑，不會因為 UNIQUE constraint
    被誤判成失敗
  - 機制降級偵測（compute_regime_signal 的核心邏輯）

全部用合成資料 + mock，不打真實 FinMind API、不需要真的跑 regime_engine。
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "automation"))
sys.path.insert(0, str(Path(__file__).parent.parent / "data_pipeline"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import daily_update as du
from data_pipeline.finmind_common import QuotaExhaustedError


def _make_test_db(tmp_path) -> str:
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE daily_price (
            date TEXT, stock_id TEXT, open REAL, high REAL, low REAL,
            close REAL, volume INTEGER, PRIMARY KEY (date, stock_id)
        )
    """)
    conn.execute("""
        CREATE TABLE daily_valuation (
            date TEXT, stock_id TEXT, dividend_yield REAL, PER REAL, PBR REAL,
            PRIMARY KEY (date, stock_id)
        )
    """)
    conn.execute(
        "INSERT INTO daily_price VALUES ('2026-08-01','2330',100,101,99,100,1000)"
    )
    conn.execute(
        "INSERT INTO daily_valuation VALUES ('2026-07-28','2330',0.02,15.0,3.0)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_get_last_date_per_stock_and_table_are_independent(tmp_path):
    """
    價格跟估值各自有自己的 checkpoint，即使同一支股票的兩張表進度不同步
    （例如之前配額用盡時只有價格寫成功），也能各自正確算出續抓的起點，
    不會被對方的進度蓋過去。
    """
    db_path = _make_test_db(tmp_path)
    conn = sqlite3.connect(db_path)

    price_start = du._get_last_date(conn, "daily_price", "2330")
    val_start = du._get_last_date(conn, "daily_valuation", "2330")

    assert price_start == "2026-08-02"   # 2026-08-01 的隔天
    assert val_start == "2026-07-29"     # 2026-07-28 的隔天（明顯落後價格）
    assert price_start != val_start

    # 沒有任何資料的股票 → fallback 到 default
    fallback = du._get_last_date(conn, "daily_price", "9999", default="2010-01-01")
    assert fallback == "2010-01-01"
    conn.close()


def test_finmind_get_raises_quota_exhausted_on_http_402():
    """
    402 判定必須在 resp.raise_for_status() 之前——跟 Task 1 402 事故的
    修復是同一種測法（tests/test_download_supplementary.py 也有一份
    幾乎一樣的斷言，這裡是 daily_update.py 自己那份 _finmind_get 的
    對應測試，避免它又悄悄長出同一種 bug）。
    """
    mock_resp = MagicMock()
    mock_resp.status_code = 402

    with patch.object(du.requests, "get", return_value=mock_resp), \
         patch.object(du._rate_limiter, "acquire"):
        with pytest.raises(QuotaExhaustedError):
            du._finmind_get("TaiwanStockPrice", "2330", "2026-08-01", "2026-08-28")


def test_finmind_get_returns_dataframe_on_200():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "status": 200,
        "data": [{"date": "2026-08-01", "stock_id": "2330", "close": 1000}],
    }
    mock_resp.raise_for_status = MagicMock()

    with patch.object(du.requests, "get", return_value=mock_resp), \
         patch.object(du._rate_limiter, "acquire"):
        df = du._finmind_get("TaiwanStockPrice", "2330", "2026-08-01", "2026-08-28")

    assert not df.empty
    assert df.iloc[0]["stock_id"] == "2330"


def test_upsert_one_price_valuation_is_idempotent(tmp_path):
    """
    用 INSERT OR REPLACE：同一段區間跑兩次不應該拋 UNIQUE constraint
    例外（這是原本 pandas to_sql(if_exists="append") 會有的問題）。
    """
    db_path = _make_test_db(tmp_path)
    conn = sqlite3.connect(db_path)

    from data_pipeline.loader import CSVLoader
    loader = CSVLoader(db_path)

    fake_price = pd.DataFrame([
        {"date": "2026-08-02", "open": 101, "max": 102, "min": 100,
         "close": 101.5, "Trading_Volume": 2000},
    ])
    fake_val = pd.DataFrame([
        {"date": "2026-07-29", "dividend_yield": 0.02, "PER": 15.2, "PBR": 3.1},
    ])

    with patch.object(du, "_finmind_get", side_effect=[fake_price, fake_val]):
        status1 = du._upsert_one_price_valuation(loader, conn, "2330", "2026-08-28")
    assert status1 == "ok"

    # 重跑同一段區間（模擬 402 休眠恢復後重試同一檔）：不該報錯
    with patch.object(du, "_finmind_get", side_effect=[fake_price, fake_val]):
        status2 = du._upsert_one_price_valuation(loader, conn, "2330", "2026-08-28")
    assert status2 == "ok"

    count = conn.execute(
        "SELECT COUNT(*) FROM daily_price WHERE date='2026-08-02' AND stock_id='2330'"
    ).fetchone()[0]
    assert count == 1   # 沒有重複列
    conn.close()


def test_compute_regime_signal_detects_downgrade():
    """
    降級偵測：今天的機制排名比昨天低就是 downgraded=True。用合成的
    regime_engine 輸出（不用真的跑 HMM/LightGBM），只驗證
    compute_regime_signal() 自己的降級判斷邏輯。
    """
    dates = pd.date_range("2026-08-20", periods=5)
    fake_df = pd.DataFrame({
        "health": [70, 65, 60, 40, 25],
        "p_bear": [0.1, 0.1, 0.2, 0.3, 0.6],
        "crash_prob": [0.05, 0.05, 0.1, 0.2, 0.4],
        "regime": ["BULL", "BULL", "NEUTRAL", "WARNING", "BEAR"],
        "exposure": [1.0, 1.0, 0.7, 0.4, 0.1],
    }, index=dates)

    with patch.object(du, "run_regime_engine", return_value=(fake_df, {})):
        info, regime_df = du.compute_regime_signal()

    assert info["regime"] == "BEAR"
    assert info["previous_regime"] == "WARNING"
    assert info["downgraded"] is True
    assert info["health_score"] == 25.0


def test_compute_regime_signal_no_downgrade_when_improving():
    dates = pd.date_range("2026-08-20", periods=2)
    fake_df = pd.DataFrame({
        "health": [40, 65],
        "p_bear": [0.3, 0.1],
        "crash_prob": [0.2, 0.05],
        "regime": ["WARNING", "BULL"],
        "exposure": [0.4, 1.0],
    }, index=dates)

    with patch.object(du, "run_regime_engine", return_value=(fake_df, {})):
        info, _ = du.compute_regime_signal()

    assert info["regime"] == "BULL"
    assert info["downgraded"] is False


def test_compute_regime_signal_handles_all_nan_gracefully():
    dates = pd.date_range("2026-08-20", periods=2)
    fake_df = pd.DataFrame({
        "health": [None, None], "p_bear": [None, None], "crash_prob": [None, None],
        "regime": [None, None], "exposure": [None, None],
    }, index=dates)

    with patch.object(du, "run_regime_engine", return_value=(fake_df, {})):
        info, _ = du.compute_regime_signal()

    assert info["regime"] is None
    assert info["downgraded"] is False
