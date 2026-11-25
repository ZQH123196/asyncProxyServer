import sqlite3

import pytest

from restart_recovery import RESTARTED_RESULT, recover_interrupted_tasks


def _conn_with(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT, result TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    conn.commit()
    return conn


def _status(conn, task_id):
    return conn.execute("SELECT status, result FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def test_upstream_reachable_resumes_interrupted_tasks_as_pending():
    # 怕的是：本服务重启时把在跑任务判死，调用方整批重提形成启动风暴。
    conn = _conn_with([
        ("a", "processing", None),
        ("b", "pending", None),
        ("c", "success", "/download/c.opus"),
    ])

    outcome = recover_interrupted_tasks(conn, upstream_reachable=True)

    assert set(outcome.resumed_task_ids) == {"a", "b"}
    assert outcome.failed_count == 0
    assert _status(conn, "a") == ("pending", None)
    assert _status(conn, "b") == ("pending", None)


def test_upstream_reachable_leaves_terminal_tasks_alone():
    conn = _conn_with([
        ("done", "success", "/download/done.opus"),
        ("bad", "failed", "boom"),
    ])

    outcome = recover_interrupted_tasks(conn, upstream_reachable=True)

    assert outcome.resumed_task_ids == ()
    assert _status(conn, "done") == ("success", "/download/done.opus")
    assert _status(conn, "bad") == ("failed", "boom")


def test_upstream_unreachable_fails_interrupted_tasks():
    # 判据：整条链路都不在时，重新派发只会立刻再失败；直接给终态。
    conn = _conn_with([
        ("a", "processing", None),
        ("b", "pending", None),
    ])

    outcome = recover_interrupted_tasks(conn, upstream_reachable=False)

    assert outcome.resumed_task_ids == ()
    assert outcome.failed_count == 2
    assert _status(conn, "a") == ("failed", RESTARTED_RESULT)
    assert _status(conn, "b") == ("failed", RESTARTED_RESULT)


def test_nothing_to_recover_returns_empty_outcome():
    conn = _conn_with([("done", "success", "/x")])

    outcome = recover_interrupted_tasks(conn, upstream_reachable=True)

    assert outcome.resumed_task_ids == ()
    assert outcome.failed_count == 0
