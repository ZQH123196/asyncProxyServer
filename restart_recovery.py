"""重启恢复：进程重启后，残留的 pending / processing 任务怎么处理。

判据（本项目定案）：

1. 进程重启时，库里残留的 pending / processing 任务的执行线程已随进程消失。
   它们不会自己变终态，必须在这里裁决。
2. **上游可达** → 恢复为 pending 并重新派发。此时「中断」是本服务重启造成的，
   调用方还在等，把任务判死会让调用方整批重提，形成启动风暴。
3. **上游不可达** → 标 failed。此时整条链路都不在，重新派发只会立刻再失败一次；
   直接给出终态，调用方无需空等一轮超时。

上游可达性由调用方探测后以布尔值传入：本模块不发起网络请求，保持可测。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

RESTARTED_RESULT = "server restarted"
"""上游不可达时给残留任务的原因，调用方据此识别「需要整段重试」。"""


@dataclass(frozen=True)
class RestartOutcome:
    """一次重启恢复的结果。"""

    resumed_task_ids: tuple[str, ...]
    """恢复为 pending 的任务，调用方需要重新派发。"""

    failed_count: int
    """被标记为 failed 的任务数（上游不可达时）。"""


def recover_interrupted_tasks(
    conn: sqlite3.Connection, upstream_reachable: bool
) -> RestartOutcome:
    """裁决残留的 pending / processing 任务。

    上游可达时只把 status 改回 pending，`result` 置空：这些任务要重新从头生成，
    上一次的 result 已无意义。上游不可达时标 failed 并写明原因。
    """
    if upstream_reachable:
        rows = conn.execute(
            "SELECT task_id FROM tasks WHERE status IN ('pending', 'processing')"
        ).fetchall()
        if not rows:
            return RestartOutcome(resumed_task_ids=(), failed_count=0)
        conn.execute(
            "UPDATE tasks SET status='pending', result=NULL "
            "WHERE status IN ('pending', 'processing')"
        )
        return RestartOutcome(
            resumed_task_ids=tuple(task_id for (task_id,) in rows),
            failed_count=0,
        )

    cursor = conn.execute(
        "UPDATE tasks SET status='failed', result=? "
        "WHERE status IN ('pending', 'processing')",
        (RESTARTED_RESULT,),
    )
    return RestartOutcome(resumed_task_ids=(), failed_count=cursor.rowcount or 0)
