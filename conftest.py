"""让测试文件能用 `from env_config import ...` 直接导入项目根下的模块。

业务代码是平铺布局（模块间互为平铺 import），没有 src/ 目录。测试收进 test/
后，pytest 只会把 test/ 放进 sys.path，项目根不在其中，直接跑 `pytest` 会报
ModuleNotFoundError；只有 `python -m pytest` 能跑通——那是因为它额外把当前
工作目录塞进了 sys.path，属于掩盖问题而非解决问题。此处显式补上项目根，
使 `pytest`、`uv run pytest`、以及从任意目录指定路径运行都一致。
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


_MAIN_TMP: tempfile.TemporaryDirectory | None = None


def _shared_main_tmp() -> str:
    """整个测试会话共用一套目录。

    main 是模块级单例：AUDIO_DIR / DB_FILE 在 import 期就定死了，
    换成每个用例一个新目录会让第二次 import 之后仍指向已删的旧路径。
    用例之间的隔离靠「每个用例清空 tasks 表 + 清空重派队列」。
    """
    global _MAIN_TMP
    if _MAIN_TMP is None:
        _MAIN_TMP = tempfile.TemporaryDirectory()
    return _MAIN_TMP.name


@pytest.fixture()
def isolated_main(monkeypatch):
    """在临时目录里导入 main，并绕开「依赖锁 / ffmpeg」这类与用例无关的启动检查。

    main 在 import 期就跑启动检查、建库、探上游。不隔离的话，import 会在
    cwd 下建出 tts_tasks.db，且宿主机没装全依赖时整组用例直接崩。
    """
    root = _shared_main_tmp()
    monkeypatch.setenv("AUDIO_DIR", os.path.join(root, "audio"))
    monkeypatch.setenv("DB_FILE", os.path.join(root, "tasks.db"))
    monkeypatch.setenv("SGLANG_URL", "http://127.0.0.1:1/v1/audio/speech")
    for name in ("HOST", "PORT", "DISK_USAGE_START", "DISK_USAGE_LIMIT", "DISK_USAGE_CRITICAL",
                 "RETENTION_FLOOR_SECONDS", "RETENTION_MAX_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    import env_config

    monkeypatch.setattr(env_config, "check_lockfile", lambda problems: None)
    # ffmpeg 是否存在与本组用例的被测对象无关：直接当它存在且带 libopus。
    monkeypatch.setattr(env_config.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        env_config.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" A..... libopus    libopus Opus", stderr=""
        ),
    )

    import main

    yield main

    main._REDISPATCH_QUEUE.clear()
    _wipe_tasks(main)
    _wipe_audio(main)


def _wipe_tasks(main):
    with main._db() as conn:
        conn.execute("DELETE FROM tasks")
        conn.commit()


def _wipe_audio(main):
    # AUDIO_DIR 可能被某个用例 monkeypatch 到不存在的路径，收尾以真实目录为准。
    audio_dir = os.path.join(_shared_main_tmp(), "audio")
    if not os.path.isdir(audio_dir):
        return
    for name in os.listdir(audio_dir):
        os.remove(os.path.join(audio_dir, name))
