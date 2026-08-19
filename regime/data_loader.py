"""
regime/data_loader.py
─────────────────────────────────────────────────────────────
機制偵測模組專用的資料載入器。

刻意獨立於 strategy/quant_layer2.py 的 load_matrices()：
Task 3-4 的設計要求是「regime/ 只能整合進 quant_layer2.py（被它呼叫），
不能反向依賴它」，所以這裡直接從 SQLite 重新讀取，不 import quant_layer2。
"""

import sqlite3
from typing import Dict

import numpy as np
import pandas as pd

DB_PATH = "data/taiwan_stock.db"


def load_regime_inputs(db_path: str = DB_PATH,
                       start: str = "2015-01-01",
                       end: str = "2026-12-31") -> Dict[str, object]:
    """
    載入機制偵測所需的全部原始資料，回傳給 regime_engine.py 使用。

    回傳字典：
      close        : pd.DataFrame  個股收盤價（date x stock_id）
      volume       : pd.DataFrame  個股成交量（date x stock_id）
      returns      : pd.DataFrame  個股日報酬（date x stock_id）
      index_close  : pd.Series     TAIEX 收盤價
      index_volume : pd.Series     TAIEX 成交量
      inst_foreign : pd.DataFrame  外資（不含投信）逐股 buy/sell/net 加總後的
                     市場層級 Series（見 foreign_market_flow）
      margin_total : pd.Series     全市場融資餘額加總
      margin_balance: pd.DataFrame 個股融資餘額（date x stock_id，供 margin_capitulation
                     以外的用途保留，目前指標集不直接用）
    """
    conn = sqlite3.connect(db_path)

    price_df = pd.read_sql(
        "SELECT date, stock_id, close, volume FROM daily_price "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    close = price_df.pivot(index="date", columns="stock_id", values="close")
    volume = price_df.pivot(index="date", columns="stock_id", values="volume")
    returns = close.pct_change()

    index_df = pd.read_sql(
        "SELECT date, close, volume FROM market_index WHERE index_id = 'TAIEX' "
        "AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    if not index_df.empty:
        index_df = index_df.set_index("date")
        index_close = index_df["close"]
        index_volume = index_df["volume"]
    else:
        index_close = pd.Series(dtype=float)
        index_volume = pd.Series(dtype=float)

    # 外資市場層級買賣超：只取 Foreign_Investor + Foreign_Dealer_Self
    # （不含 Investment_Trust投信，foreign_flow_pressure 指標定義上只看外資）
    inst_df = pd.read_sql(
        """SELECT date,
                  SUM(buy)  AS buy,
                  SUM(sell) AS sell,
                  SUM(net)  AS net
           FROM institutional_investors
           WHERE investor_type IN ('Foreign_Investor', 'Foreign_Dealer_Self')
             AND date BETWEEN ? AND ?
           GROUP BY date
           ORDER BY date""",
        conn, params=(start, end), parse_dates=["date"],
    )
    if not inst_df.empty:
        inst_foreign = inst_df.set_index("date")[["buy", "sell", "net"]]
    else:
        inst_foreign = pd.DataFrame(columns=["buy", "sell", "net"])

    margin_df = pd.read_sql(
        "SELECT date, stock_id, margin_balance FROM margin_trading "
        "WHERE date BETWEEN ? AND ? ORDER BY date",
        conn, params=(start, end), parse_dates=["date"],
    )
    if not margin_df.empty:
        margin_balance = margin_df.pivot(index="date", columns="stock_id", values="margin_balance")
        margin_total = margin_balance.sum(axis=1, min_count=1)
    else:
        margin_balance = pd.DataFrame()
        margin_total = pd.Series(dtype=float)

    conn.close()

    return {
        "close": close,
        "volume": volume,
        "returns": returns,
        "index_close": index_close,
        "index_volume": index_volume,
        "inst_foreign": inst_foreign,
        "margin_total": margin_total,
        "margin_balance": margin_balance,
    }
