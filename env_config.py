"""启动期环境变量、依赖锁与运行前置检查。

约定：环境变量取值不合法、已装依赖与 uv.lock 不一致、依赖的二进制缺失、
产物目录不可写这类「迟早会炸」的问题，一律在服务启动时一次性报完并拒绝启动，
禁止拖到某个 TTS 任务中途才暴露。校验失败抛 StartupConfigError
（携带全部问题项），由 main.py 打印后退出。
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from retention import RetentionPolicy

# 必须先于任何 os.environ 读取执行
load_dotenv()

ENV_FILE_HINT = ".env（与 main.py 同目录，可手工创建）"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 6006
DEFAULT_SGLANG_URL = "http://localhost:8000/v1/audio/speech"
DEFAULT_AUDIO_DIR = "static_audio"
DEFAULT_DB_FILE = "tts_tasks.db"

# 磁盘用量驱动的保留策略。取值含义见 retention.py 的模块说明。
DEFAULT_DISK_USAGE_START = 0.80
DEFAULT_DISK_USAGE_LIMIT = 0.90
DEFAULT_DISK_USAGE_CRITICAL = 0.95
DEFAULT_RETENTION_FLOOR_SECONDS = 5 * 60
DEFAULT_RETENTION_MAX_SECONDS = 30 * 60

# opus 转码由外部 ffmpeg 的 libopus 编码器承担（不在 PyPI 依赖范畴）
REQUIRED_FFMPEG_ENCODER = "libopus"

# pyproject 的 requires-python 下限。实际锁到 3.11 而非 3.10：启动期要解析 uv.lock，
# 而读 TOML 的标准库 tomllib 自 3.11 才有。写成 3.10 会出现「声明支持、实际起不来」。
REQUIRED_PYTHON = (3, 11)

LOCK_FILE = Path(__file__).resolve().parent / "uv.lock"


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
    retention: RetentionPolicy

    def describe(self) -> list[str]:
        return [
            f"监听={self.host}:{self.port}",
            f"SGLANG_URL={self.sglang_url}",
            f"AUDIO_DIR={self.audio_dir}",
            f"DB_FILE={self.db_file}",
            self.retention.describe(),
            f"Python={'.'.join(map(str, sys.version_info[:3]))}",
        ]


def _read(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _normalize(name: str) -> str:
    """包名归一化到 PEP 503 形态，使锁文件与 importlib.metadata 的写法可比。

    锁文件写的是 `Levenshtein`，已装元数据里可能是 `levenshtein`；下划线与连字符同理。
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _check_port(raw: str, problems: list[str]) -> int:
    try:
        port = int(raw)
    except ValueError:
        problems.append(f"PORT={raw!r} 不是整数")
        return 0
    if not 1 <= port <= 65535:
        problems.append(f"PORT={port} 不在 1–65535 范围内")
    return port


def _parse_ratio(name: str, raw: str, problems: list[str]) -> float | None:
    """解析已用率小数。取值范围由 RetentionPolicy.validation_problems 统一裁决。"""
    try:
        return float(raw)
    except ValueError:
        problems.append(f"{name}={raw!r} 不是数字（按已用率的小数表示，例如 0.9）")
        return None


def _parse_seconds(name: str, raw: str, problems: list[str]) -> int | None:
    try:
        return int(raw)
    except ValueError:
        problems.append(f"{name}={raw!r} 不是整数秒")
        return None


def _check_python(problems: list[str]) -> None:
    """解释器版本必须满足 pyproject 的 requires-python。

    不满足时依赖会挑到不匹配的 wheel，报错发生在安装或 import 期，与根因无关；
    在启动期拦住才能给出「换解释器」这种可执行的结论。
    """
    current = sys.version_info[:2]
    if current < REQUIRED_PYTHON:
        problems.append(
            f"Python {'.'.join(map(str, current))} 低于本项目要求的 "
            f"{'.'.join(map(str, REQUIRED_PYTHON))}，请执行 autoInstall.sh 重建 .venv"
        )


def _check_writable_dir(directory: str, name: str, problems: list[str]) -> None:
    """校验产物目录可创建且可写。"""
    path = Path(directory).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        problems.append(f"{name}={directory} 无法创建：{e}")
        return
    if not os.access(path, os.W_OK):
        problems.append(f"{name}={directory} 不可写（音频产物与任务库都要落在这里）")


