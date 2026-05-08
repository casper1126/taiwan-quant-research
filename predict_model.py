"""
predict_model.py
─────────────────────────────────────────────────────────────
Discord bot 的 /run_model 指令會呼叫這個腳本。

功能：
  1. 跑 N1 v2 ML pipeline（最佳策略）
  2. 取最新 rebalance 日的目標持倉
  3. 對比 portfolio.json，產出買/賣/維持 action 清單
  4. 寫入 signals/YYYY-MM-DD.json

帳戶資金：500 萬（NTD），可在 config.json 修改
依靠：data/stock_names.json（請先跑 fetch_stock_names.py 一次）

執行：
  python predict_model.py
"""
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from strategy import quant_layer3 as L3
from strategy.quant_layer3 import _industry_of

# ── 路徑 ─────────────────────────────────────────────────────
PORTFOLIO_PATH    = BASE_DIR / "portfolio.json"
CONFIG_PATH       = BASE_DIR / "config.json"
SIGNALS_DIR       = BASE_DIR / "signals"
STOCK_NAMES_PATH  = BASE_DIR / "data" / "stock_names.json"

# ── 預設帳戶資金（可在 config.json 修改）──────────────────
DEFAULT_ACCOUNT_VALUE = 5_000_000   # NTD


# ══════════════════════════════════════════════════════════════
# 1. 載入輔助
# ══════════════════════════════════════════════════════════════

def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════════
# 2. 跑策略，取最新持倉
# ══════════════════════════════════════════════════════════════

def get_latest_positions() -> tuple:
    """
    執行 N1 v2 ML pipeline 並取最新一天的持倉。

    Returns:
      (positions_today: dict[stock_id, weight], rebal_date: pd.Timestamp,
       exposure: float, all_close_today: pd.Series)
    """
    print("📊 跑 N1 v2 ML pipeline（與最終策略一致）...")
    data = L3.load_matrices(L3.DB_PATH, L3.WARMUP_START, L3.END_DATE)
    factors = L3.build_factors(data)

    # 線性 + ML ensemble (與 quant_layer3.py --ml 同邏輯)
    linear = L3.build_composite(factors)
    ml_pred = L3.build_ml_composite(factors)
    linear_z = L3._cross_zscore(linear).fillna(0.0)
    ml_z = L3._cross_zscore(ml_pred).fillna(0.0)
    composite = 0.5 * linear_z + 0.5 * ml_z
    any_valid = (~linear.isna()) | (~ml_pred.isna())
    composite = composite.where(any_valid, np.nan)

    positions = L3.build_positions(factors, composite)

    # 取最新有非零持倉的那一天
    last_idx = (positions.sum(axis=1) > 0).idxmax()  # 第一個 True
    # 實際取「最後一個有持倉」的日期
    nonzero_dates = positions.index[positions.sum(axis=1) > 0]
    if len(nonzero_dates) == 0:
        raise RuntimeError("沒有任何持倉日期")
    rebal_date = nonzero_dates[-1]

    today_pos = positions.loc[rebal_date]
    holdings = today_pos[today_pos > 0]
    exposure = float(holdings.sum())

    close_today = data["close"].loc[rebal_date].dropna()
    return holdings.to_dict(), rebal_date, exposure, close_today


# ══════════════════════════════════════════════════════════════
# 3. 對比 portfolio.json，產出 actions
# ══════════════════════════════════════════════════════════════

