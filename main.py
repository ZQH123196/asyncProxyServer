import json
import queue
import sqlite3
import time
import os
import re
import shutil
import sys
import subprocess
import requests
import anyio.to_thread
import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect
from env_config import StartupConfigError, check_lockfile, load_server_env
from log_sink import AsyncLineSink, format_event
from restart_recovery import RESTARTED_RESULT, recover_interrupted_tasks
from retention import is_expired
from tts_quality import assess_tts_audio_duration, generate_with_quality_retry

# 落盘缓冲由 AsyncLineSink 统一控制：日志经有界队列交给后台线程攒批写。
# 这里只保留编码设置；不设 line_buffering，否则每行一次 write 的系统调用开销
# 由业务线程承担（这正是要消灭的阻塞点）。
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace', line_buffering=True)

SNAPSHOT_INTERVAL = 30   # 运行中任务快照打印间隔：30秒
IDLE_HEARTBEAT_INTERVAL = 5 * 60  # 待机心跳打印间隔：5分钟
TTS_MAX_RETRIES = 3      # TTS 生成最大重试次数（含首次）
ENCODE_CONCURRENCY = max(1, os.cpu_count() // 10)  # opus 编码并发上限：CPU 密集，限为核数 1/10 向下取整（至少 1）
THREADPOOL_SIZE = 64     # anyio 线程池上限（默认 40）：状态查询与后台任务共用，显式抬高
REDISPATCH_WORKERS = 16  # 重启恢复时的重派发线程数：只决定同时挂起的上游请求数
MAX_RERUN_ROUNDS = 3     # 同一 task_id 因重复提交而接着跑的轮数上限：防调用方疯狂重试占住线程
OPUS_SAMPLE_RATE = 48000  # opus 产物采样率：与仓库交付音轨一致，显式钉住，不靠 libopus 隐式行为

_encode_semaphore = threading.Semaphore(ENCODE_CONCURRENCY)

_running_task_ids: set[str] = set()
"""正在执行的 task_id。同一 task_id 同时只允许一个执行体。

同一 task_id 有两条进入路径：提交接口的 BackgroundTasks，以及重启后的重新派发。
`INSERT OR REPLACE` 只重置库里的行，不会终止已在跑的旧线程；
两条路径都写 `<id>.txt/.wav/.opus`，并发重入会让产物互相覆盖。
"""
_pending_rerun: dict[str, bytes] = {}
"""执行中又被提交的 task_id 及其最新请求体。

后到者不能直接丢弃：提交接口已经把库里的行重置成 pending，而先到者收尾时会写
自己的终态覆盖它。丢掉就等于「这次提交没人服务、库里却永远 pending」。
这里记下最新请求体，先到者结束后由它接着跑一轮。
"""
_running_lock = threading.Lock()


NOT_STARTED = "not_started"
"""`_begin_task` 的返回值：本次由调用方执行。"""

ALREADY_RUNNING = "already_running"
"""`_begin_task` 的返回值：已在跑，本次请求体已记为待重跑。"""


def _begin_task(task_id: str, raw_body: bytes) -> str:
    """占住 task_id。返回 NOT_STARTED（由本次执行）或 ALREADY_RUNNING（已排队待重跑）。"""
    with _running_lock:
        if task_id in _running_task_ids:
            _pending_rerun[task_id] = raw_body
            return ALREADY_RUNNING
        _running_task_ids.add(task_id)
        return NOT_STARTED


def _finish_round(task_id: str) -> bytes | None:
    """一轮正常跑完：原子地「取走待重跑 + 决定是否续占位」。

    有重跑则**保留占位**（同一个执行体接着跑下一轮，中间不放行其他人）；
    没有则释放。取走与释放必须在同一把锁内，否则会出现「已释放、新提交还没进来」的空窗。
    """
    with _running_lock:
        rerun = _pending_rerun.pop(task_id, None)
        if rerun is None:
            _running_task_ids.discard(task_id)
        return rerun


def _release_task(task_id: str) -> None:
    """无条件释放占位。异常退出路径必须走它。

    这里不能复用 `_finish_round`：那个函数在有重跑时不释放占位，是给「接着跑一轮」用的。
    异常路径若不释放，该 task_id 之后永远是 ALREADY_RUNNING，再也没人执行。
    队列里残留的重跑意图也一并清掉 —— 本轮已失败，让调用方重新提交更可预期。
    """
    with _running_lock:
        _pending_rerun.pop(task_id, None)
        _running_task_ids.discard(task_id)


def _now() -> str:
    return time.strftime("%H:%M:%S")


_REDISPATCH_QUEUE: list[str] = []
"""重启恢复后待重新派发的 task_id。

import 期只收集，不启动线程：此时 FastAPI 尚未就绪，事件循环还没跑起来。
派发在 lifespan 启动阶段做，与正常提交走同一条 hard_tts_work 路径。
"""


_LOG_SINK = AsyncLineSink()


def log_event(event: str, **payload) -> None:
    """结构化日志：标量字段压缩成一行，多值字段折成一行附在其后。

    渲染在调用线程完成，落盘经有界队列交给后台线程批量写。
    业务线程永不阻塞在 write() 上：下游慢只让队列积压，满则丢并计数。
    """
    for line in format_event(event, payload, _now()):
        _LOG_SINK.write_line(line)


# ==========================================
# 启动配置与运行前置检查：不通过即拒绝启动，不留到某个 TTS 任务中途才炸
# ==========================================
try:
    SERVER_ENV = load_server_env()
    _startup_problems: list[str] = []
    # 依赖锁比对读的是已装环境，故与配置校验分开调用、汇总报错：
    # 混进 load_server_env 会让该函数的单测依赖完整安装。
    check_lockfile(_startup_problems)
    if _startup_problems:
        raise StartupConfigError(_startup_problems)
except StartupConfigError as config_error:
    for problem in config_error.problems:
        log_event("startup_config_error", problem=problem)
    sys.exit(1)

AUDIO_DIR = SERVER_ENV.audio_dir
DB_FILE = SERVER_ENV.db_file
SGLANG_URL = SERVER_ENV.sglang_url
RETENTION = SERVER_ENV.retention
CLEANUP_INTERVAL = 60  # 清理巡检间隔：60 秒
AUDIO_EXTS = (".wav", ".opus", ".txt", ".req")
"""产物目录中受清理策略管辖的扩展名。

`.req` 是提交时落盘的原始请求体，重启恢复要读它。必须入册，否则会永久堆积。
清理时它与同名 .txt 一样按任务 status 判定（见 _scan_artifacts）。
"""
log_event("startup_config", config=SERVER_ENV.describe())


def _sanitize_task_id(raw: str) -> str:
    """剥除路径分隔符与 .wav 后缀，避免写入 AUDIO_DIR 之外。

    客户端可上送与本地产出同名的标识用作任务键与落盘文件名前缀，方便端到端调试。
    这里仅做最小防御：取最后一段截断路径穿越，剔 .wav 后缀（落盘时再统一拼回）。
    """
    if not raw:
        return ""
    value = raw.replace("\\", "/").split("/")[-1]
    value = re.sub(r"\.wav$", "", value, flags=re.IGNORECASE)
    value = value.strip()
    return value


# ==========================================
# 清理后台守护任务（只读 DB，不修改任务状态）
# ==========================================
@dataclass(frozen=True)
class _Artifact:
    """一个可清理的产物文件。"""

    path: str
    name: str
    task_id: str
    size: int
    mtime: float
    failed: bool


def _task_ids_by_status() -> tuple[set[str], set[str]]:
    """读一次任务状态，返回（失败任务, 在跑任务）两个集合。

    只取三种状态：tasks 表没有删除路径，全表拉取会随运行时长无限变慢。
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT task_id, status FROM tasks WHERE status IN ('failed','pending','processing')"
        ).fetchall()
    failed = {task_id for task_id, status in rows if status == "failed"}
    active = {task_id for task_id, status in rows if status in ("pending", "processing")}
    return failed, active


def _scan_artifacts(failed_ids: set[str], active_ids: set[str]) -> list[_Artifact]:
    """枚举可清理的产物。

    pending/processing 任务的文件一律不入候选：它们可能正被写入或尚未编码完成。
    TTS 单次请求超时上限是 3600 秒，保留时间压到 5 分钟后，若把它们纳入候选，
    清理线程会删掉正在生成的音频与文本。
    """
    artifacts: list[_Artifact] = []
    with os.scandir(AUDIO_DIR) as entries:
        for entry in entries:
            stem, ext = os.path.splitext(entry.name)
            if ext not in AUDIO_EXTS or stem in active_ids:
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            artifacts.append(
                _Artifact(
                    path=entry.path,
                    name=entry.name,
                    task_id=stem,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    failed=stem in failed_ids,
                )
            )
    return artifacts


def _remove(artifact: _Artifact) -> bool:
    """删一个文件。个别文件删不掉（如 Windows 下正被下载占用）不中断本轮其余删除。"""
    try:
        os.remove(artifact.path)
    except OSError as e:
        log_event("cleanup_error", error=f"删除 {artifact.name} 失败：{e}")
        return False
    return True


def _retention_label(seconds: float | None) -> str:
    return "keep" if seconds is None else f"{int(seconds)}s"


def _disk_state(usage: float) -> str:
    if usage > RETENTION.critical:
        return "critical"
    if usage >= RETENTION.limit:
        return "limit"
    if usage > RETENTION.start:
        return "pressure"
    return "normal"


def _cleanup_once() -> float:
    """跑一轮清理，返回本轮观测到的磁盘已用率。"""
    total, used, _free = shutil.disk_usage(AUDIO_DIR)
    if total <= 0:
        raise RuntimeError(f"无法读取 {AUDIO_DIR} 所在文件系统的容量")
    usage = used / total

    failed_ids, active_ids = _task_ids_by_status()
    artifacts = _scan_artifacts(failed_ids, active_ids)
    retention = RETENTION.success_retention_seconds(usage)
    now = time.time()

    candidates = [a for a in artifacts if is_expired(a.failed, a.mtime, now, retention)]
    if candidates:
        # 扫描与删除之间可能有人用同一 task_id 重新提交（INSERT OR REPLACE），
        # 新任务会立刻写同名 .txt/.wav。删前重读一次在跑集合，缩小这个窗口。
        _, active_now = _task_ids_by_status()
        candidates = [a for a in candidates if a.task_id not in active_now]
    candidates.sort(key=lambda a: a.mtime)

    removed = [a for a in candidates if _remove(a)]
    freed = sum(a.size for a in removed)
    if removed:
        log_event(
            "cleanup_expired_files",
            usage=f"{usage:.1%}",
            retention=_retention_label(retention),
            count=len(removed),
            freed_bytes=freed,
            sample=[a.name for a in removed[:5]],
        )

    target = RETENTION.reclaim_target_bytes(total, used)
    if target <= 0:
        return usage

    # 强制回收：失败产物优先，同类里最旧的先删；不看保留时间。
    removed_paths = {a.path for a in removed}
    _, active_now = _task_ids_by_status()
    remaining = sorted(
        (
            a
            for a in artifacts
            if a.path not in removed_paths and a.task_id not in active_now
        ),
        key=lambda a: (not a.failed, a.mtime),
    )
    forced_freed = 0
    forced: list[str] = []
    for artifact in remaining:
        if freed + forced_freed >= target:
            break
        if not _remove(artifact):
            continue
        forced_freed += artifact.size
        forced.append(artifact.name)

    # 按实际剩余空间复核：被下载中的文件 unlink 后 inode 未释放，字节数会高估回收量。
    total_after, used_after, _free_after = shutil.disk_usage(AUDIO_DIR)
    usage_after = used_after / total_after
    log_event(
        "cleanup_forced_reclaim",
        usage=f"{usage:.1%}",
        target_bytes=target,
        expired_freed_bytes=freed,
        forced_count=len(forced),
        forced_freed_bytes=forced_freed,
        usage_after=f"{usage_after:.1%}",
        shortfall_bytes=max(0, used_after - int(total_after * RETENTION.limit)),
        sample=forced[:5],
    )
    return usage


_last_disk_state: str | None = None


async def cleanup_loop():
    """按磁盘用量调节保留时间，并清理过期产物。

    保留时间见 retention.py 的映射。用量越过 start/limit/critical 时各记一行。
    每轮重新读容量：同一块盘上还有模型、数据库等其他写入方。
    """
    global _last_disk_state
    while True:
        try:
            usage = _cleanup_once()
            state = _disk_state(usage)
            if state != _last_disk_state:
                event = "cleanup_disk_state" if state == "normal" else "cleanup_disk_pressure"
                log_event(
                    event,
                    state=state,
                    usage=f"{usage:.1%}",
                    retention=_retention_label(RETENTION.success_retention_seconds(usage)),
                )
                _last_disk_state = state
        except Exception as e:
            log_event("cleanup_error", error=str(e))

        await asyncio.sleep(CLEANUP_INTERVAL)


async def snapshot_running_tasks_loop():
    """周期性查询 DB，把正在运行的任务 (pending/processing) 以多行列表方式打印。

    每个任务一个对象，整体作为数组输出，绝不叠在一行，方便像列表一样浏览。
    只读 DB，不修改状态。
    """
    while True:
        interval = SNAPSHOT_INTERVAL
        try:
            with _db() as conn:
                rows = conn.execute(
                    "SELECT task_id, status FROM tasks WHERE status IN ('pending','processing')"
                ).fetchall()
            tasks = [{"task_id": tid, "status": st} for tid, st in rows]
            if tasks:
                log_event("running_tasks_snapshot", count=len(tasks), tasks=tasks)
            else:
                log_event("heartbeat", status="idle")
                interval = IDLE_HEARTBEAT_INTERVAL
        except Exception as e:
            log_event("snapshot_error", error=str(e))
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 默认 40，状态查询与 BackgroundTasks 同步任务共用；抬高到 THREADPOOL_SIZE
    anyio.to_thread.current_default_thread_limiter().total_tokens = THREADPOOL_SIZE
    _dispatch_resumed_tasks()
    cleanup_task = asyncio.create_task(cleanup_loop())
    snapshot_task = asyncio.create_task(snapshot_running_tasks_loop())
    yield
    cleanup_task.cancel()
    snapshot_task.cancel()
    _LOG_SINK.close()


def _dispatch_resumed_tasks() -> None:
    """把重启恢复的任务重新派发出去。

    与正常提交共用 hard_tts_work，区别只是入队时机在 lifespan 而非请求里。
    仍读不到请求体的任务在 init_db 里已经判 failed，不会进这里。

    重启时可能一次恢复上百个任务。这里用固定大小的线程池消费，不按任务数起线程：
    上游 TTS 本身是串行队列，起再多线程也只是在 requests.post 上等待。
    线程池大小取 REDISPATCH_WORKERS，与 ENCODE_CONCURRENCY 无关。
    """
    task_ids = list(_REDISPATCH_QUEUE)
    _REDISPATCH_QUEUE.clear()
    if not task_ids:
        return

    workers = min(REDISPATCH_WORKERS, len(task_ids))
    pending: queue.SimpleQueue[str | None] = queue.SimpleQueue()

    def worker() -> None:
        while True:
            task_id = pending.get()
            if task_id is None:
                return
            raw_body = _load_request_body(task_id)
            if raw_body is None:
                continue
            hard_tts_work(task_id, raw_body)

    for index in range(workers):
        threading.Thread(target=worker, name=f"redispatch-{index}", daemon=True).start()
    for task_id in task_ids:
        pending.put(task_id)
    for _ in range(workers):
        pending.put(None)


# ==========================================
_DB_LOCAL = threading.local()


def _db() -> sqlite3.Connection:
    """每线程复用一个 SQLite 连接。

    WAL：读写不互斥，状态轮询/快照查询不再被任务状态写入挡住；
    timeout=5：锁冲突时等待而非直接抛 database is locked（
    否则任务实际成功却被误标 failed）。
    """
    conn = getattr(_DB_LOCAL, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        _DB_LOCAL.conn = conn
    return conn


def _upstream_reachable(timeout: float = 2.0) -> bool:
    """探测上游 TTS 端点是否可达。

    判据是「上游进程活着、能收下请求」，不是「现在能出音频」：
    收到任何 HTTP 响应（含 404 / 405 / 503）都算可达 —— 上游加载模型期间回 503 是常态，
    此时把它判死会让残留任务全部 failed，调用方整批重提，正是要消灭的启动风暴。
    只有连不上（连接被拒 / DNS 失败 / 超时）才算不可达。
    """
    try:
        with requests.get(SGLANG_URL, timeout=timeout, stream=True) as response:
            return response.status_code > 0
    except requests.RequestException:
        return False


STALE_TEMP_SECONDS = 3600
"""临时请求体文件超过这个年龄即视为上次进程的残留。正常写入是毫秒级。"""

REQUEST_BODY_SUFFIX = ".req"
REQUEST_BODY_TEMP_SUFFIX = ".req.tmp"


def _request_body_path(task_id: str) -> str:
    return os.path.join(AUDIO_DIR, f"{task_id}{REQUEST_BODY_SUFFIX}")


def _request_body_temp_path(task_id: str) -> str:
    """临时文件名带上进程号与线程号。

    同一 task_id 本来就被执行体互斥保护，带上这两个号是为了让清理逻辑能分辨
    「本次进程正在写的临时文件」与「上个进程被 kill 留下的残留」。
    """
    return os.path.join(
        AUDIO_DIR,
        f"{task_id}{REQUEST_BODY_TEMP_SUFFIX}.{os.getpid()}.{threading.get_ident()}",
    )


def _persist_request_body(task_id: str, raw_body: bytes) -> None:
    """把原始请求体写到产物目录，供重启后重新派发。

    先写临时文件再改名：崩溃时不会留下半截请求体被误读。
    改名失败时原地清掉临时文件，不留孤儿。
    """
    final = _request_body_path(task_id)
    temp = _request_body_temp_path(task_id)
    try:
        with open(temp, "wb") as f:
            f.write(raw_body)
        os.replace(temp, final)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _load_request_body(task_id: str) -> bytes | None:
    try:
        with open(_request_body_path(task_id), "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _sweep_stale_request_body_temps() -> int:
    """删掉上次进程被 kill 时留下的 `<task_id>.req.tmp.*`。

    这些半截文件不会被读到（读的只是 `.req`），也不在清理策略的扩展名白名单里，
    不主动扫就会永久堆积。启动期扫一次即可。

    判据用**修改时间**而不是进程号：写请求体是毫秒级的动作，
    比 `STALE_TEMP_SECONDS` 还旧的临时文件必然是残留。
    不用 `os.kill(pid, 0)` 探活：Windows 上它对任意信号值都调 TerminateProcess，
    pid 复用时会把无关进程杀掉。
    """
    if not os.path.isdir(AUDIO_DIR):
        return 0
    deadline = time.time() - STALE_TEMP_SECONDS
    removed = 0
    with os.scandir(AUDIO_DIR) as entries:
        for entry in entries:
            if REQUEST_BODY_TEMP_SUFFIX not in entry.name:
                continue
            try:
                if not entry.is_file() or entry.stat().st_mtime > deadline:
                    continue
                os.remove(entry.path)
                removed += 1
            except OSError:
                continue
    return removed


def init_db() -> None:
    """建表，并裁决上一次进程遗留的 pending / processing 任务。

    重启不再无条件把在跑任务判死（旧做法会引爆调用方的全量重提）。
    上游可达就把它们恢复为 pending 并重新派发；不可达才标 failed。
    """
    swept = _sweep_stale_request_body_temps()
    if swept:
        log_event("sweep_request_body_temps", count=swept, reason="上次进程被 kill 留下的半截文件")

    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT,
                result TEXT
            )
        """)
        outcome = recover_interrupted_tasks(conn, _upstream_reachable())

    if not outcome.resumed_task_ids:
        if outcome.failed_count:
            log_event(
                "restart_fail",
                count=outcome.failed_count,
                reason=f"upstream unreachable, result={RESTARTED_RESULT}",
            )
        return

    # 没有请求体的任务无法重新派发（旧版本提交的、或落盘被清理的）：
    # 判 failed 而不是让它永远停在 pending，调用方才能拿到终态。
    redispatchable: list[str] = []
    missing: list[str] = []
    for task_id in outcome.resumed_task_ids:
        (redispatchable if _load_request_body(task_id) is not None else missing).append(task_id)

    if missing:
        with _db() as conn:
            conn.executemany(
                "UPDATE tasks SET status='failed', result=? WHERE task_id=?",
                [(f"{RESTARTED_RESULT} (request body missing)", task_id) for task_id in missing],
            )
        log_event("restart_fail", count=len(missing), reason="request body missing")

    if redispatchable:
        log_event(
            "restart_resume",
            count=len(redispatchable),
            reason="upstream reachable, re-dispatch interrupted tasks",
            task_ids=redispatchable,
        )
        for task_id in redispatchable:
            _REDISPATCH_QUEUE.append(task_id)


