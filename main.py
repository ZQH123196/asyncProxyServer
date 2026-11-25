import json
import sqlite3
import time
import os
import re
import sys
import subprocess
import requests
import anyio.to_thread
import asyncio
import threading
import unicodedata
from contextlib import asynccontextmanager
from urllib.parse import quote
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect
from env_config import StartupConfigError, load_server_env
from tts_quality import assess_tts_audio_duration, generate_with_quality_retry

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

FILE_LIFETIME = 30 * 60  # 音频存活时间：30分钟
FAILED_FILE_LIFETIME = 3 * 24 * 60 * 60  # 失败任务的音频/文本保留：3天（debug 用）
SNAPSHOT_INTERVAL = 30   # 运行中任务快照打印间隔：30秒
IDLE_HEARTBEAT_INTERVAL = 5 * 60  # 待机心跳打印间隔：5分钟
TTS_MAX_RETRIES = 3      # TTS 生成最大重试次数（含首次）
ENCODE_CONCURRENCY = max(1, os.cpu_count() // 10)  # opus 编码并发上限：CPU 密集，限为核数 1/10 向下取整（至少 1）
THREADPOOL_SIZE = 64     # anyio 线程池上限（默认 40）：状态查询与后台任务共用，显式抬高

_encode_semaphore = threading.Semaphore(ENCODE_CONCURRENCY)


def _now() -> str:
    return time.strftime("%H:%M:%S")


_USE_COLOR = sys.stdout.isatty()

_FAILURE_MARKERS = {
    "startup_config_error": "❌",
    "quality_check_error": "❌",
    "quality_check_failed": "⚠️",
    "task_failed": "💥",
    "submit_aborted": "⚠️",
    "cleanup_error": "❌",
    "snapshot_error": "❌",
}
_SUCCESS_MARKERS = {
    "quality_check_passed": "✅",
    "task_done": "✅",
}


def _paint(text: str, color: str) -> str:
    return f"\033[{color}m{text}\033[0m" if _USE_COLOR else text


def _display_width(s: str) -> int:
    """终端显示宽度：CJK/全角字符占 2 列，其余占 1 列。"""
    w = 0
    for ch in s:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ('W', 'F') else 1
    return w


def _pad(s: str, width: int) -> str:
    """按显示宽度左对齐填充空格。"""
    return s + " " * (width - _display_width(s))


def print_table(headers: list[str], rows: list[list[str]], indent: str = "  ") -> None:
    """top 风格表格：第一行表头，第二行分隔线，其后每行一条数据。"""
    widths = [_display_width(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], _display_width(c))
    print(indent + "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers)))
    print(indent + "  ".join("-" * w for w in widths))
    for r in rows:
        print(indent + "  ".join(_pad(str(x), widths[i]) for i, x in enumerate(r)))


def log_event(event: str, **payload) -> None:
    """结构化日志：标量字段压缩成一行，列表/字典字段自动渲染成下方表格。

    禁止用 "|" 拼字符串；正在运行的任务、批量 task_ids 等以多行表格列出，
    第一列为表头，下面是数据行，便于像 top 一样浏览。
    """
    scalars: dict[str, object] = {}
    blocks: dict[str, list] = {}
    for k, v in payload.items():
        if isinstance(v, (list, dict)) and not isinstance(v, (str, bytes)):
            blocks[k] = v if isinstance(v, list) else [v]
        else:
            scalars[k] = v

    head = f"{_now()}  {event:<22}"
    if scalars:
        kv = "  ".join(f"{k}={v}" for k, v in scalars.items())
        line = f"{head}  {kv}"
    else:
        line = head

    if event in _FAILURE_MARKERS:
        line = f"{_FAILURE_MARKERS[event]} {_paint(line, '1;31')}"
    elif event in _SUCCESS_MARKERS:
        line = f"{_SUCCESS_MARKERS[event]} {_paint(line, '1;32')}"
    print(line)

    for name, items in blocks.items():
        if not items:
            print(f"  -- {name} (0) --")
            continue
        if isinstance(items[0], dict):
            headers = list(items[0].keys())
            rows = [[str(it.get(h, "")) for h in headers] for it in items]
            print(f"  -- {name} ({len(items)}) --")
            print_table(headers, rows)
        else:
            print(f"  -- {name} ({len(items)}) --")
            for it in items:
                print(f"    {it}")


# ==========================================
# 启动配置与运行前置检查：不通过即拒绝启动，不留到某个 TTS 任务中途才炸
# ==========================================
try:
    SERVER_ENV = load_server_env()
except StartupConfigError as config_error:
    for problem in config_error.problems:
        log_event("startup_config_error", problem=problem)
    sys.exit(1)

AUDIO_DIR = SERVER_ENV.audio_dir
DB_FILE = SERVER_ENV.db_file
SGLANG_URL = SERVER_ENV.sglang_url
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
# 清理后台守护任务（只读 DB，不修改状态）
# ==========================================
async def cleanup_loop():
    """定期巡检并清理过期文件（.wav / .opus / .txt）。

    失败任务的文件保留 FAILED_FILE_LIFETIME 供 debug，其余按 FILE_LIFETIME。
    """
    while True:
        try:
            with _db() as conn:
                failed_ids = {
                    row[0] for row in conn.execute(
                        "SELECT task_id FROM tasks WHERE status='failed'"
                    )
                }
            removed_files: list[str] = []
            now = time.time()
            for filename in os.listdir(AUDIO_DIR):
                stem, ext = os.path.splitext(filename)
                if ext in (".wav", ".opus", ".txt"):
                    filepath = os.path.join(AUDIO_DIR, filename)
                    if os.path.isfile(filepath):
                        lifetime = FAILED_FILE_LIFETIME if stem in failed_ids else FILE_LIFETIME
                        file_mtime = os.path.getmtime(filepath)
                        if now - file_mtime > lifetime:
                            os.remove(filepath)
                            removed_files.append(filepath)
            if removed_files:
                log_event("cleanup_expired_files", removed=removed_files, count=len(removed_files))
        except Exception as e:
            log_event("cleanup_error", error=str(e))

        await asyncio.sleep(60)


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
    cleanup_task = asyncio.create_task(cleanup_loop())
    snapshot_task = asyncio.create_task(snapshot_running_tasks_loop())
    yield
    cleanup_task.cancel()
    snapshot_task.cancel()
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


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT,
                result TEXT
            )
        """)
        conn.execute(
            "UPDATE tasks SET status='failed', result='server restarted' "
            "WHERE status IN ('pending', 'processing')"
        )
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
    """直接把原始 TTS WAV 编码为 Opus，不做任何音频裁剪或补静音。"""
    ffmpeg_cmd = [
        "ffmpeg", "-y", "-i", raw_wav_path,
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
