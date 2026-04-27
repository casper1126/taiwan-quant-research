"""
run.py
─────────────────────────────────────────────────────────────
主執行檔：按步驟完成整個流程。

使用方式：
    python run.py            ← 執行全部步驟
    python run.py --step 1   ← 只執行 Step 1（載入 CSV）
    python run.py --step 2   ← 只執行 Step 2（回測）
    python run.py --stats    ← 查看資料庫統計
    python run.py --verify 1101  ← 驗證單一股票資料
"""

import argparse
import sys
from loguru import logger

# 日誌設定
logger.remove()
logger.add(sys.stdout, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")

# ── Step 1：載入所有 CSV → SQLite ─────────────────────────────
def step1_load(csv_dir: str = "raw_data", overwrite: bool = False):
    """
    把 raw_data/ 裡的所有 CSV 載入 SQLite。

    你的 CSV 命名格式：
      1101_2010-01-01_2026-04-20.csv  → 日頻價格
      1101_rev.csv                    → 月頻營收
      1101_val.csv                    → 日頻估值
    """
    from loader import CSVLoader
    logger.info(f"=== Step 1：載入 CSV 目錄 [{csv_dir}] ===")
    loader = CSVLoader("data/taiwan_stock.db")
    loader.load_directory(csv_dir, overwrite=overwrite)


# ── Step 2：執行回測 ──────────────────────────────────────────
def step2_backtest():
    """執行 Layer 2 因子回測"""
    logger.info("=== Step 2：執行多因子回測 ===")
    import quant_layer2   # 直接執行 __main__ 內容
    import runpy
    runpy.run_module("quant_layer2", run_name="__main__")


# ── 工具指令 ──────────────────────────────────────────────────
def cmd_stats():
    from loader import CSVLoader
    CSVLoader("data/taiwan_stock.db").print_stats()


def cmd_verify(stock_id: str):
    import numpy as np
    from loader import CSVLoader
    loader = CSVLoader("data/taiwan_stock.db")
    df = loader.read_merged(stock_id)
    if df.empty:
        logger.error(f"{stock_id} 無資料，請先執行 Step 1"); return

    print(f"\n{'='*50}")
    print(f"  {stock_id} 資料驗證")
    print(f"{'='*50}")
    print(f"  筆數       : {len(df):,}")
    print(f"  日期範圍   : {df['date'].min().date()} ~ {df['date'].max().date()}")
    null_pct = (df.isnull().sum() / len(df) * 100).round(1)
    nz = null_pct[null_pct > 0]
    if len(nz):
        print(f"  缺漏值     : { {c: f'{p}%' for c, p in nz.items()} }")
    else:
        print(f"  缺漏值     : 無")

    # 簡易績效
    close = df.set_index("date")["close"].sort_index()
    daily_ret = close.pct_change().dropna()
    ann_ret = (1 + daily_ret.mean()) ** 252 - 1
    ann_vol = daily_ret.std() * (252**0.5)
    mdd     = (close / close.cummax() - 1).min()
    print(f"  年化報酬   : {ann_ret*100:.1f}%")
    print(f"  年化波動度 : {ann_vol*100:.1f}%")
    print(f"  Sharpe     : {(ann_ret-0.015)/ann_vol:.2f}")
    print(f"  最大回撤   : {mdd*100:.1f}%")
    print(f"\n  最後 3 筆：")
    print(df[["date","close","volume","PER","PBR"]].tail(3).to_string(index=False))


# ── 主程式 ────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股量化系統")
    parser.add_argument("--step",    type=int,  help="執行指定步驟 (1 或 2)")
    parser.add_argument("--dir",     default="raw_data", help="CSV 目錄 (預設 raw_data)")
    parser.add_argument("--overwrite", action="store_true", help="覆蓋已載入的資料")
    parser.add_argument("--stats",   action="store_true",   help="查看資料庫統計")
    parser.add_argument("--verify",  type=str,              help="驗證單一股票，如 --verify 1101")
    args = parser.parse_args()

    if args.stats:
        cmd_stats()
    elif args.verify:
        cmd_verify(args.verify)
    elif args.step == 1:
        step1_load(args.dir, args.overwrite)
    elif args.step == 2:
        step2_backtest()
    else:
        # 預設：全部執行
        logger.info("執行完整流程（Step 1 + Step 2）")
        step1_load(args.dir, args.overwrite)
        step2_backtest()