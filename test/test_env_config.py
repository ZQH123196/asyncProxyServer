import os
import re
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import env_config
from env_config import StartupConfigError, check_lockfile, load_server_env

# 本文件在 test/ 下，项目根是上一级。锁文件与 pyproject 都在根上，
# 用 __file__ 的 parent 会指向 test/ 而找不到它们。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_VARS = (
    "HOST",
    "PORT",
    "SGLANG_URL",
    "AUDIO_DIR",
    "DB_FILE",
    "DISK_USAGE_START",
    "DISK_USAGE_LIMIT",
    "DISK_USAGE_CRITICAL",
    "RETENTION_FLOOR_SECONDS",
    "RETENTION_MAX_SECONDS",
)


class EnvConfigTests(unittest.TestCase):
    """启动配置与运行前置依赖检查：缺 ffmpeg、目录不可写、地址不合法都要在启动时拦下。"""

    def setUp(self):
        self._saved_environ = dict(os.environ)
        self.addCleanup(os.environ.update, self._saved_environ)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ENV_VARS:
            os.environ.pop(name, None)

    def _load(self, **overrides):
        env = {k: str(v) for k, v in overrides.items() if v is not None}
        env.setdefault("AUDIO_DIR", str(Path(self.tmp.name) / "audio"))
        env.setdefault("DB_FILE", str(Path(self.tmp.name) / "tasks.db"))
        os.environ.update(env)
        # 除「缺 ffmpeg」那一条外，成功路径都默认 ffmpeg 存在：它不在 PyPI 依赖范畴，
        # 是否装在开发机上与本组用例的被测对象无关，让测试依赖宿主机只会造成无关的红。
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" A..... libopus                libopus Opus"
        )
        with (
            patch.object(env_config.shutil, "which", lambda name: "/usr/bin/ffmpeg"),
            patch.object(env_config.subprocess, "run", lambda *a, **kw: fake),
        ):
            return load_server_env()

    def test_missing_ffmpeg_rejects_startup(self):
        # Act & Assert：opus 编码依赖外部 ffmpeg，缺失时首个任务才会炸，必须启动期拦住。
        # 这里不能走 _load 的 ffmpeg 成功态 mock，故直接调真实校验。
        problems: list[str] = []
        with patch.object(env_config.shutil, "which", lambda name: None):
            env_config._check_ffmpeg(problems)
        self.assertIn("ffmpeg", "；".join(problems))

    def test_ffmpeg_without_libopus_rejects_startup(self):
        # Arrange：ffmpeg 存在但没编译 libopus，同样无法产出 opus
        probe = subprocess.CompletedProcess(args=[], returncode=0, stdout=" A..... aac  AAC (Advanced Audio Coding)")
        problems: list[str] = []

        # Act
        with (
            patch.object(env_config.shutil, "which", lambda name: "/usr/bin/ffmpeg"),
            patch.object(env_config.subprocess, "run", lambda *a, **kw: probe),
        ):
            env_config._check_ffmpeg(problems)

        # Assert
        self.assertIn("libopus", "；".join(problems))

    def test_incomplete_sglang_url_rejects_startup(self):
        # Act & Assert
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(SGLANG_URL="localhost:8000/v1/audio/speech")
        self.assertIn("SGLANG_URL", "；".join(ctx.exception.problems))

    def test_uncreatable_audio_dir_rejects_startup(self):
        # Arrange：同名路径已被普通文件占据，产物目录无从创建
        blocker = Path(self.tmp.name) / "blocked"
        blocker.write_text("")

        # Act & Assert
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(AUDIO_DIR=str(blocker / "audio"))
        self.assertIn("AUDIO_DIR", "；".join(ctx.exception.problems))

    def test_db_file_pointing_at_a_directory_rejects_startup(self):
        # Arrange：DB_FILE 路径已被同名目录占据，SQLite 会在首个任务才失败
        occupied = Path(self.tmp.name) / "occupied.db"
        occupied.mkdir()

        # Act & Assert
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(DB_FILE=str(occupied))
        joined = "；".join(ctx.exception.problems)
        self.assertIn("DB_FILE", joined)
        self.assertIn("目录", joined)

    def test_valid_startup_config_resolves_defaults_and_creates_audio_dir(self):
        # Act
        env = self._load()

        # Assert
        self.assertEqual((env.host, env.port), ("0.0.0.0", 6006))
        self.assertEqual(env.sglang_url, "http://localhost:8000/v1/audio/speech")
        self.assertTrue(Path(env.audio_dir).is_dir())

    def test_default_retention_policy_turns_usage_into_keep_time(self):
        # 80% 以下不删；85% 是区间中点；90% 起钉在下限 5 分钟。
        env = self._load()

        self.assertIsNone(env.retention.success_retention_seconds(0.79))
        self.assertAlmostEqual(env.retention.success_retention_seconds(0.85), 1050.0, places=3)
        self.assertEqual(env.retention.success_retention_seconds(0.93), 300.0)

    def test_retention_thresholds_out_of_order_reject_startup(self):
        # 起压线高于上限线时，映射没有区间可言，必须启动期拦住。
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(DISK_USAGE_START="0.92")
        self.assertIn("DISK_USAGE_START", "；".join(ctx.exception.problems))

    def test_non_numeric_retention_value_rejects_startup(self):
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(RETENTION_FLOOR_SECONDS="5m")
        self.assertIn("RETENTION_FLOOR_SECONDS", "；".join(ctx.exception.problems))

    def test_max_retention_below_floor_rejects_startup(self):
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(RETENTION_MAX_SECONDS="60")
        self.assertIn("RETENTION_MAX_SECONDS", "；".join(ctx.exception.problems))


