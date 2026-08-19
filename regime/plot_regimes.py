"""
regime/plot_regimes.py
─────────────────────────────────────────────────────────────
Task 3f：把機制偵測結果畫成圖，存到 reports/regime_history.png。

上圖：TAIEX 走勢，背景依機制著色（綠 BULL / 黃 NEUTRAL / 橘 WARNING / 紅 BEAR）
下圖：健康分數（0-100）時序
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

REGIME_COLORS = {
    "BULL": "#2ecc71",
    "NEUTRAL": "#f1c40f",
    "WARNING": "#e67e22",
    "BEAR": "#e74c3c",
}


def plot_regime_history(index_close: pd.Series, result_df: pd.DataFrame,
                        output_path: str = "reports/regime_history.png") -> str:
    """
    index_close: pd.Series(index=date)，TAIEX 收盤價
    result_df  : regime_engine.run_regime_engine() 回傳的 result_df
                 （需要 'regime'、'health' 兩欄）
    """
    idx = index_close.dropna().index
    regime = result_df["regime"].reindex(idx)
    health = result_df["health"].reindex(idx)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 9), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    # ── 背景著色：找出每個機制連續區段，用 axvspan 畫 ──────────
    regime_filled = regime.ffill()
    change_points = regime_filled.ne(regime_filled.shift()).cumsum()
    for _, seg in regime_filled.groupby(change_points):
        state = seg.iloc[0]
        if state is None or (isinstance(state, float)):
            continue
        color = REGIME_COLORS.get(state, "#bdc3c7")
        ax1.axvspan(seg.index[0], seg.index[-1], color=color, alpha=0.18, linewidth=0)

    ax1.plot(idx, index_close.reindex(idx), color="#2c3e50", linewidth=1.0)
    ax1.set_ylabel("TAIEX Close")
    ax1.set_title("Taiwan Market Regime History (Task 3)")

    # 圖表文字統一用英文，避免依賴系統是否安裝中文字型（尤其 CI 環境
    # 通常只有 DejaVu Sans，畫中文會變成方框）。
    handles = [plt.Rectangle((0, 0), 1, 1, color=c, alpha=0.4) for c in REGIME_COLORS.values()]
    ax1.legend(handles, REGIME_COLORS.keys(), loc="upper left", ncol=4)

    ax2.plot(idx, health, color="#8e44ad", linewidth=0.9)
    ax2.axhline(60, color="#2ecc71", linestyle="--", linewidth=0.6)
    ax2.axhline(45, color="#f1c40f", linestyle="--", linewidth=0.6)
    ax2.axhline(30, color="#e67e22", linestyle="--", linewidth=0.6)
    ax2.set_ylabel("Health Score")
    ax2.set_ylim(0, 100)
    ax2.set_xlabel("Date")

    fig.tight_layout()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=120)
    plt.close(fig)
    return output_path


if __name__ == "__main__":
    from regime.regime_engine import run_regime_engine
    from regime.data_loader import load_regime_inputs

    df, extras = run_regime_engine()
    data = load_regime_inputs()
    plot_regime_history(data["index_close"], df)
    print("已存到 reports/regime_history.png")
