"""
run.py
─────────────────────────────────────────────────────────────
主執行檔：按步驟完成整個流程。

使用方式：
    python run.py              ← 執行全部步驟（1 + 1b + 2）
    python run.py --step 1     ← Step 1：載入 CSV → SQLite
    python run.py --step 1b    ← Step 1b：下載三大法人資料（需 FINMIND_TOKEN）
    python run.py --step 2     ← Step 2：多因子回測
    python run.py --step 3     ← Step 3：Walk-Forward 驗證
    python run.py --stats      ← 查看資料庫統計
    python run.py --verify 2330 ← 驗證單一股票資料
"""

import argparse
import os
import sys
from loguru import logger

sys.path.insert(0, "data_pipeline")
sys.path.insert(0, "strategy")
sys.path.insert(0, "automation")

logger.remove()
logger.add(sys.stdout, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")


# ── Step 1：載入所有 CSV → SQLite ─────────────────────────────
def step1_load(csv_dir: str = "raw_data", overwrite: bool = False) -> None:
    """
    把 raw_data/ 裡的所有 CSV 載入 SQLite。

    CSV 命名格式：
      {id}_{start}_{end}.csv  → 日頻價格
      {id}_rev.csv            → 月頻營收
      {id}_val.csv            → 日頻估值
    """
    from loader import CSVLoader
    logger.info(f"=== Step 1：載入 CSV 目錄 [{csv_dir}] ===")
    loader = CSVLoader("data/taiwan_stock.db")
    loader.load_directory(csv_dir, overwrite=overwrite)


# ── Step 1b：下載三大法人資料 + 融資融券 + TAIEX ──────────────────
def step1b_institutional(start: str = "2015-01-01",
                          force: bool = False,
                          sid: str = None,
                          workers: int = 1) -> None:
    """
    從 FinMind 增量下載三大法人買賣超、融資融券、TAIEX 大盤指數。

    需要先執行 Step 1 建立股票清單，並設定環境變數 FINMIND_TOKEN。
    法人買賣超由 download_institutional.py 負責（支援並行）；
    融資融券 + TAIEX 由 download_supplementary.py 負責（固定節奏速率限制）。

    Parameters
    ----------
    workers : 法人資料並行下載數（預設 1）。建議 2–4 以平衡速度與 API 限制。
    """
    from download_institutional import download_all
    from download_supplementary import download_all_supplementary
    token = os.getenv("FINMIND_TOKEN", "")
    if not token:
        logger.error("請設定環境變數 FINMIND_TOKEN（FinMind 免費帳號可申請）")
        return

    logger.info("=== Step 1b-1：下載三大法人資料 ===")
    download_all(
        db_path    = "data/taiwan_stock.db",
        token      = token,
        start      = start,
        force      = force,
        sid_filter = sid,
        workers    = workers,
    )

    logger.info("=== Step 1b-2：下載融資融券 + TAIEX ===")
    download_all_supplementary(
        db_path    = "data/taiwan_stock.db",
        token      = token,
        start      = start,
        force      = force,
        sid_filter = sid,
    )


# ── Step 2：執行回測 ──────────────────────────────────────────
def step2_backtest() -> None:
    """執行 Layer 2 多因子回測"""
    logger.info("=== Step 2：執行多因子回測 ===")
    import runpy
    runpy.run_path("strategy/quant_layer2.py", run_name="__main__")


# ── Step 3：Walk-Forward 驗證 ────────────────────────────────
def step3_walkforward() -> None:
    """執行 Layer 3 Walk-Forward Out-of-Sample 驗證"""
    logger.info("=== Step 3：Walk-Forward 驗證 ===")
    import runpy
    runpy.run_path("strategy/walk_forward.py", run_name="__main__")


# ── 工具指令 ──────────────────────────────────────────────────
def cmd_stats() -> None:
    from loader import CSVLoader
    CSVLoader("data/taiwan_stock.db").print_stats()


def cmd_verify(stock_id: str) -> None:
    import numpy as np
    from loader import CSVLoader
    loader = CSVLoader("data/taiwan_stock.db")
    df = loader.read_merged(stock_id)
    if df.empty:
        logger.error(f"{stock_id} 無資料，請先執行 Step 1")
        return

    print(f"\n{'='*50}")
    print(f"  {stock_id} 資料驗證")
    print(f"{'='*50}")
    print(f"  筆數       : {len(df):,}")
    print(f"  日期範圍   : {df['date'].min().date()} ~ {df['date'].max().date()}")
    null_pct = (df.isnull().sum() / len(df) * 100).round(1)
    nz = null_pct[null_pct > 0]
    print(f"  缺漏值     : { {c: f'{p}%' for c, p in nz.items()} if len(nz) else '無' }")

    close     = df.set_index("date")["close"].sort_index()
    daily_ret = close.pct_change().dropna()
    ann_ret   = (1 + daily_ret.mean()) ** 252 - 1
    ann_vol   = daily_ret.std() * (252 ** 0.5)
    mdd       = (close / close.cummax() - 1).min()
    print(f"  年化報酬   : {ann_ret*100:.1f}%")
    print(f"  年化波動度 : {ann_vol*100:.1f}%")
    print(f"  Sharpe     : {(ann_ret - 0.015) / ann_vol:.2f}")
    print(f"  最大回撤   : {mdd*100:.1f}%")
    print(f"\n  最後 3 筆：")
    cols = [c for c in ["date", "close", "volume", "PER", "PBR"] if c in df.columns]
    print(df[cols].tail(3).to_string(index=False))


# ── 主程式 ────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股量化系統")
    parser.add_argument("--step",      type=str,  help="執行指定步驟 (1 / 1b / 2 / 3)")
    parser.add_argument("--dir",       default="raw_data", help="CSV 目錄 (預設 raw_data)")
    parser.add_argument("--overwrite", action="store_true", help="覆蓋已載入的資料")
    parser.add_argument("--start",     default="2015-01-01", help="法人資料起始日")
    parser.add_argument("--force",     action="store_true",  help="強制重新下載法人資料")
    parser.add_argument("--sid",       type=str,  help="只下載指定股票的法人資料")
    parser.add_argument("--workers",   type=int, default=1, help="並行下載數（預設 1）")
    parser.add_argument("--stats",     action="store_true",  help="查看資料庫統計")
    parser.add_argument("--verify",    type=str,  help="驗證單一股票，如 --verify 2330")
    args = parser.parse_args()

    if args.stats:
        cmd_stats()
    elif args.verify:
        cmd_verify(args.verify)
    elif args.step == "1":
        step1_load(args.dir, args.overwrite)
    elif args.step == "1b":
        step1b_institutional(args.start, args.force, args.sid, args.workers)
    elif args.step == "2":
        step2_backtest()
    elif args.step == "3":
        step3_walkforward()
    else:
        logger.info("執行完整流程（Step 1 + Step 2）")
        step1_load(args.dir, args.overwrite)
        step2_backtest()