def diff_with_portfolio(target: Dict[str, float],
                         portfolio: List[Dict],
                         account_value: float,
                         close_today: pd.Series) -> List[Dict]:
    """
    對比目標權重與當前持倉，產出 BUY / SELL / HOLD 動作。
    """
    current = {p["symbol"]: p for p in portfolio}
    actions = []

    # ── BUY / HOLD：在目標清單裡的 ────────────────────────
    for sid, target_w in target.items():
        target_dollars = account_value * target_w
        price = float(close_today.get(sid, 0))
        if price <= 0:
            continue
        # 台股 1 張 = 1000 股；先給張數（floor 取整）
        target_shares_raw = target_dollars / price
        target_lots = int(target_shares_raw // 1000)
        if target_lots == 0:
            target_lots = 1   # 至少 1 張（張數太低代表權重 vs 帳戶資金不匹配）
        target_shares = target_lots * 1000
        target_dollars_actual = target_shares * price

        cur = current.get(sid)
        if cur is None:
            actions.append({
                "symbol": sid,
                "action": "BUY",
                "target_weight": round(target_w, 4),
                "target_lots": target_lots,
                "target_shares": target_shares,
                "target_dollars": round(target_dollars_actual, 0),
                "price_ref": round(price, 2),
                "current_quantity": 0,
            })
        else:
            cur_qty = float(cur.get("quantity", 0))
            cur_lots = int(cur_qty // 1000)
            delta_lots = target_lots - cur_lots
            if delta_lots > 0:
                actions.append({
                    "symbol": sid, "action": "BUY",
                    "target_weight": round(target_w, 4),
                    "target_lots": target_lots,
                    "delta_lots": delta_lots,
                    "target_shares": target_shares,
                    "current_shares": int(cur_qty),
                    "price_ref": round(price, 2),
                })
            elif delta_lots < 0:
                actions.append({
                    "symbol": sid, "action": "TRIM",
                    "target_weight": round(target_w, 4),
                    "target_lots": target_lots,
                    "delta_lots": delta_lots,
                    "current_shares": int(cur_qty),
                    "price_ref": round(price, 2),
                })
            else:
                actions.append({
                    "symbol": sid, "action": "HOLD",
                    "target_weight": round(target_w, 4),
                    "current_shares": int(cur_qty),
                    "price_ref": round(price, 2),
                })

    # ── SELL：在 portfolio 裡但不在 target ─────────────────
    for sid, cur in current.items():
        if sid not in target:
            actions.append({
                "symbol": sid, "action": "SELL",
                "current_shares": int(float(cur.get("quantity", 0))),
                "current_cost": float(cur.get("cost", 0)),
                "price_ref": round(float(close_today.get(sid, 0)), 2),
            })

    # 排序：BUY > TRIM > HOLD > SELL
    order = {"BUY": 0, "TRIM": 1, "HOLD": 2, "SELL": 3}
    actions.sort(key=lambda x: (order.get(x["action"], 99),
                                 -x.get("target_weight", 0)))
    return actions


# ══════════════════════════════════════════════════════════════
# 4. 產業分布
# ══════════════════════════════════════════════════════════════

def industry_breakdown(target: Dict[str, float]) -> Dict[str, float]:
    out = {}
    for sid, w in target.items():
        ind = _industry_of(sid)
        out[ind] = out.get(ind, 0.0) + w
    return {k: round(v, 4) for k, v in
             sorted(out.items(), key=lambda x: -x[1])}


# ══════════════════════════════════════════════════════════════
# 5. 主流程
# ══════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  📊 N1 v2 ML 預測模型 — 產生交易訊號")
    print("=" * 70)

    cfg = load_json(CONFIG_PATH, {})
    account_value = float(cfg.get("account_value", DEFAULT_ACCOUNT_VALUE))
    print(f"\n💰 帳戶資金：{account_value:,.0f} NTD")

    # 1. 跑策略
    target, rebal_date, exposure, close_today = get_latest_positions()
    print(f"\n📅 最新 rebalance：{rebal_date.date()}")
    print(f"   曝險：{exposure*100:.1f}%")
    print(f"   持有檔數：{len(target)}")

    # 2. 抓股票名稱
    names = load_json(STOCK_NAMES_PATH, {})
    if not names:
        print("⚠️  data/stock_names.json 不存在，先跑：")
        print("    python data_pipeline/fetch_stock_names.py")

    # 3. 對比 portfolio.json
    portfolio = load_json(PORTFOLIO_PATH, [])
    actions = diff_with_portfolio(target, portfolio, account_value, close_today)

    # 4. 產業分布
    by_industry = industry_breakdown(target)

    # 5. 組成輸出 JSON
    today_str = datetime.now().strftime("%Y-%m-%d")
    signals = {
        "generated_at":      datetime.now().isoformat(),
        "rebalance_date":    rebal_date.strftime("%Y-%m-%d"),
        "strategy":          "N1 v2 ML (mom_52w 60% + inst_flow 40%)",
        "account_value":     account_value,
        "exposure":          round(exposure, 4),
        "n_holdings":        len(target),
        "industry":          by_industry,
        "positions": [
            {
                "rank": i + 1,
                "symbol": sid,
                "name": names.get(sid, ""),
                "weight": round(w, 4),
                "industry": _industry_of(sid),
                "target_dollars": round(account_value * w, 0),
                "price_ref": round(float(close_today.get(sid, 0)), 2),
            }
            for i, (sid, w) in enumerate(
                sorted(target.items(), key=lambda x: -x[1])
            )
        ],
        "actions": [
            {**a, "name": names.get(a["symbol"], "")}
            for a in actions
        ],
    }

    out_path = SIGNALS_DIR / f"{today_str}.json"
    save_json(out_path, signals)
    save_json(SIGNALS_DIR / "latest.json", signals)

    # ── 印出摘要 ──────────────────────────────────────────
    print(f"\n📝 訊號已寫入：{out_path}")
    print(f"               {SIGNALS_DIR / 'latest.json'}")

    print("\n" + "─" * 70)
    print("  📋 持股清單（按權重排序）")
    print("─" * 70)
    print(f"  {'#':<3} {'代號':<6} {'名稱':<14} {'權重':<8} {'金額':<12} {'產業':<12}")
    for p in signals["positions"]:
        print(f"  {p['rank']:<3} {p['symbol']:<6} {p['name']:<14} "
              f"{p['weight']*100:5.2f}%  "
              f"{p['target_dollars']:>10,.0f}  {p['industry']:<12}")

    print("\n" + "─" * 70)
    print("  🛠️  交易動作（vs 你目前的 portfolio.json）")
    print("─" * 70)
    if not actions:
        print("  （portfolio.json 為空，所有目標都是 BUY）")
    counts = {"BUY": 0, "TRIM": 0, "HOLD": 0, "SELL": 0}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    print(f"  BUY: {counts.get('BUY', 0)}, TRIM: {counts.get('TRIM', 0)}, "
          f"HOLD: {counts.get('HOLD', 0)}, SELL: {counts.get('SELL', 0)}")

    print("\n" + "─" * 70)
    print("  🏭 產業分布")
    print("─" * 70)
    for ind, w in by_industry.items():
        bar = "█" * int(w * 100)
        print(f"  {ind:<12} {w*100:5.2f}%  {bar}")

    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
