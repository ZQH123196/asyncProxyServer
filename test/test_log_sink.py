import io
import sys
import threading
import time

import pytest

from log_sink import AsyncLineSink


class SlowStream(io.RawIOBase):
    """写得很慢的流：模拟下游（pm2 / 管道）消费跟不上。"""

    def __init__(self, delay_seconds: float):
        self.delay = delay_seconds
        self.received = bytearray()
        self._lock = threading.Lock()

    def writable(self):
        return True

    def write(self, data):
        time.sleep(self.delay)
        with self._lock:
            self.received.extend(data)
        return len(data)

    def flush(self):
        pass


class CollectingStream:
    """与 sys.stdout 同形的文本流：sink 写入的是 str，不是 bytes。"""

    def __init__(self):
        self.received = ""

    def write(self, text):
        self.received += text

    def flush(self):
        pass


class SlowTextStream(CollectingStream):
    def __init__(self, delay_seconds: float):
        super().__init__()
        self.delay = delay_seconds

    def write(self, text):
        time.sleep(self.delay)
        super().write(text)


def test_write_line_does_not_block_when_downstream_is_slow():
    # 怕的是：下游慢时业务线程被 write() 卡住，整条链路跟着冻。
    stream = SlowTextStream(delay_seconds=0.5)
    sink = AsyncLineSink(stream=stream)
    try:
        started = time.perf_counter()
        for i in range(200):
            sink.write_line(f"line-{i}")
        elapsed = time.perf_counter() - started
    finally:
        sink.close(timeout=10)

    # 200 次入队必须远快于「每次写都等下游」：下游单次就要 0.5s。
    assert elapsed < 0.5


def test_lines_are_written_in_order_after_close():
    stream = CollectingStream()
    sink = AsyncLineSink(stream=stream)
    sink.write_line("first")
    sink.write_line("second")
    sink.close()

    assert stream.received == "first\nsecond\n"


def test_sink_writes_text_not_bytes():
    # 真实 sys.stdout 是文本流，写 bytes 会抛 TypeError 并静默丢掉整条日志线程。
    # 这里直接用真 stdout 的替身（StringIO 同为 TextIOBase）验证类型。
    stream = io.StringIO()
    sink = AsyncLineSink(stream=stream)
    sink.write_line("中文日志 ok")
    sink.close()

    assert stream.getvalue() == "中文日志 ok\n"


def test_queue_full_drops_and_reports_instead_of_blocking():
    # 怕的是：队列满后阻塞业务线程。判据是「丢并计数」，不是「卡住」。
    stream = SlowTextStream(delay_seconds=5.0)
    sink = AsyncLineSink(stream=stream, max_lines=4)
    try:
        started = time.perf_counter()
        for i in range(500):
            sink.write_line(f"line-{i}")
        elapsed = time.perf_counter() - started
    finally:
        sink.close(timeout=1)

    assert elapsed < 1.0


def test_write_after_close_is_ignored():
    stream = CollectingStream()
    sink = AsyncLineSink(stream=stream)
    sink.close()
    sink.write_line("late")

    assert stream.received == ""


def test_lines_are_batched_into_fewer_writes():
    # 攒批写：多行应合并在少数几次 write 里，而不是一行一次系统调用。
    class CountingStream(CollectingStream):
        def __init__(self):
            super().__init__()
            self.write_calls = 0

        def write(self, text):
            self.write_calls += 1
            super().write(text)

    stream = CountingStream()
    sink = AsyncLineSink(stream=stream)
    for i in range(300):
        sink.write_line(f"line-{i}")
    sink.close()

    assert stream.write_calls < 300
    assert stream.received.count("\n") == 300


@pytest.mark.parametrize("bad_write", [OSError("closed"), ValueError("closed")])
def test_write_failure_does_not_raise(bad_write):
    # 下游关掉（pm2 重启中）时，日志写不出去不应该把异常抛回业务线程。
    class BrokenStream:
        def write(self, text):
            raise bad_write

        def flush(self):
            pass

    sink = AsyncLineSink(stream=BrokenStream())
    sink.write_line("boom")
    sink.close()


def test_survives_real_stdout_round_trip(capsys):
    # 回归：曾经因为写 bytes 到 sys.stdout 让整条日志线程崩掉，而单测用假字节流没抓到。
    sink = AsyncLineSink(stream=sys.stdout)
    sink.write_line("hello")
    sink.close()

    assert "hello" in capsys.readouterr().out


def test_background_thread_survives_unexpected_exception():
    # 回归：日志线程曾因「向文本 stdout 写 bytes」整条崩掉，此后日志全丢且无提示。
    # 这里用非 OSError/ValueError 的异常锁死：线程必须继续服务后续写入。
    class FlakyStream(CollectingStream):
        def __init__(self):
            super().__init__()
            self.failures_left = 1

        def flush(self):
            if self.failures_left > 0:
                self.failures_left -= 1
                raise RuntimeError("flush 炸了")

    stream = FlakyStream()
    sink = AsyncLineSink(stream=stream)
    sink.write_line("first")
    time.sleep(0.5)          # 让后台线程撞上那次 flush 异常
    sink.write_line("second")
    sink.close()

    assert sink._thread.is_alive() is False   # 已正常退出，不是崩掉
    assert "second" in stream.received        # 异常之后仍能写出
