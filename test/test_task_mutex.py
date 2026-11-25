"""同一 task_id 的执行互斥与重跑。

怕的是：提交接口（BackgroundTasks）与重启重派是两条独立进入路径，
同时跑同一个 task_id 会并发写 `<id>.wav/.opus`，产物互相覆盖。
而单纯「丢弃后到者」又会留下「库里永远 pending」——提交接口已经重置过那行了。
"""

import os
import threading
import time


def _status(main, task_id):
    with main._db() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return row[0] if row else None


def test_same_task_never_runs_twice_concurrently(isolated_main, monkeypatch):
    main = isolated_main
    inside = []
    overlap = []
    lock = threading.Lock()
    release = threading.Event()

    def slow(task_id, raw_body):
        with lock:
            inside.append(task_id)
            if len(inside) > 1:
                overlap.append(task_id)
        release.wait(timeout=3)
        with lock:
            inside.remove(task_id)

    monkeypatch.setattr(main, "_run_tts_task", slow)

    first = threading.Thread(target=main.hard_tts_work, args=("same", b'{"v":1}'))
    first.start()
    time.sleep(0.1)
    main.hard_tts_work("same", b'{"v":2}')   # 应记为待重跑并返回
    release.set()
    first.join(timeout=5)

    assert overlap == []


def test_later_submission_is_rerun_with_its_own_body(isolated_main, monkeypatch):
    # 怕的是：后到者被丢掉，它的那次提交无人服务，库里永远 pending。
    main = isolated_main
    seen = []
    gate = threading.Event()

    def slow(task_id, raw_body):
        seen.append(raw_body)
        if len(seen) == 1:
            gate.wait(timeout=3)

    monkeypatch.setattr(main, "_run_tts_task", slow)

    first = threading.Thread(target=main.hard_tts_work, args=("same", b'{"v":1}'))
    first.start()
    time.sleep(0.1)
    main.hard_tts_work("same", b'{"v":2}')
    gate.set()
    first.join(timeout=5)

    assert seen == [b'{"v":1}', b'{"v":2}']


def test_rerun_uses_latest_body_when_submitted_many_times(isolated_main, monkeypatch):
    main = isolated_main
    seen = []
    gate = threading.Event()

    def slow(task_id, raw_body):
        seen.append(raw_body)
        if len(seen) == 1:
            gate.wait(timeout=3)

    monkeypatch.setattr(main, "_run_tts_task", slow)

    first = threading.Thread(target=main.hard_tts_work, args=("same", b'{"v":1}'))
    first.start()
    time.sleep(0.1)
    for version in (2, 3, 4):
        main.hard_tts_work("same", f'{{"v":{version}}}'.encode())
    gate.set()
    first.join(timeout=5)

    assert seen == [b'{"v":1}', b'{"v":4}']


def test_task_slot_is_released_after_failure(isolated_main, monkeypatch):
    # 怕的是：执行体抛异常后占位不释放，该 task_id 之后永远跑不了。
    main = isolated_main

    def boom(task_id, raw_body):
        raise RuntimeError("生成失败")

    monkeypatch.setattr(main, "_run_tts_task", boom)

    try:
        main.hard_tts_work("retry-me", b"{}")
    except RuntimeError:
        pass

    ran = []
    monkeypatch.setattr(main, "_run_tts_task", lambda task_id, body: ran.append(task_id))
    main.hard_tts_work("retry-me", b"{}")

    assert ran == ["retry-me"]


def test_different_task_ids_run_concurrently(isolated_main, monkeypatch):
    main = isolated_main
    ran = []
    lock = threading.Lock()

    def work(task_id, raw_body):
        with lock:
            ran.append(task_id)

    monkeypatch.setattr(main, "_run_tts_task", work)

    threads = [
        threading.Thread(target=main.hard_tts_work, args=(f"t{index}", b"{}"))
        for index in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(ran) == ["t0", "t1", "t2"]


def test_failure_with_queued_rerun_releases_the_slot(isolated_main, monkeypatch):
    # 回归（假绿教训）：上一版「有重跑排队时抛异常」只会 pop 重跑、不释放占位，
    # 该 task_id 之后永远被判为 ALREADY_RUNNING，再也没有人执行。
    main = isolated_main
    gate = threading.Event()
    attempts = []

    def boom_after_queue(task_id, raw_body):
        attempts.append(raw_body)
        if len(attempts) == 1:
            gate.wait(timeout=3)   # 让后到者有时间排队
            raise RuntimeError("生成失败")
        return

    monkeypatch.setattr(main, "_run_tts_task", boom_after_queue)

    raised = []

    def run_first():
        try:
            main.hard_tts_work("hazard", b'{"v":1}')
        except RuntimeError as error:
            raised.append(error)

    first = threading.Thread(target=run_first)
    first.start()
    time.sleep(0.1)
    main.hard_tts_work("hazard", b'{"v":2}')   # 排队待重跑
    gate.set()
    first.join(timeout=5)

    assert raised, "第一轮应当抛出"
    assert "hazard" not in main._running_task_ids
    assert "hazard" not in main._pending_rerun

    # 关键断言：之后再提交必须真的执行，而不是被当成「已在跑」
    main.hard_tts_work("hazard", b'{"v":3}')
    assert attempts[-1] == b'{"v":3}'


def test_rerun_rounds_are_capped(isolated_main, monkeypatch):
    # 怕的是：调用方疯狂重复提交同一 task_id，执行体被无限重跑，长期占住线程与编码信号量。
    main = isolated_main
    rounds = []

    def always_resubmit(task_id, raw_body):
        rounds.append(raw_body)
        # 每一轮执行期间都再提交一次：模拟调用方持续重试
        main.hard_tts_work(task_id, f'{{"v":{len(rounds)}}}'.encode())

    monkeypatch.setattr(main, "_run_tts_task", always_resubmit)

    main.hard_tts_work("spam", b'{"v":0}')

    assert len(rounds) == main.MAX_RERUN_ROUNDS
    assert "spam" not in main._running_task_ids
    assert "spam" not in main._pending_rerun
