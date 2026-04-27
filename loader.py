"""
loader.py
─────────────────────────────────────────────────────────────
CSV → SQLite 載入器

支援的命名格式（對應你的三種檔案）：
  {stock_id}_{start}_{end}.csv    → 日頻價格（如 1101_2010-01-01_2026-04-20.csv）
  {stock_id}_rev.csv              → 月頻營收（如 1101_rev.csv）
  {stock_id}_val.csv              → 日頻估值（如 1101_val.csv）

使用範例：
  from loader import CSVLoader
  loader = CSVLoader("data/taiwan_stock.db")
  loader.load_directory("raw_data/")   # 批次載入整個資料夾
"""

import re
import sqlite3
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional

import pandas as pd
from tqdm import tqdm
from loguru import logger

from schema import TABLE_DDL, TABLE_INDEXES
from cleaner import clean_price, clean_val, clean_rev

# ── 檔名解析 regex ────────────────────────────────────────────
_PRICE_RE = re.compile(
    r"^(?P<sid>\d{4,6})_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.csv$"
)
_REV_RE = re.compile(r"^(?P<sid>\d{4,6})_rev\.csv$")
_VAL_RE = re.compile(r"^(?P<sid>\d{4,6})_val\.csv$")


def _scan(directory: str) -> Dict[str, Dict[str, Path]]:
    """掃描資料夾，把 CSV 按股票代號 + 類型分組"""
    result: Dict[str, Dict[str, Path]] = defaultdict(dict)
    for f in Path(directory).iterdir():
        if not f.is_file() or f.suffix != ".csv":
            continue
        for pattern, key in [(_PRICE_RE, "price"), (_REV_RE, "rev"), (_VAL_RE, "val")]:
            m = pattern.match(f.name)
            if m:
                result[m.group("sid")][key] = f
                break
    return dict(result)


