"""
schema.py
─────────────────────────────────────────────────────────────
資料表定義 + 清洗規則的單一事實來源（Single Source of Truth）

所有欄位名稱、型別、業務邏輯集中在這裡。
修改清洗規則只需改這個檔案，其他程式不用動。
"""

from dataclasses import dataclass, field
from typing import List, Optional

# ── 欄位型別定義 ──────────────────────────────────────────────
PRICE_COLS = {
    "date":   "datetime64[ns]",
    "open":   "float64",
    "high":   "float64",
    "low":    "float64",
    "close":  "float64",
    "volume": "int64",
}

VAL_COLS = {
    "date":           "datetime64[ns]",
    "dividend_yield": "float64",
    "PER":            "float64",
    "PBR":            "float64",
}

REV_COLS = {
    "date":    "datetime64[ns]",
    "revenue": "int64",
}

# ── 清洗規則定義 ──────────────────────────────────────────────
@dataclass
class CleaningRule:
    name:       str
    dataset:    str
    column:     str
    condition:  str
    action:     str          # 'drop_row' | 'set_nan'
    rationale:  str

CLEANING_RULES: List[CleaningRule] = [

    CleaningRule(
        name="price_zero_close", dataset="price", column="close",
        condition="close == 0", action="drop_row",
        rationale=(
            "收盤價為 0 代表交易暫停或資料缺漏。"
            "保留會讓報酬率計算出現 -100% 或 inf，污染所有技術因子。"
        ),
    ),
    CleaningRule(
        name="price_zero_volume", dataset="price", column="volume",
        condition="volume == 0 AND close != 0", action="drop_row",
        rationale=(
            "成交量為 0 但收盤價不為 0，屬於無量漲跌停或資料異常。"
            "此類交易日無法在實盤執行，回測中保留會高估流動性。"
        ),
    ),
    CleaningRule(
        name="price_ohlc_check", dataset="price", column="high",
        condition="high < low or open/close outside [low, high]", action="drop_row",
        rationale="OHLC 邏輯不一致，屬於資料來源錯誤，無法修正，直接移除。",
    ),
    CleaningRule(
        name="val_per_zero", dataset="val", column="PER",
        condition="PER == 0", action="set_nan",
        rationale=(
            "PER=0 代表當年虧損（EPS≤0）或財報未更新，不是真實市場估值。"
            "若保留為 0，z-score 標準化時會產生極端值主導因子訊號。"
            "轉為 NaN 讓 pandas 在排名時自動忽略。"
        ),
    ),
    CleaningRule(
        name="val_negative_yield", dataset="val", column="dividend_yield",
        condition="dividend_yield < 0", action="set_nan",
        rationale="殖利率不可能為負，視為資料來源錯誤。",
    ),
    CleaningRule(
        name="rev_negative", dataset="rev", column="revenue",
        condition="revenue < 0", action="set_nan",
        rationale="月營收為負在此資料集中視為缺漏值，轉為 NaN。",
    ),
]

# ── SQLite 建表語句 ────────────────────────────────────────────
TABLE_DDL = {
    "daily_price": """
        CREATE TABLE IF NOT EXISTS daily_price (
            date        TEXT NOT NULL,
            stock_id    TEXT NOT NULL,
            open        REAL NOT NULL,
            high        REAL NOT NULL,
            low         REAL NOT NULL,
            close       REAL NOT NULL,
            volume      INTEGER NOT NULL,
            PRIMARY KEY (date, stock_id)
        )""",

    "daily_valuation": """
        CREATE TABLE IF NOT EXISTS daily_valuation (
            date            TEXT NOT NULL,
            stock_id        TEXT NOT NULL,
            dividend_yield  REAL,
            PER             REAL,
            PBR             REAL,
            PRIMARY KEY (date, stock_id)
        )""",

    "monthly_revenue": """
        CREATE TABLE IF NOT EXISTS monthly_revenue (
            date        TEXT NOT NULL,
            stock_id    TEXT NOT NULL,
            revenue     INTEGER,
            PRIMARY KEY (date, stock_id)
        )""",

    "data_quality_log": """
        CREATE TABLE IF NOT EXISTS data_quality_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_id      TEXT,
            dataset       TEXT,
            rule_name     TEXT,
            affected_rows INTEGER,
            logged_at     TEXT DEFAULT (datetime('now','localtime'))
        )""",

    "load_manifest": """
        CREATE TABLE IF NOT EXISTS load_manifest (
            stock_id    TEXT NOT NULL,
            dataset     TEXT NOT NULL,
            row_count   INTEGER,
            date_min    TEXT,
            date_max    TEXT,
            loaded_at   TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (stock_id, dataset)
        )""",

    "institutional_investors": """
        CREATE TABLE IF NOT EXISTS institutional_investors (
            date          TEXT NOT NULL,
            stock_id      TEXT NOT NULL,
            investor_type TEXT NOT NULL,
            buy           REAL,
            sell          REAL,
            net           REAL,
            PRIMARY KEY (date, stock_id, investor_type)
        )""",

    "margin_trading": """
        CREATE TABLE IF NOT EXISTS margin_trading (
            date            TEXT NOT NULL,
            stock_id        TEXT NOT NULL,
            margin_balance  REAL,
            short_balance   REAL,
            margin_change   REAL,
            PRIMARY KEY (date, stock_id)
        )""",

    "market_index": """
        CREATE TABLE IF NOT EXISTS market_index (
            date        TEXT NOT NULL,
            index_id    TEXT NOT NULL,
            open        REAL,
            high        REAL,
            low         REAL,
            close       REAL,
            volume      REAL,
            PRIMARY KEY (date, index_id)
        )""",

    "delisted_stocks": """
        CREATE TABLE IF NOT EXISTS delisted_stocks (
            stock_id          TEXT NOT NULL,
            first_seen_date   TEXT,
            last_seen_date    TEXT,
            days_stale        INTEGER,
            detection_method  TEXT NOT NULL,
            confidence        TEXT NOT NULL,
            note              TEXT,
            detected_at       TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (stock_id, detection_method)
        )""",
}

TABLE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_price_sid  ON daily_price(stock_id)",
    "CREATE INDEX IF NOT EXISTS idx_price_date ON daily_price(date)",
    "CREATE INDEX IF NOT EXISTS idx_val_sid    ON daily_valuation(stock_id)",
    "CREATE INDEX IF NOT EXISTS idx_rev_sid    ON monthly_revenue(stock_id)",
    "CREATE INDEX IF NOT EXISTS idx_inst_sid   ON institutional_investors(stock_id)",
    "CREATE INDEX IF NOT EXISTS idx_margin_sid  ON margin_trading(stock_id)",
    "CREATE INDEX IF NOT EXISTS idx_margin_date ON margin_trading(date)",
    "CREATE INDEX IF NOT EXISTS idx_mktidx_id   ON market_index(index_id)",
    "CREATE INDEX IF NOT EXISTS idx_mktidx_date ON market_index(date)",
    "CREATE INDEX IF NOT EXISTS idx_inst_date  ON institutional_investors(date)",
]