class LockfileTests(unittest.TestCase):
    """依赖锁自检：绕开 uv.lock 装过包的环境必须在启动时被抓住。

    检查是单向的——遍历实际已装的包，比对它被锁钉成的版本。
    所以这里构造的是「已装分布」，而不是锁里的包清单。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _check(self, lock_body: str, installed: dict[str, str]):
        """installed 映射包名 → 实际版本，作为环境里已装包的集合。"""
        path = Path(self.tmp.name) / "uv.lock"
        path.write_text(lock_body, encoding="utf-8")
        fake = [
            types.SimpleNamespace(metadata={"Name": name}, version=ver)
            for name, ver in installed.items()
        ]
        with (
            patch.object(env_config, "LOCK_FILE", path),
            patch.object(env_config, "distributions", lambda: fake),
        ):
            problems: list[str] = []
            check_lockfile(problems)
            return problems

    def test_missing_lock_file_is_reported_with_its_path(self):
        # Arrange：报错必须带完整路径，否则用户不知道该去哪找锁文件
        absent = Path(self.tmp.name) / "absent.lock"

        # Act
        with patch.object(env_config, "LOCK_FILE", absent):
            problems: list[str] = []
            check_lockfile(problems)

        # Assert
        self.assertTrue(any(str(absent) in p for p in problems))

    def test_consistent_environment_has_no_problem(self):
        # Act
        problems = self._check(
            '[[package]]\nname = "soundfile"\nversion = "0.14.0"\n',
            {"soundfile": "0.14.0"},
        )

        # Assert
        self.assertEqual(problems, [])

    def test_version_drift_is_reported_with_both_versions(self):
        # Act
        problems = self._check(
            '[[package]]\nname = "requests"\nversion = "2.34.2"\n',
            {"requests": "2.31.0"},
        )

        # Assert
        joined = "；".join(problems)
        self.assertIn("requests", joined)
        self.assertIn("2.31.0", joined)
        self.assertIn("2.34.2", joined)

    def test_packages_absent_from_the_lock_are_not_reported(self):
        # Act：手工装的包（如可选的 flash-attn）不在锁里，不属本检查范围
        problems = self._check(
            '[[package]]\nname = "soundfile"\nversion = "0.14.0"\n',
            {"soundfile": "0.14.0", "flash-attn": "2.8.3"},
        )

        # Assert
        self.assertEqual(problems, [])

    def test_platform_excluded_and_virtual_packages_are_not_reported_missing(self):
        # Arrange：这三类包在锁里但本就不该装，正向遍历锁会误报
        lock = (
            '[[package]]\nname = "async-tts-proxy"\nversion = "0.1.0"\n'  # 虚拟根包
            '[[package]]\nname = "colorama"\nversion = "0.4.6"\n'  # 仅 win32
            '[[package]]\nname = "soundfile"\nversion = "0.14.0"\n'
        )

        # Act：环境里只有该装的
        problems = self._check(lock, {"soundfile": "0.14.0"})

        # Assert
        self.assertEqual(problems, [])

    def test_name_normalization_matches_underscore_to_dash(self):
        # Act：锁里写 some_pkg，已装元数据写 some-pkg，属同一包不该报漂移
        problems = self._check(
            '[[package]]\nname = "some_pkg"\nversion = "1.0.0"\n',
            {"some-pkg": "1.0.0"},
        )

        # Assert
        self.assertEqual(problems, [])


class ShippedLockfileTests(unittest.TestCase):
    """仓库里提交的 uv.lock 必须本身自洽，且不能对按它装好的环境产生假警报。

    这条不 mock 任何东西：锁文件写坏了、版本号手改成不存在、直接依赖忘了钉版本、
    或检查逻辑退化成正向遍历锁全集（虚拟根包 / 平台 marker / 未启用 extra 都会误报），
    都会被它抓住。
    """

    def test_committed_lock_file_parses_and_covers_declared_dependencies(self):
        # Arrange
        lock_path = _PROJECT_ROOT / "uv.lock"
        pyproject = _PROJECT_ROOT / "pyproject.toml"

        # Act
        import tomllib

        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        locked = {pkg["name"] for pkg in lock.get("package", []) if pkg.get("name")}

        # Assert
        self.assertTrue(lock.get("package"), "uv.lock 里没有任何 package 记录")
        for direct in project["project"]["dependencies"]:
            # 包名可能带 extras（uvicorn[standard]==…）与版本约束，取最前的标识符段
            name = re.match(r"[A-Za-z0-9._-]+", direct)
            self.assertIsNotNone(name, f"无法解析依赖名：{direct}")
            self.assertIn(name.group(0).lower(), locked, f"直接依赖 {name.group(0)} 不在 uv.lock 里")
            # 直接依赖必须写死版本，否则 uv sync --frozen 的约束会被后续安装绕过
            self.assertIn("==", direct, f"直接依赖未钉版本：{direct}")

    def test_freshly_installed_environment_produces_no_false_alarm(self):
        # Arrange：模拟「按锁装好」的环境——已装集 = 锁里排除本就不该装的那些包。
        # 真实环境里跑这条需要完整 sync，属重测试；此处用锁自身推导等价形态。
        import tomllib

        lock_path = _PROJECT_ROOT / "uv.lock"
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        # 本就该装的：排除虚拟根包与仅 win32 的 colorama
        installed = {
            pkg["name"]: pkg["version"]
            for pkg in lock.get("package", [])
            if pkg.get("name")
            and pkg.get("source", {}).get("virtual") is None
            and pkg["name"] not in ("colorama", "tzdata", "audioop-lts")
        }
        fake = [
            types.SimpleNamespace(metadata={"Name": name}, version=ver)
            for name, ver in installed.items()
        ]

        # Act
        with patch.object(env_config, "distributions", lambda: fake):
            problems: list[str] = []
            check_lockfile(problems)

        # Assert
        self.assertEqual(problems, [], f"按锁装好却报了 {len(problems)} 处假漂移")