init_db()

app = FastAPI(title="TTS 异步代理服务", lifespan=lifespan)
app.mount("/download", StaticFiles(directory=AUDIO_DIR), name="audio")


@app.get("/v1/audio/async/status/{task_id}")
def check_tts_status(task_id: str):
    """
    提供给客户端轮询查询状态的接口
    """
    with _db() as conn:
        cursor = conn.execute("SELECT status, result FROM tasks WHERE task_id=?", (task_id,))
        row = cursor.fetchone()
    
    # 恢复你原本的纯粹逻辑
    if not row:
        return {"task_id": task_id, "status": "failed", "result": "Task not found"}
        
    status, result = row
    return {"task_id": task_id, "status": status, "result": result}


@app.post("/v1/audio/async/status/batch")
def batch_check_status(body: dict):
    # 同步 def：FastAPI 自动放入线程池执行，sqlite 查询不再阻塞事件循环
    task_ids = body.get("task_ids", [])
    log_event("batch_status_lookup", count=len(task_ids), task_ids=task_ids)

    if not isinstance(task_ids, list):
        raise HTTPException(status_code=400, detail="task_ids must be a list")
    if len(task_ids) == 0:
        return {"results": []}

    placeholders = ",".join("?" * len(task_ids))
    query = f"SELECT task_id, status, result FROM tasks WHERE task_id IN ({placeholders})"

    with _db() as conn:
        rows = conn.execute(query, task_ids).fetchall()

    by_id = {row[0]: row for row in rows}
    results = []
    for tid in task_ids:
        row = by_id.get(tid)
        if row is None:
            results.append({"task_id": tid, "status": "failed", "result": "Task not found"})
        else:
            _, status, result = row
            results.append({"task_id": tid, "status": status, "result": result})
    return {"results": results}


