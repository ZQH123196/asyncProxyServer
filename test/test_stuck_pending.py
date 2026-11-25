"""回归：执行中又被提交时，那次提交不能被丢弃。

原缺陷：后到者被直接跳过，但提交接口已把库里那行重置成 pending，
先到者收尾写的是自己的终态 —— 后到者那次提交无人服务，库里永远 pending。
"""

import threading
import time


def test_resubmit_while_running_does_not_leave_task_pending(isolated_main, monkeypatch):
    main = isolated_main
    gate = threading.Event()
    rounds = []

    def work(task_id, raw_body):
        rounds.append(raw_body)
        if len(rounds) == 1:
            gate.wait(timeout=3)
        with main._db() as conn:
            conn.execute("UPDATE tasks SET status='success' WHERE task_id=?", (task_id,))

    monkeypatch.setattr(main, "_run_tts_task", work)
    with main._db() as conn:
        conn.execute("INSERT OR REPLACE INTO tasks VALUES ('dup', 'pending', NULL)")
        conn.commit()

    first = threading.Thread(target=main.hard_tts_work, args=("dup", b'{"v":1}'))
    first.start()
    time.sleep(0.1)
    # 模拟第二次提交：接口把行重置为 pending，并派发第二个执行体
    with main._db() as conn:
        conn.execute("INSERT OR REPLACE INTO tasks VALUES ('dup', 'pending', NULL)")
        conn.commit()
    main.hard_tts_work("dup", b'{"v":2}')
    gate.set()
    first.join(timeout=5)

    with main._db() as conn:
        status = conn.execute("SELECT status FROM tasks WHERE task_id='dup'").fetchone()[0]
    assert status == "success"
    assert len(rounds) == 2