def _check_sglang_url(value: str, problems: list[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        problems.append(f"SGLANG_URL={value!r} 不是完整的 http(s) 地址")


def _check_db_file(db_file: str, problems: list[str]) -> None:
    """DB_FILE 必须能落成 SQLite 文件。

    父目录可创建且可写之外，还要挡「路径已被同名目录占据」——SQLite 到首个任务
    才会在那里失败，而任务失败的信息与「DB_FILE 配错了」毫无关联。
    """
    path = Path(db_file).expanduser()
    parent = path.parent if str(path.parent) else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        problems.append(f"DB_FILE={db_file} 所在目录无法创建：{e}")
        return
    if not os.access(parent, os.W_OK):
        problems.append(f"DB_FILE={db_file} 不可写（任务状态要落在这里）")
        return
    if path.is_dir():
        problems.append(f"DB_FILE={db_file} 指向的是一个已存在的目录，不是 SQLite 文件")


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


def check_lockfile(problems: list[str]) -> None:
    """比对已装版本与 uv.lock，抓「绕开锁装包」造成的版本漂移。

    比对方向是单向的：遍历**实际已装**的包，看它在锁里被钉成什么版本。
    反过来遍历锁的全集是错的——本锁里至少有两类本就不该装的包，报出来全是假警报：
    - 虚拟根包自身（`[tool.uv] package = false`，uv 不把它装进 venv）；
    - 平台 marker 排除的包（colorama 只被 win32 边引用）。

    这也意味着「锁里有但没装」不在本函数的职责内：那属于安装问题，
    autoInstall.sh 负责依赖安装，漏了会在 import 时报出明确的
    ModuleNotFoundError，指出缺哪个模块，比启动期笼统报「缺 N 个包」更好定位。
    """
    if not LOCK_FILE.is_file():
        problems.append(f"找不到依赖锁文件 {LOCK_FILE}，无从校验已装版本；请先跑 uv sync --frozen")
        return

    try:
        lock = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        problems.append(f"依赖锁文件 {LOCK_FILE} 解析失败：{exc}")
        return

    locked: dict[str, set[str]] = {}
    for package in lock.get("package", []):
        name = package.get("name")
        ver = package.get("version")
        if name and ver:
            locked.setdefault(_normalize(name), set()).add(ver)

    drifted: list[str] = []
    for dist in distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        candidates = locked.get(_normalize(name))
        if candidates is None:
            # 锁里没有的包（如手工装的 flash-attn）不在本检查范围内
            continue
        actual = dist.version
        if actual not in candidates and actual.split("+", 1)[0] not in {
            c.split("+", 1)[0] for c in candidates
        }:
            drifted.append(f"{name} 实际 {actual} ≠ 锁定的 {'/'.join(sorted(candidates))}")

    if drifted:
        problems.append(
            "已装依赖与 uv.lock 不一致，共 "
            f"{len(drifted)} 处：" + "；".join(drifted[:8])
            + ("；……其余见 uv sync --frozen" if len(drifted) > 8 else "")
            + "。请执行 `uv sync --frozen` 重建环境"
        )


def load_server_env() -> ServerEnv:
    """校验启动配置与运行前置依赖，不通过抛 StartupConfigError。

    只做配置层校验，保持零重依赖、可在单测里独立跑。
    依赖锁比对（check_lockfile）由 main.py 单独调用并汇总问题项，
    它读的是已装环境，放进来会让本函数在单测里依赖完整安装。
    """
    problems: list[str] = []

    host = _read("HOST", DEFAULT_HOST)
    port = _check_port(_read("PORT", str(DEFAULT_PORT)), problems)
    sglang_url = _read("SGLANG_URL", DEFAULT_SGLANG_URL)
    audio_dir = _read("AUDIO_DIR", DEFAULT_AUDIO_DIR)
    db_file = _read("DB_FILE", DEFAULT_DB_FILE)

    if not host:
        problems.append(f"HOST 为空，请检查 {ENV_FILE_HINT}")
    _check_python(problems)
    _check_sglang_url(sglang_url, problems)
    _check_writable_dir(audio_dir, "AUDIO_DIR", problems)
    _check_db_file(db_file, problems)
    _check_ffmpeg(problems)

    # 解析失败的那一项退回默认值继续校验：否则一个值写错会把其余值的语义检查全吞掉，
    # 用户要重启两次才能看全问题。
    start = _parse_ratio("DISK_USAGE_START", _read("DISK_USAGE_START", f"{DEFAULT_DISK_USAGE_START:g}"), problems)
    limit = _parse_ratio("DISK_USAGE_LIMIT", _read("DISK_USAGE_LIMIT", f"{DEFAULT_DISK_USAGE_LIMIT:g}"), problems)
    critical = _parse_ratio(
        "DISK_USAGE_CRITICAL",
        _read("DISK_USAGE_CRITICAL", f"{DEFAULT_DISK_USAGE_CRITICAL:g}"),
        problems,
    )
    floor_seconds = _parse_seconds(
        "RETENTION_FLOOR_SECONDS",
        _read("RETENTION_FLOOR_SECONDS", str(DEFAULT_RETENTION_FLOOR_SECONDS)),
        problems,
    )
    max_seconds = _parse_seconds(
        "RETENTION_MAX_SECONDS",
        _read("RETENTION_MAX_SECONDS", str(DEFAULT_RETENTION_MAX_SECONDS)),
        problems,
    )
    retention = RetentionPolicy(
        start=DEFAULT_DISK_USAGE_START if start is None else start,
        limit=DEFAULT_DISK_USAGE_LIMIT if limit is None else limit,
        critical=DEFAULT_DISK_USAGE_CRITICAL if critical is None else critical,
        floor_seconds=DEFAULT_RETENTION_FLOOR_SECONDS if floor_seconds is None else floor_seconds,
        max_seconds=DEFAULT_RETENTION_MAX_SECONDS if max_seconds is None else max_seconds,
    )
    problems.extend(retention.validation_problems())

    if problems:
        raise StartupConfigError(problems)

    return ServerEnv(
        host=host,
        port=port,
        sglang_url=sglang_url,
        audio_dir=audio_dir,
        db_file=db_file,
        retention=retention,
    )