def _validate_tts_audio_quality(wav_path: str, text: str) -> dict:
    """读取 WAV 时长并执行挤碎/拉伸检测；≤3s 的短音频直接放行。"""
    import soundfile as sf

    duration = sf.info(wav_path).duration
    if duration <= 3:
        return {"passed": True, "diag": f"正常 | 短音频≤3s免于挤碎/拉伸检测 | 时长={duration:.2f}s"}
    return assess_tts_audio_duration(duration, text)


def _encode_opus(raw_wav_path: str, opus_path: str) -> None:
    """直接把原始 TTS WAV 编码为 Opus，不做任何音频裁剪或补静音。

    **这是本服务的边界，不是遗漏。** 本服务只做「异步中转 + 编码」，职责单一。
    模型对音频有自己的习性（例如某些模型在标点后插入长静音），这类习性随上游模型变化。
    把它们放进中转层，等于让一个与模型无关的服务跟着每个模型漂。
    调用方已有各自的音频管线，同一特例在多个项目各实现一遍，维护成本随项目数相乘。
    故模型习性归调用方的音频管线，本服务只保证「字节进、素材出」，不改变时长。

    采样率显式钉 OPUS_SAMPLE_RATE（48 kHz）。Ogg 容器只会声明 48000，
    但不写 -ar 时 ffmpeg 按源采样率喂 libopus，窄带源会走窄带编码分支，
    产物码率分配随上游采样率漂移。显式钉住后该行为与源无关。
    """
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", raw_wav_path,
        "-ar", str(OPUS_SAMPLE_RATE),
        "-c:a", "libopus",
        "-b:a", "96k",
        "-application", "audio",
        "-vbr", "on",
        "-compression_level", "10",
        opus_path,
    ]
    proc = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg opus 编码失败 returncode={proc.returncode} "
            f"stderr={proc.stderr.decode('utf-8', errors='ignore')[:500]}"
        )
    if not os.path.exists(opus_path):
        raise RuntimeError("opus 产物未生成")


