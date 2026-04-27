"""
cleaner.py
─────────────────────────────────────────────────────────────
資料清洗引擎：對原始 DataFrame 套用 schema.py 的規則，
並回傳清洗後的 DataFrame + 稽核記錄。

設計原則：
- 非破壞性（不修改原始檔案）
- 每筆被處理的資料都有文字說明
- 輸入/輸出行數差異完全可追蹤
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

from schema import CLEANING_RULES, PRICE_COLS, VAL_COLS, REV_COLS


# ── 稽核記錄結構 ───────────────────────────────────────────────
@dataclass
class AuditEntry:
    rule_name:     str
    affected_rows: int
    sample_dates:  List[str]

@dataclass
class CleaningReport:
    stock_id:    str
    dataset:     str
    rows_before: int
    rows_after:  int
    entries:     List[AuditEntry] = field(default_factory=list)

    @property
    def rows_removed(self):
        return self.rows_before - self.rows_after


# ── 型別轉換 ───────────────────────────────────────────────────
def _cast(df: pd.DataFrame, col_types: dict) -> pd.DataFrame:
    df = df.copy()
    for col, dtype in col_types.items():
        if col not in df.columns:
            continue
        if dtype == "datetime64[ns]":
            df[col] = pd.to_datetime(df[col], errors="coerce")
        elif dtype in ("float64",):
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        elif dtype == "int64":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ── 主要清洗函式 ───────────────────────────────────────────────
def clean_price(df: pd.DataFrame, stock_id: str) -> Tuple[pd.DataFrame, CleaningReport]:
    report = CleaningReport(stock_id=stock_id, dataset="price",
                            rows_before=len(df), rows_after=0)
    df = _cast(df.copy(), PRICE_COLS)
    df = df.sort_values("date").drop_duplicates(subset="date", keep="last")

    # ── 清洗順序說明 ────────────────────────────────────────────
    # 停牌日的資料通常 OHLCV 全部為 0。
    # 如果先跑 OHLC 一致性檢查，會把「close=0 且 open/high/low 也是 0」
    # 的停牌日算進去，然後 price_zero_close 又算一次，造成稽核數字虛報。
    #
    # 正確順序：
    #   Step A：先移除明顯錯誤（zero close / zero volume）
    #   Step B：再對剩餘資料做 OHLC 一致性檢查
    # 這樣每一筆只被一條規則算到，稽核數字準確。

    # ── Step A：移除停牌日與無量資料 ───────────────────────────
    # Rule 1：收盤價為 0（停牌或資料缺漏）
    mask_zero_close = df["close"] == 0
    if mask_zero_close.sum() > 0:
        sample = df.loc[mask_zero_close, "date"].dt.strftime("%Y-%m-%d").head(5).tolist()
        report.entries.append(AuditEntry("price_zero_close",
                                         int(mask_zero_close.sum()), sample))
        df = df[~mask_zero_close]

    # Rule 2：成交量為 0 但收盤價正常（無量漲跌停）
    mask_zero_vol = (df["volume"] == 0) & (df["close"] != 0)
    if mask_zero_vol.sum() > 0:
        sample = df.loc[mask_zero_vol, "date"].dt.strftime("%Y-%m-%d").head(5).tolist()
        report.entries.append(AuditEntry("price_zero_volume",
                                         int(mask_zero_vol.sum()), sample))
        df = df[~mask_zero_vol]

    # ── Step B：OHLC 邏輯一致性（只在非零資料上跑）──────────────
    # 此時 df 已不含停牌日，任何 OHLC 不合邏輯都是真實資料錯誤
    mask_ohlc = (
        (df["high"] < df["low"])
        | (df["open"]  > df["high"]) | (df["close"] > df["high"])
        | (df["open"]  < df["low"])  | (df["close"] < df["low"])
    )
    if mask_ohlc.sum() > 0:
        sample = df.loc[mask_ohlc, "date"].dt.strftime("%Y-%m-%d").head(5).tolist()
        report.entries.append(AuditEntry("price_ohlc_check",
                                         int(mask_ohlc.sum()), sample))
        df = df[~mask_ohlc]

    df = df.reset_index(drop=True)
    report.rows_after = len(df)
    return df, report


def clean_val(df: pd.DataFrame, stock_id: str,
              price_dates: pd.DatetimeIndex) -> Tuple[pd.DataFrame, CleaningReport]:
    report = CleaningReport(stock_id=stock_id, dataset="val",
                            rows_before=len(df), rows_after=0)
    df = _cast(df.copy(), VAL_COLS)
    df = df.sort_values("date").drop_duplicates(subset="date", keep="last")

    # 對齊 price 的交易日
    extra = ~df["date"].isin(price_dates)
    if extra.sum() > 0:
        sample = df.loc[extra, "date"].dt.strftime("%Y-%m-%d").tolist()[:5]
        report.entries.append(AuditEntry("val_date_align", int(extra.sum()), sample))
        df = df[~extra]

    for rule in [r for r in CLEANING_RULES if r.dataset == "val"]:
        if rule.column not in df.columns:
            continue
        if rule.name == "val_per_zero":
            mask = df["PER"] == 0
        elif rule.name == "val_negative_yield":
            mask = df["dividend_yield"] < 0
        else:
            continue

        if mask.sum() > 0:
            sample = df.loc[mask, "date"].dt.strftime("%Y-%m-%d").head(5).tolist()
            report.entries.append(AuditEntry(rule.name, int(mask.sum()), sample))
            if rule.action == "set_nan":
                df.loc[mask, rule.column] = np.nan

    df = df.reset_index(drop=True)
    report.rows_after = len(df)
    return df, report


def clean_rev(df: pd.DataFrame, stock_id: str) -> Tuple[pd.DataFrame, CleaningReport]:
    report = CleaningReport(stock_id=stock_id, dataset="rev",
                            rows_before=len(df), rows_after=0)
    df = _cast(df.copy(), REV_COLS)
    df = df.sort_values("date").drop_duplicates(subset="date", keep="last")
    # 標準化成每月 1 日
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp()

    for rule in [r for r in CLEANING_RULES if r.dataset == "rev"]:
        if rule.name == "rev_negative":
            mask = df["revenue"].notna() & (df["revenue"] < 0)
            if mask.sum() > 0:
                sample = df.loc[mask, "date"].dt.strftime("%Y-%m").head(5).tolist()
                report.entries.append(AuditEntry(rule.name, int(mask.sum()), sample))
                if rule.action == "set_nan":
                    df.loc[mask, "revenue"] = pd.NA

    df = df.reset_index(drop=True)
    report.rows_after = len(df)
    return df, report