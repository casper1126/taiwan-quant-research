"""
verify_inst_flow.py — 籌碼資料完整性驗證

檢查 7 件事：
  1. 資料覆蓋範圍（日期、股票數）
  2. 三大法人類型分布
  3. 缺漏日（與 daily_price 的交集）
  4. buy / sell / net 的一致性
  5. 異常值（極端 net 數字）
  6. 個股級覆蓋（每檔股票有幾天）
  7. 60 日累積 net 的分布是否合理（看一檔知名股）
"""
import sqlite3
import pandas as pd
import numpy as np

DB = "data/taiwan_stock.db"
conn = sqlite3.connect(DB)

print("═" * 60)
print("  📊 籌碼資料正確性驗證")
print("═" * 60)

# ── 1. 覆蓋範圍 ──────────────────────────────────────────────
q1 = pd.read_sql("""
    SELECT MIN(date) AS first_d, MAX(date) AS last_d,
           COUNT(*) AS rows, COUNT(DISTINCT stock_id) AS n_stk,
           COUNT(DISTINCT date) AS n_days
    FROM institutional_investors""", conn)
print(f"\n[1] 覆蓋範圍")
print(f"    日期：{q1['first_d'][0]} ~ {q1['last_d'][0]}")
print(f"    總列數：{q1['rows'][0]:,}")
print(f"    股票數：{q1['n_stk'][0]:,}")
print(f"    交易日：{q1['n_days'][0]:,}")

# ── 2. 法人類型 ──────────────────────────────────────────────
q2 = pd.read_sql("""
    SELECT investor_type, COUNT(*) AS n,
           SUM(CASE WHEN net <> 0 THEN 1 ELSE 0 END) AS nonzero,
           ROUND(AVG(buy), 0) AS avg_buy,
           ROUND(AVG(sell), 0) AS avg_sell
    FROM institutional_investors
    GROUP BY investor_type ORDER BY n DESC""", conn)
print(f"\n[2] 法人類型分布")
print(q2.to_string(index=False))

# ── 3. 缺漏日 vs daily_price ────────────────────────────────
q3 = pd.read_sql("""
    SELECT
       (SELECT COUNT(DISTINCT date) FROM daily_price WHERE date >= '2015-01-05') AS price_days,
       (SELECT COUNT(DISTINCT date) FROM institutional_investors) AS inst_days
""", conn)
print(f"\n[3] 缺漏日比較")
print(f"    daily_price 從 2015-01-05 起：{q3['price_days'][0]:,} 天")
print(f"    institutional_investors 全部：{q3['inst_days'][0]:,} 天")
print(f"    差：{q3['price_days'][0] - q3['inst_days'][0]} 天")

# ── 4. buy/sell/net 一致性檢查 ───────────────────────────────
q4 = pd.read_sql("""
    SELECT COUNT(*) AS bad_rows
    FROM institutional_investors
    WHERE ABS((buy - sell) - net) > 1""", conn)
print(f"\n[4] (buy - sell) ≠ net 的列：{q4['bad_rows'][0]:,}")
total_q4 = q1['rows'][0]
bad_pct = q4['bad_rows'][0] / total_q4 * 100
print(f"    占比：{bad_pct:.4f}% {'✅' if bad_pct < 0.1 else '⚠️'}")

# ── 5. 極端值檢查 ────────────────────────────────────────────
q5 = pd.read_sql("""
    SELECT MIN(net) AS min_net, MAX(net) AS max_net,
           AVG(net) AS avg_net
    FROM institutional_investors WHERE net <> 0""", conn)
print(f"\n[5] net 分布")
print(f"    最小：{q5['min_net'][0]:,.0f}")
print(f"    最大：{q5['max_net'][0]:,.0f}")
print(f"    平均：{q5['avg_net'][0]:,.0f}")

# ── 6. 個股級覆蓋（前 10 大 vs 後 10 名）─────────────────────
q6 = pd.read_sql("""
    SELECT stock_id, COUNT(DISTINCT date) AS days
    FROM institutional_investors
    GROUP BY stock_id ORDER BY days DESC LIMIT 5""", conn)
q6b = pd.read_sql("""
    SELECT stock_id, COUNT(DISTINCT date) AS days
    FROM institutional_investors
    GROUP BY stock_id ORDER BY days ASC LIMIT 5""", conn)
print(f"\n[6] 個股覆蓋（取樣）")
print("    覆蓋最多的 5 檔：")
print(q6.to_string(index=False))
print("    覆蓋最少的 5 檔：")
print(q6b.to_string(index=False))

# ── 7. 用台積電 (2330) 看 60 日累積 net 的分布 ──────────────
print(f"\n[7] 範例：台積電 2330 的 60 日累積外資+投信淨買超")
q7 = pd.read_sql("""
    SELECT date, investor_type, net
    FROM institutional_investors
    WHERE stock_id = '2330'
      AND investor_type IN
          ('Foreign_Investor','Foreign_Dealer_Self','Investment_Trust')
      AND date >= '2024-01-01'
    ORDER BY date""", conn, parse_dates=['date'])
if not q7.empty:
    pivot = q7.groupby('date')['net'].sum()
    cum60 = pivot.rolling(60, min_periods=10).sum()
    print(f"    日期範圍：{pivot.index.min().date()} ~ {pivot.index.max().date()}")
    print(f"    日均 net：{pivot.mean():,.0f}")
    print(f"    60 日累積 — min: {cum60.min():,.0f}  max: {cum60.max():,.0f}")
    print(f"    目前最新 60 日累積（{pivot.index[-1].date()}）：{cum60.iloc[-1]:,.0f}")
else:
    print("    （查無資料）")

# ── 8. 因子建構驗證 — 重做一次 build_factors 看 inst_flow 矩陣 ─────────
print(f"\n[8] inst_flow 因子矩陣健康度（從 2015 起）")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from strategy.quant_layer3 import load_matrices, build_factors, WARMUP_START, END_DATE, DB_PATH

data = load_matrices(DB_PATH, WARMUP_START, END_DATE)
factors = build_factors(data)
inst = factors['inst_flow']
mask = inst.index >= '2015-01-01'
inst_2015 = inst.loc[mask]
total_cells = inst_2015.size
nan_cells = inst_2015.isna().sum().sum()
print(f"    矩陣尺寸：{inst_2015.shape}")
print(f"    NaN 比例：{nan_cells / total_cells * 100:.1f}%")
print(f"    每天平均有效股票數：{(~inst_2015.isna()).sum(axis=1).mean():.0f} 檔（理想 300）")
print(f"    截面標準差中位數：{inst_2015.std(axis=1).median():,.0f}")

conn.close()
print("\n" + "═" * 60)