def hard_tts_work(task_id: str, raw_body: bytes):
    """执行一个 TTS 任务：生成 → 质检（带重试）→ 编码 opus → 回写状态。

    同一 task_id 同时只允许一个执行体：提交接口与重启重派是两条独立进入路径，
    都在写 `<id>.wav/.opus`。执行中又被提交时，把最新请求体记为待重跑，
    由当前执行体跑完后接着跑，而不是丢弃。
    """
    if _begin_task(task_id, raw_body) == ALREADY_RUNNING:
        # 已在跑：请求体已记为待重跑，先到者结束后会接着跑这一份。
        # 这里不能直接返回而不留痕迹——否则调用方提交的这次就无人服务。
        log_event(
            "task_queued_after_running",
            task_id=task_id,
            reason="same task_id is running, queued to rerun after it finishes",
        )
        return

    # 正常跑完：_finish_round 原子地「取走待重跑 + 决定续占位」；有重跑就用最新请求体
    # 再跑一轮。异常退出：_release_task 无条件释放，绝不把占位留在集合里。
    rounds = 0
    while True:
        rounds += 1
        try:
            _run_tts_task(task_id, raw_body)
        except BaseException:
            _release_task(task_id)
            raise
        rerun = _finish_round(task_id)
        if rerun is None:
            return
        if rounds >= MAX_RERUN_ROUNDS:
            # 调用方在持续重复提交同一个 task_id。再跑下去等于替它做无限重试，
            # 会一直占住一个线程和编码信号量。停下并释放，下一次提交重新排队即可。
            _release_task(task_id)
            log_event(
                "task_rerun_capped",
                task_id=task_id,
                rounds=rounds,
                reason="repeated submissions exceeded MAX_RERUN_ROUNDS",
            )
            return
        log_event("task_rerun", task_id=task_id, reason="submitted again while running")
        raw_body = rerun


