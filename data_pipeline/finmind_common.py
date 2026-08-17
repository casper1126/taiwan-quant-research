"""
finmind_common.py
─────────────────────────────────────────────────────────────
download_institutional.py 與 download_supplementary.py 共用的配額處理元件。

抽成共用模組，是因為這兩支下載器原本各自獨立實作了「402 配額用盡」的
處理邏輯，兩份實作互相不同步、都有同一種 bug（見 QuotaExhaustedError
的說明）。共用元件之後只需要修一個地方。

兩支下載器的「速率限制器」本身刻意保留不同設計（download_institutional.py
用動態 quota-aware pacing，download_supplementary.py 用任務書規格的固定
節奏），不勉強統一——這裡只共用「偵測到配額用盡之後要做什麼」的部分。
"""

import time
from datetime import datetime
from loguru import logger

# 連續幾個「休眠醒來後立刻又配額用盡」的週期，才視為異常放棄整批。
# 不是「整個回填過程中撞到配額上限的次數」——正常的多小時回填本來就會
# 規律地撞到每小時的配額上限，那是預期行為，不該被這個計數器影響。
MAX_QUOTA_RETRY_CYCLES = 3


class QuotaExhaustedError(Exception):
    """
    FinMind 回傳真正的 HTTP 402（配額用盡）。

    這不是暫時性錯誤，重試沒有意義——只會用僅剩的配額繼續打空氣，
    延後真正恢復的時間。呼叫端應該休眠到下個時間窗口再重試，
    不應該跟「200 但真的沒資料」的合法空結果混在一起處理。
    """
    def __init__(self, data_id: str):
        self.data_id = data_id
        super().__init__(f"{data_id}: FinMind 402 配額用盡")


def sleep_with_heartbeat(wake_at: datetime, processed: int, total: int,
                          heartbeat_interval: float = 600.0) -> None:
    """
    睡到 wake_at，期間每 heartbeat_interval 秒印一次心跳 log。

    用途：背景長時間執行（可能好幾個小時）時，不需要人一直盯著終端機，
    但透過心跳可以確認 process 還活著、還在等，不是卡死了。
    """
    while True:
        remaining = (wake_at - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, heartbeat_interval))
        remaining_after = (wake_at - datetime.now()).total_seconds()
        if remaining_after > 0:
            logger.info(
                f"💓 心跳：休眠中，已處理 {processed}/{total} 檔，"
                f"距離自動恢復還有 {remaining_after/60:.1f} 分鐘"
            )
