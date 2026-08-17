"""
download_lock.py
─────────────────────────────────────────────────────────────
下載腳本互斥鎖：防止多個下載程序同時搶同一個 FINMIND_TOKEN 的配額。

背景：download_institutional.py、download_supplementary.py（以及透過
run.py --step 1/1b 呼叫它們）都各自在自己的 process 記憶體裡算配額，
互相看不到對方，兩個 process 同時跑會一起把共用配額用光，
表現成大量 402。lock file 是唯一的事實來源，確保同一時間只有一個
下載程序在跑。

Lock file 內容（JSON）：{"pid": ..., "script": ..., "started_at": ...}
"""

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from loguru import logger

DEFAULT_LOCK_PATH = Path("data/.download_lock")


def _pid_alive(pid: int) -> bool:
    """
    檢查 PID 是否還活著。

    os.kill(pid, 0) 不會真的送訊號，只檢查 process 是否存在：
      - ProcessLookupError → 該 PID 不存在，process 已死
      - PermissionError    → process 存在，只是不屬於目前使用者
      - 無例外              → process 存在
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def acquire_lock(script_name: str, lock_path: Path = DEFAULT_LOCK_PATH) -> None:
    """
    取得下載鎖。已有其他存活的下載程序在跑時，印出清楚訊息後結束整個 process
    （sys.exit(1)），不允許重疊執行。lock file 內的 PID 已死（stale lock，
    通常是上次 kill/crash 沒清乾淨）則自動清除後正常取得鎖。
    """
    if lock_path.exists():
        try:
            info = json.loads(lock_path.read_text())
            pid = info.get("pid")
        except (json.JSONDecodeError, OSError):
            pid = None

        if pid and _pid_alive(pid):
            logger.error(
                f"❌ 已有下載程序在跑（PID {info.get('pid')}，"
                f"啟動於 {info.get('started_at')}，執行 {info.get('script')}），"
                f"請先確認該程序狀態再重試。"
            )
            sys.exit(1)
        else:
            logger.warning(f"⚠️  發現 stale lock（PID {pid} 已不存在），自動清除")

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({
        "pid": os.getpid(),
        "script": script_name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }))
    logger.debug(f"🔒 已取得下載鎖（PID {os.getpid()}，{script_name}）")


def release_lock(lock_path: Path = DEFAULT_LOCK_PATH) -> None:
    """釋放下載鎖。只釋放屬於目前 process 的鎖，避免誤刪別人剛取得的鎖。"""
    try:
        if lock_path.exists():
            info = json.loads(lock_path.read_text())
            if info.get("pid") == os.getpid():
                lock_path.unlink()
                logger.debug(f"🔓 已釋放下載鎖（PID {os.getpid()}）")
    except (json.JSONDecodeError, OSError):
        pass


@contextmanager
def download_lock(script_name: str, lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    """
    用法：
        with download_lock("download_supplementary.py"):
            ... 真正的下載邏輯 ...

    離開 with 區塊時（包含例外/Ctrl+C）一定會釋放鎖。
    """
    acquire_lock(script_name, lock_path)
    try:
        yield
    finally:
        release_lock(lock_path)
