"""日志落盘：有界队列 + 单后台线程批量写。

判据：

1. 业务线程永不阻塞在 `write()` 上。后台线程写不出去（下游慢 / 管道满）时，
   只让队列积压，不反压业务线程。
2. 队列有界。满时丢弃新日志并计数，恢复后补一行汇总，不阻塞。
   正常量级（一次批量查询 1 条日志）永远写不满队列，这个上限只是防呆。
3. 进程退出时把队列排空再关流，尾部日志不丢。

为什么不用第三方异步日志库：阻塞的唯一原因是文件描述符写不出去，任何库最终都写
同一个 fd。标准库已有的就是本类要的能力（有界队列 + 单消费线程 + 攒批写），
不需要新增依赖，也不需要改 uv.lock。
"""

from __future__ import annotations

import queue
import sys
import threading

# 队列容量。一次批量查询产生 1 条日志；10000 条是极大量级的防呆上限。
QUEUE_MAX_LINES = 10000

# 一次 flush 最多合并多少行。只影响后台线程的系统调用次数，不影响入队侧。
FLUSH_BATCH_LINES = 256

# 后台线程最长等待时间（秒）。超时即把现存行写出，保证正常量级下日志及时可见。
DRAIN_TIMEOUT_SECONDS = 0.2

_CLOSE = object()
"""关闭哨兵。用独立对象而非 None，避免与正常行混淆。"""

# 失败 / 成功事件的行首标记。进程由 pm2 托管、无 TTY，颜色分支永远不生效，
# 故只保留标记，不做 ANSI 着色。
_FAILURE_MARKERS = {
    "startup_config_error": "❌",
    "quality_check_error": "❌",
    "quality_check_failed": "⚠️",
    "task_failed": "💥",
    "submit_aborted": "⚠️",
    "cleanup_error": "❌",
    "cleanup_disk_pressure": "⚠️",
    "cleanup_forced_reclaim": "⚠️",
    "snapshot_error": "❌",
}
_SUCCESS_MARKERS = {
    "quality_check_passed": "✅",
    "task_done": "✅",
}


def format_event(event: str, payload: dict, now: str) -> list[str]:
    """把一个事件渲染成若干行，纯函数。

    多值字段（`task_ids`、`tasks`）一律折成一行，用逗号拼接。
    不再每项一行：一次批量查询带 146 个 id 时，逐行渲染会产生 444 行、约 12KB，
    足以写满日志管道并反压业务线程。折行后行数从 444 降到 1，id 一个不丢。
    """
    scalars: dict[str, object] = {}
    multi: dict[str, list] = {}
    for key, value in payload.items():
        if isinstance(value, (list, tuple)):
            multi[key] = list(value)
        elif isinstance(value, dict):
            multi[key] = list(value.values())
        else:
            scalars[key] = value

    head = f"{now}  {event:<22}"
    if scalars:
        kv = "  ".join(f"{key}={value}" for key, value in scalars.items())
        line = f"{head}  {kv}"
    else:
        line = head

    if event in _FAILURE_MARKERS:
        line = f"{_FAILURE_MARKERS[event]} {line}"
    elif event in _SUCCESS_MARKERS:
        line = f"{_SUCCESS_MARKERS[event]} {line}"

    lines = [line]
    for name, items in multi.items():
        lines.append(f"  -- {name} ({len(items)}) {','.join(str(item) for item in items)}")
    return lines


class AsyncLineSink:
    """把日志行经有界队列交给后台线程批量写出。

    只负责「不阻塞 + 批量写」。格式化（表格、颜色）由调用方在入队前完成。
    """

    def __init__(self, stream=None, max_lines: int = QUEUE_MAX_LINES) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=max_lines)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(target=self._drain, name="log-sink", daemon=True)
        self._thread.start()

    def write_line(self, line: str) -> None:
        """投递一行。队列满时丢弃并计数，绝不阻塞。"""
        if self._closed:
            return
        try:
            self._queue.put_nowait(line + "\n")
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1

    def _take_dropped(self) -> int:
        with self._dropped_lock:
            count = self._dropped
            self._dropped = 0
        return count

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
        except (OSError, ValueError):
            # 下游已关闭（如 pm2 重启中）：丢弃本批。日志写不出去不应让业务线程崩溃。
            pass

    def _flush(self) -> None:
        try:
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def _emit_dropped(self) -> None:
        dropped = self._take_dropped()
        if dropped > 0:
            self._write(f"[log-sink] 队列满，丢弃 {dropped} 行日志\n")

    def _drain(self) -> None:
        """后台线程：攒批取出后一次写出，减少系统调用次数。

        整个循环体包一层兜底：任何未预期异常都只丢掉当前这批，不让线程退出。
        线程一退，之后所有日志静默消失且没有任何提示 —— 这个服务曾经因为
        「向文本 stdout 写 bytes」让日志线程崩掉，而日志全丢没人发现。
        """
        pending: list[str] = []
        closing = False
        while True:
            try:
                try:
                    item = self._queue.get(timeout=DRAIN_TIMEOUT_SECONDS)
                except queue.Empty:
                    if pending:
                        self._write("".join(pending))
                        pending = []
                        self._flush()
                    self._emit_dropped()
                    continue

                if item is _CLOSE:
                    closing = True
                else:
                    pending.append(item)
                    # 队列还有货就继续攒，不立刻写：让一次 write 覆盖多行。
                    if self._queue.qsize() > 0 and len(pending) < FLUSH_BATCH_LINES:
                        continue

                if pending:
                    self._write("".join(pending))
                    pending = []
                    self._flush()
                self._emit_dropped()
                if closing:
                    return
            except Exception as error:  # noqa: BLE001 - 后台线程兜底，必须吞掉并继续
                pending = []
                self._report_failure(error)

    def _report_failure(self, error: BaseException) -> None:
        """日志线程自己出错时，往 stderr 写一行。stderr 不走本队列，不会被自身拖住。"""
        try:
            sys.stderr.write(f"[log-sink] 日志写出异常，已丢弃本批：{error!r}\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - 连 stderr 都写不出去就只能放弃
            pass

    def close(self, timeout: float = 5.0) -> None:
        """排空队列后停止后台线程。退出路径调用。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(_CLOSE)
        except queue.Full:
            # 队列满：先等后台线程排掉一批，再放哨兵。
            self._thread.join(timeout=timeout / 2)
            try:
                self._queue.put_nowait(_CLOSE)
            except queue.Full:
                return
        self._thread.join(timeout=timeout)
