"""重启恢复的装配层：init_db 的分支裁决 + 重派发不崩。

纯函数 recover_interrupted_tasks 已被 test_restart_recovery.py 覆盖；
这里测的是把它接进 main 之后的那段（读写 .req、进重派队列、线程派发）。
"""

import os
import sqlite3
import threading

import pytest


def _seed(main, rows):
    """在 main 已建好的库里放遗留任务。"""
    with main._db() as conn:
        conn.executemany("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?)", rows)
        conn.commit()


def _write_req(main, task_id, payload=b'{"input":"hi"}'):
    os.makedirs(main.AUDIO_DIR, exist_ok=True)
    main._persist_request_body(task_id, payload)


def _status(main, task_id):
    with main._db() as conn:
        return conn.execute("SELECT status, result FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def test_missing_request_body_fails_instead_of_staying_pending(isolated_main, monkeypatch):
    # 怕的是：没有请求体的遗留任务永远停在 pending，调用方轮询不到终态。
    main = isolated_main
    monkeypatch.setattr(main, "_upstream_reachable", lambda timeout=2.0: True)
    _seed(main, [("nobody", "processing", None)])

    main.init_db()

    status, result = _status(main, "nobody")
    assert status == "failed"
    assert "request body missing" in result
    assert "nobody" not in main._REDISPATCH_QUEUE


def test_task_with_request_body_is_queued_for_redispatch(isolated_main, monkeypatch):
    main = isolated_main
    monkeypatch.setattr(main, "_upstream_reachable", lambda timeout=2.0: True)
    _seed(main, [("hasbody", "processing", None)])
    _write_req(main, "hasbody")

    main.init_db()

    assert _status(main, "hasbody") == ("pending", None)
    assert "hasbody" in main._REDISPATCH_QUEUE


def test_unreachable_upstream_marks_everything_failed(isolated_main, monkeypatch):
    main = isolated_main
    monkeypatch.setattr(main, "_upstream_reachable", lambda timeout=2.0: False)
    _seed(main, [("a", "processing", None), ("b", "pending", None)])
    _write_req(main, "a")

    main.init_db()

    assert _status(main, "a")[0] == "failed"
    assert _status(main, "b")[0] == "failed"
    assert main._REDISPATCH_QUEUE == []


def test_dispatch_runs_every_queued_task(isolated_main, monkeypatch):
    # 回归：派发函数曾因局部变量遮蔽模块名，一旦有任务待恢复就抛 UnboundLocalError，
    # 导致整个应用启动失败。这里锁死「不抛且任务全被消费」。
    main = isolated_main
    done = []
    finished = threading.Event()

    def fake_work(task_id, raw_body):
        done.append((task_id, raw_body))
        if len(done) == 3:
            finished.set()

    monkeypatch.setattr(main, "hard_tts_work", fake_work)
    for index in range(3):
        _write_req(main, f"t{index}", f'{{"input":"{index}"}}'.encode())
        main._REDISPATCH_QUEUE.append(f"t{index}")

    main._dispatch_resumed_tasks()

    assert finished.wait(timeout=5), f"只跑到 {done}"
    assert sorted(task_id for task_id, _ in done) == ["t0", "t1", "t2"]
    assert main._REDISPATCH_QUEUE == []


def test_dispatch_with_empty_queue_is_a_noop(isolated_main):
    isolated_main._dispatch_resumed_tasks()

    assert isolated_main._REDISPATCH_QUEUE == []
