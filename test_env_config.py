import os
import tempfile
import unittest
from pathlib import Path

from env_config import StartupConfigError, load_server_env

ENV_VARS = ("HOST", "PORT", "SGLANG_URL", "AUDIO_DIR", "DB_FILE")


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
        return load_server_env()

    def test_missing_ffmpeg_rejects_startup(self):
        # Act & Assert：opus 编码依赖外部 ffmpeg，缺失时首个任务才会炸，必须启动期拦住
        with self.assertRaises(StartupConfigError) as ctx:
            self._load(PATH=str(Path(self.tmp.name) / "empty-bin"))
        self.assertIn("ffmpeg", "；".join(ctx.exception.problems))

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

    def test_valid_startup_config_resolves_defaults_and_creates_audio_dir(self):
        # Act
        env = self._load()

        # Assert
        self.assertEqual((env.host, env.port), ("0.0.0.0", 6006))
        self.assertEqual(env.sglang_url, "http://localhost:8000/v1/audio/speech")
        self.assertTrue(Path(env.audio_dir).is_dir())