def _run_tts_task(task_id: str, raw_body: bytes) -> None:
    log_event("task_start", task_id=task_id, status="processing")
    with _db() as conn:
        conn.execute("UPDATE tasks SET status='processing' WHERE task_id=?", (task_id,))

    try:
        body_json = json.loads(raw_body)
        input_text = body_json.get("input", "")

        # 文本开跑即落盘：失败任务（质检不合格/请求异常）也要留文本供 debug
        with open(os.path.join(AUDIO_DIR, f"{task_id}.txt"), "w", encoding="utf-8") as f:
            f.write(input_text)

        wav_name = f"{task_id}.wav"
        wav_path = os.path.join(AUDIO_DIR, wav_name)

        def generate_audio() -> bytes:
            response = requests.post(
                SGLANG_URL,
                data=raw_body,
                headers={"Content-Type": "application/json"},
                timeout=3600
            )
            response.raise_for_status()
            return response.content

        def validate_audio(audio_content: bytes) -> dict:
            with open(wav_path, "wb") as f:
                f.write(audio_content)
            return _validate_tts_audio_quality(wav_path, input_text)

        def on_quality_failure(attempt: int, quality: dict | None, error: Exception | None) -> None:
            if error is not None:
                log_event(
                    "quality_check_error",
                    task_id=task_id,
                    attempt=attempt,
                    max_retries=TTS_MAX_RETRIES,
                    error=str(error),
                )
                return

            log_event(
                "quality_check_failed",
                task_id=task_id,
                attempt=attempt,
                max_retries=TTS_MAX_RETRIES,
                diag=quality.get("diag", "未知质量问题") if quality else "未知质量问题",
            )

        audio_content, quality_result, attempt = generate_with_quality_retry(
            generate_audio,
            validate_audio,
            max_retries=TTS_MAX_RETRIES,
            on_failure=on_quality_failure,
        )
        log_event(
            "quality_check_passed",
            task_id=task_id,
            attempt=attempt,
            diag=quality_result["diag"],
        )

        opus_name = f"{task_id}.opus"
        opus_path = os.path.join(AUDIO_DIR, opus_name)
        with _encode_semaphore:
            _encode_opus(wav_path, opus_path)

        download_path = f"/download/{quote(opus_name)}"

        with _db() as conn:
            conn.execute("UPDATE tasks SET status='success', result=? WHERE task_id=?", (download_path, task_id))

        log_event(
            "task_done",
            task_id=task_id,
            status="success",
            result=opus_name,
            src_bytes=len(audio_content),
            opus_bytes=int(os.path.getsize(opus_path)),
        )

    except Exception as e:
        with _db() as conn:
            conn.execute("UPDATE tasks SET status='failed', result=? WHERE task_id=?", (str(e), task_id))
        log_event("task_failed", task_id=task_id, status="failed", error=str(e))