class CSVLoader:
    """台股 CSV → SQLite 載入器"""

    def __init__(self, db_path: str = "data/taiwan_stock.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── DB 初始化 ─────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init_db(self):
        with self._conn() as c:
            for ddl in TABLE_DDL.values():
                c.execute(ddl)
            for idx in TABLE_INDEXES:
                c.execute(idx)

    # ── 寫入輔助 ──────────────────────────────────────────────
    def _upsert(self, conn, table: str, df: pd.DataFrame, stock_id: str,
                cols: List[str]):
        df = df.copy()
        df["stock_id"] = stock_id
        # date 欄轉字串
        if "date" in df.columns:
            df["date"] = df["date"].dt.strftime("%Y-%m-%d")
        available = [c for c in cols if c in df.columns]
        rows = df[available].values.tolist()
        ph = ",".join("?" * len(available))
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(available)}) VALUES ({ph})",
            rows,
        )

    def _log_manifest(self, conn, stock_id, dataset, df):
        conn.execute(
            "INSERT OR REPLACE INTO load_manifest "
            "(stock_id, dataset, row_count, date_min, date_max) VALUES (?,?,?,?,?)",
            (stock_id, dataset, len(df),
             str(df["date"].min())[:10], str(df["date"].max())[:10]),
        )

    def _log_quality(self, conn, report):
        for e in report.entries:
            conn.execute(
                "INSERT INTO data_quality_log (stock_id, dataset, rule_name, affected_rows)"
                " VALUES (?,?,?,?)",
                (report.stock_id, report.dataset, e.rule_name, e.affected_rows),
            )

    # ── 單檔載入 ──────────────────────────────────────────────
    def load_stock(self, stock_id: str,
                   price_file: str = "", rev_file: str = "", val_file: str = "",
                   overwrite: bool = False) -> list:

        if not overwrite and self._loaded(stock_id):
            return []

        reports = []
        price_dates = pd.DatetimeIndex([])

        with self._conn() as conn:
            if overwrite:
                for t in ["daily_price", "daily_valuation", "monthly_revenue"]:
                    conn.execute(f"DELETE FROM {t} WHERE stock_id=?", (stock_id,))
                conn.execute("DELETE FROM load_manifest WHERE stock_id=?", (stock_id,))

            # 價格
            if price_file and Path(price_file).exists():
                raw = pd.read_csv(price_file)
                df, rpt = clean_price(raw, stock_id)
                reports.append(rpt)
                if not df.empty:
                    self._upsert(conn, "daily_price", df, stock_id,
                                 ["date","stock_id","open","high","low","close","volume"])
                    self._log_manifest(conn, stock_id, "price", df)
                    price_dates = df["date"]
                    logger.info(f"  [{stock_id}] price: {rpt.rows_before:,} → {rpt.rows_after:,} "
                                f"(移除 {rpt.rows_removed})")

            # 估值
            if val_file and Path(val_file).exists():
                raw = pd.read_csv(val_file)
                p_dates = pd.to_datetime(price_dates) if len(price_dates) else pd.DatetimeIndex([])
                df, rpt = clean_val(raw, stock_id, p_dates)
                reports.append(rpt)
                if not df.empty:
                    self._upsert(conn, "daily_valuation", df, stock_id,
                                 ["date","stock_id","dividend_yield","PER","PBR"])
                    self._log_manifest(conn, stock_id, "val", df)
                    nan_count = sum(e.affected_rows for e in rpt.entries)
                    logger.info(f"  [{stock_id}] val  : {rpt.rows_before:,} → {rpt.rows_after:,} "
                                f"(NaN處理 {nan_count})")

            # 營收
            if rev_file and Path(rev_file).exists():
                raw = pd.read_csv(rev_file)
                df, rpt = clean_rev(raw, stock_id)
                reports.append(rpt)
                if not df.empty:
                    self._upsert(conn, "monthly_revenue", df, stock_id,
                                 ["date","stock_id","revenue"])
                    self._log_manifest(conn, stock_id, "rev", df)
                    logger.info(f"  [{stock_id}] rev  : {rpt.rows_before:,} → {rpt.rows_after:,}")

            for r in reports:
                self._log_quality(conn, r)

        return reports

    # ── 批次載入 ──────────────────────────────────────────────
    def load_directory(self, directory: str, overwrite: bool = False):
        stock_files = _scan(directory)
        if not stock_files:
            logger.error(f"在 {directory} 找不到符合格式的 CSV。")
            logger.error("  期望格式：{id}_{start}_{end}.csv / {id}_rev.csv / {id}_val.csv")
            return

        logger.info(f"掃描到 {len(stock_files)} 檔股票，開始載入...")
        failed = []

        for sid, files in tqdm(stock_files.items(), desc="載入進度", ncols=70):
            try:
                self.load_stock(
                    stock_id=sid,
                    price_file=str(files.get("price", "")),
                    rev_file=str(files.get("rev", "")),
                    val_file=str(files.get("val", "")),
                    overwrite=overwrite,
                )
            except Exception as e:
                logger.error(f"  {sid} 失敗：{e}")
                failed.append(sid)

        self.print_stats()
        if failed:
            logger.warning(f"失敗清單（{len(failed)} 檔）：{failed[:20]}")

    # ── 查詢 ──────────────────────────────────────────────────
    def _loaded(self, sid: str) -> bool:
        with self._conn() as c:
            return c.execute(
                "SELECT 1 FROM load_manifest WHERE stock_id=? LIMIT 1", (sid,)
            ).fetchone() is not None

    def read_merged(self, stock_id: str,
                    start: str = "2015-01-01", end: Optional[str] = None) -> pd.DataFrame:
        """
        讀取某股票的合併資料（price + val + revenue ffill）。
        這是 Layer 2 因子引擎的標準輸入。
        """
        from pandas.tseries.offsets import DateOffset

        end_clause = f"AND date <= '{end}'" if end else ""
        with self._conn() as c:
            price = pd.read_sql(
                f"SELECT * FROM daily_price WHERE stock_id=? AND date>=? {end_clause} ORDER BY date",
                c, params=(stock_id, start), parse_dates=["date"])
            val = pd.read_sql(
                f"SELECT date,dividend_yield,PER,PBR FROM daily_valuation "
                f"WHERE stock_id=? AND date>=? {end_clause} ORDER BY date",
                c, params=(stock_id, start), parse_dates=["date"])
            rev = pd.read_sql(
                "SELECT date,revenue FROM monthly_revenue WHERE stock_id=? ORDER BY date",
                c, params=(stock_id,), parse_dates=["date"])

        if price.empty:
            return pd.DataFrame()

        merged = price.merge(val, on="date", how="left")

        # Revenue forward-fill（月頻 → 日頻，延遲 40 天防 look-ahead bias）
        if not rev.empty:
            rev_idx = rev.set_index("date")["revenue"].sort_index()
            yoy = rev_idx.pct_change(12)
            # 延遲 40 天才允許使用（模擬公告日）
            yoy.index = yoy.index + DateOffset(days=40)
            daily_yoy = yoy.reindex(merged["date"], method="ffill")

            rev_aligned = rev_idx.reindex(merged["date"], method="ffill")
            merged["revenue"]     = rev_aligned.values
            merged["revenue_yoy"] = daily_yoy.values

        return merged.sort_values("date").reset_index(drop=True)

    def get_price_matrix(self, start: str = "2015-01-01",
                         end: Optional[str] = None) -> pd.DataFrame:
        """所有股票的收盤價寬格式矩陣（index=date, columns=stock_id）"""
        end_clause = f"AND date <= '{end}'" if end else ""
        with self._conn() as c:
            df = pd.read_sql(
                f"SELECT date, stock_id, close FROM daily_price "
                f"WHERE date >= ? {end_clause} ORDER BY date",
                c, params=(start,), parse_dates=["date"])
        return df.pivot(index="date", columns="stock_id", values="close")

    def print_stats(self):
        with self._conn() as c:
            print("\n" + "="*50)
            print("  資料庫統計")
            print("="*50)
            for tbl in ["daily_price", "daily_valuation", "monthly_revenue"]:
                row = c.execute(
                    f"SELECT COUNT(*), COUNT(DISTINCT stock_id) FROM {tbl}"
                ).fetchone()
                print(f"  {tbl:<25} {row[0]:>10,} 筆  {row[1]:>5} 檔")
            quality = c.execute(
                "SELECT rule_name, SUM(affected_rows) FROM data_quality_log GROUP BY rule_name"
            ).fetchall()
            if quality:
                print("\n  清洗摘要：")
                for rule, cnt in quality:
                    print(f"    {rule:<30} {cnt:>8,} 筆")
            print("="*50 + "\n")