"""
tests/test_download_lock.py
─────────────────────────────────────────────────────────────
本檔案為單元測試，驗證程式邏輯正確性，非策略績效驗證，
使用構造資料屬正常做法。

驗證 data_pipeline/download_lock.py 的互斥鎖行為：存活 PID 擋下第二個
下載程序、死掉的 PID（stale lock）自動清除、正常釋放鎖。
"""

import json
import os
import sys
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "data_pipeline"))

from download_lock import acquire_lock, release_lock, _pid_alive


def test_pid_alive_true_for_self():
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_false_for_dead_pid():
    # 開一個立刻結束的子行程，等它結束後這個 PID 一定是死的
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert _pid_alive(proc.pid) is False


def test_acquire_creates_lock_file_with_pid_and_script(tmp_path):
    lock_path = tmp_path / ".download_lock"
    acquire_lock("test_script.py", lock_path=lock_path)
    try:
        assert lock_path.exists()
        info = json.loads(lock_path.read_text())
        assert info["pid"] == os.getpid()
        assert info["script"] == "test_script.py"
        assert "started_at" in info
    finally:
        release_lock(lock_path)


def test_acquire_blocked_by_alive_process(tmp_path):
    """已有存活的下載程序（這裡直接用自己的 PID 模擬）在跑時，第二次 acquire 應該直接結束 process。"""
    lock_path = tmp_path / ".download_lock"
    lock_path.write_text(json.dumps({
        "pid": os.getpid(),  # 用自己的 PID，保證「存活」
        "script": "other_script.py",
        "started_at": "2026-01-01T00:00:00",
    }))
    with pytest.raises(SystemExit) as exc_info:
        acquire_lock("test_script.py", lock_path=lock_path)
    assert exc_info.value.code == 1


def test_acquire_clears_stale_lock_from_dead_pid(tmp_path):
    """lock file 存在但裡面的 PID 已經死了（stale lock）時，應該自動清除並正常取得新鎖。"""
    lock_path = tmp_path / ".download_lock"
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # 確保這個 PID 現在已經死了

    lock_path.write_text(json.dumps({
        "pid": proc.pid,
        "script": "crashed_script.py",
        "started_at": "2026-01-01T00:00:00",
    }))

    acquire_lock("test_script.py", lock_path=lock_path)
    try:
        info = json.loads(lock_path.read_text())
        assert info["pid"] == os.getpid()
        assert info["script"] == "test_script.py"
    finally:
        release_lock(lock_path)


def test_release_only_removes_own_lock(tmp_path):
    """release_lock 不該誤刪不屬於自己 PID 的鎖（例如鎖在自己 acquire 之後被別人重新取得的極端情況）。"""
    lock_path = tmp_path / ".download_lock"
    other_pid = 999999999  # 幾乎不可能是真實 PID
    lock_path.write_text(json.dumps({
        "pid": other_pid,
        "script": "someone_else.py",
        "started_at": "2026-01-01T00:00:00",
    }))
    release_lock(lock_path)
    assert lock_path.exists()  # 沒被清掉，因為不是自己的鎖