@app.post("/v1/audio/async/speech")
async def proxy_start_tts(request: Request, background_tasks: BackgroundTasks, task_id: str):
    try:
        raw_body = await request.body()
    except ClientDisconnect:
        # 客户端在请求体读完前断开（如上游 submit timeout 自动重试），
        # 任务未入 DB，重试会以同 task_id 重新提交；明确记日志而非 500 崩栈
        log_event(
            "submit_aborted",
            task_id=task_id,
            reason="client disconnected before body received",
        )
        raise HTTPException(status_code=499, detail="client disconnected before body received")
    final_task_id = _sanitize_task_id(task_id)
    if not final_task_id:
        raise HTTPException(status_code=400, detail="task_id is required and must be non-empty")

    # 原始请求体先落盘再入库：重启恢复要按它重新派发。落盘失败即拒绝提交，
    # 否则会出现「库里 pending、盘上无请求体」的不可恢复任务。
    _persist_request_body(final_task_id, raw_body)
    with _db() as conn:
        # INSERT OR REPLACE：同 task_id 的重试/重复运行重置为 pending 并重新派发后台任务
        conn.execute("INSERT OR REPLACE INTO tasks (task_id, status, result) VALUES (?, 'pending', NULL)", (final_task_id,))

    background_tasks.add_task(hard_tts_work, final_task_id, raw_body)
    log_event("task_submitted", task_id=final_task_id, status="pending", body_bytes=len(raw_body))
    return {"task_id": final_task_id, "status": "pending"}


if __name__ == "__main__":
    import uvicorn
    # 传 app 对象而非 "main:app"：避免本模块被二次导入，重复跑一遍启动检查
    uvicorn.run(app, host=SERVER_ENV.host, port=SERVER_ENV.port, proxy_headers=True)
