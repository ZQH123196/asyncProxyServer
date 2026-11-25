import sqlite3
import time
import os
import requests
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles

AUDIO_DIR = "static_audio"
DB_FILE = "tts_tasks.db"
FILE_LIFETIME = 10 * 60  # 音频存活时间：10分钟

os.makedirs(AUDIO_DIR, exist_ok=True)

# ==========================================
# 仅清理文件的后台守护任务 (绝对不碰数据库)
# ==========================================
async def cleanup_loop():
    """定期巡检并仅清理过期音频文件"""
    while True:
        try:
            now = time.time()
            for filename in os.listdir(AUDIO_DIR):
                if filename.endswith(".wav"):
                    filepath = os.path.join(AUDIO_DIR, filename)
                    if os.path.isfile(filepath):
                        file_mtime = os.path.getmtime(filepath)
                        # 仅删除超过10分钟的文件
                        if now - file_mtime > FILE_LIFETIME:
                            os.remove(filepath)
                            print(f"[清理机制] 已释放磁盘空间，删除过期文件: {filepath}")
        except Exception as e:
            print(f"[清理机制] 任务异常: {e}")
        
        # 每隔 1 分钟执行一次扫描
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(cleanup_loop())
    yield
    cleanup_task.cancel()
# ==========================================

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT,
                result TEXT
            )
        """)
init_db()

app = FastAPI(title="TTS 异步代理服务", lifespan=lifespan)
app.mount("/download", StaticFiles(directory=AUDIO_DIR), name="audio")


@app.get("/v1/audio/async/status/{task_id}")
def check_tts_status(task_id: str):
    """
    提供给客户端轮询查询状态的接口
    """
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("SELECT status, result FROM tasks WHERE task_id=?", (task_id,))
        row = cursor.fetchone()
    
    # 恢复你原本的纯粹逻辑
    if not row:
        return {"task_id": task_id, "status": "failed", "result": "Task not found"}
        
    status, result = row
    return {"task_id": task_id, "status": status, "result": result}

    
def hard_tts_work(task_id: str, raw_body: bytes):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE tasks SET status='processing' WHERE task_id=?", (task_id,))
    
    try:
        sglang_url = "http://localhost:8000/v1/audio/speech"
        
        response = requests.post(
            sglang_url, 
            data=raw_body, 
            headers={"Content-Type": "application/json"},
            timeout=3600
        )
        response.raise_for_status()
        
        file_name = f"{task_id}.wav"
        file_path = os.path.join(AUDIO_DIR, file_name)
        with open(file_path, "wb") as f:
            f.write(response.content)
            
        download_url = f"http://127.0.0.1:6006/download/{file_name}"
        
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("UPDATE tasks SET status='success', result=? WHERE task_id=?", (download_url, task_id))
            
    except Exception as e:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("UPDATE tasks SET status='failed', result=? WHERE task_id=?", (str(e), task_id))

@app.post("/v1/audio/async/speech")
async def proxy_start_tts(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body() 
    task_id = f"task_{int(time.time() * 1000)}" 
    
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO tasks VALUES (?, 'pending', NULL)", (task_id,))
    
    background_tasks.add_task(hard_tts_work, task_id, raw_body)
    return {"task_id": task_id, "status": "pending"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=6006)