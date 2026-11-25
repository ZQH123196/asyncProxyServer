"""启动期环境变量与运行前置检查。

约定：环境变量取值不合法、依赖的二进制缺失、产物目录不可写这类"迟早会炸"的问题，
一律在服务启动时一次性报完并拒绝启动，禁止拖到某个 TTS 任务中途才暴露。
校验失败抛 StartupConfigError（携带全部问题项），由 main.py 打印后退出。
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# 必须先于任何 os.environ 读取执行
load_dotenv()

ENV_FILE_HINT = ".env（与 main.py 同目录，可手工创建）"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 6006
DEFAULT_SGLANG_URL = "http://localhost:8000/v1/audio/speech"
DEFAULT_AUDIO_DIR = "static_audio"
DEFAULT_DB_FILE = "tts_tasks.db"

# opus 转码由外部 ffmpeg 的 libopus 编码器承担（不在 PyPI 依赖范畴）
REQUIRED_FFMPEG_ENCODER = "libopus"


class StartupConfigError(RuntimeError):
    """启动配置检查未通过。problems 为可直接落日志的逐行描述。"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("；".join(problems))


@dataclass(frozen=True)
class ServerEnv:
    host: str
    port: int
    sglang_url: str
    audio_dir: str
    db_file: str

    def describe(self) -> list[str]:
        return [
            f"监听={self.host}:{self.port}",
            f"SGLANG_URL={self.sglang_url}",
            f"AUDIO_DIR={self.audio_dir}",
            f"DB_FILE={self.db_file}",
        ]


def _read(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _check_port(raw: str, problems: list[str]) -> int:
    try:
        port = int(raw)
    except ValueError:
        problems.append(f"PORT={raw!r} 不是整数")
        return 0
    if not 1 <= port <= 65535:
        problems.append(f"PORT={port} 不在 1–65535 范围内")
    return port


def _check_sglang_url(value: str, problems: list[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        problems.append(f"SGLANG_URL={value!r} 不是完整的 http(s) 地址")


def _check_writable_dir(directory: str, name: str, problems: list[str]) -> None:
    path = Path(directory).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        problems.append(f"{name}={directory} 无法创建：{e}")
        return
    if not os.access(path, os.W_OK):
        problems.append(f"{name}={directory} 不可写（音频产物与任务库都要落在这里）")


def _check_ffmpeg(problems: list[str]) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        problems.append("PATH 中找不到 ffmpeg 可执行文件，无法编码 opus 产物")
        return
    try:
        probe = subprocess.run(
            [binary, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        problems.append(f"探测 ffmpeg 编码器失败：{e}")
        return
    if REQUIRED_FFMPEG_ENCODER not in probe.stdout:
        problems.append(f"{binary} 未编译 {REQUIRED_FFMPEG_ENCODER} 编码器，无法输出 opus 产物")


def load_server_env() -> ServerEnv:
    """校验启动配置与运行前置依赖，不通过抛 StartupConfigError。"""
    problems: list[str] = []

    host = _read("HOST", DEFAULT_HOST)
    port = _check_port(_read("PORT", str(DEFAULT_PORT)), problems)
    sglang_url = _read("SGLANG_URL", DEFAULT_SGLANG_URL)
    audio_dir = _read("AUDIO_DIR", DEFAULT_AUDIO_DIR)
    db_file = _read("DB_FILE", DEFAULT_DB_FILE)

    if not host:
        problems.append(f"HOST 为空，请检查 {ENV_FILE_HINT}")
    _check_sglang_url(sglang_url, problems)
    _check_writable_dir(audio_dir, "AUDIO_DIR", problems)
    _check_writable_dir(str(Path(db_file).expanduser().parent), "DB_FILE", problems)
    _check_ffmpeg(problems)

    if problems:
        raise StartupConfigError(problems)

    return ServerEnv(
        host=host,
        port=port,
        sglang_url=sglang_url,
        audio_dir=audio_dir,
        db_file=db_file,
    )
