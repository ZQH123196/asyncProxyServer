# 异步 TTS 代理

把同步的长耗时 TTS 请求拆成「提交 → 轮询 → 下载」三步，避免上游超时。

- 提交：`POST /v1/audio/async/speech?task_id=xxx`，立即返回 `pending`，任务进后台线程池。
- 轮询：`GET /v1/audio/async/status/{task_id}`，或批量 `POST /v1/audio/async/status/batch`。
- 下载：成功后 `result` 是 `/download/<task_id>.opus`。

上游是 SGLang-Omni 的 OpenAI 兼容语音端点。产物经 ffmpeg 的 libopus 编码成 opus，采样率固定 48 kHz（`OPUS_SAMPLE_RATE`）。

## 安装与启动（生产唯一入口）

**本项目只有两个脚本会被生产运行：`start_asyncProxyServer.sh` 与它内部调用的 `uv sync`。
人工手工装环境、手工启动进程，都不是受支持的部署方式。**

| 场景 | 唯一命令 | 说明 |
|---|---|---|
| 交付到生产 | `./start_asyncProxyServer.sh` | 安装依赖 + 拉起 pm2 托管进程，一次完成 |
| 生产重启 | `./start_asyncProxyServer.sh` | 幂等，可反复执行 |

`start_asyncProxyServer.sh` 按顺序做四件事：

1. `uv sync` —— 按 `pyproject.toml` 装依赖，必要时先把 `uv.lock` 对齐。这**就是**本项目的安装步骤，没有独立的安装脚本。
2. 装 `pm2` 与 `pm2-logrotate`（已存在则跳过）。
3. `pm2 delete asyncProxyServer`，再 `pm2 start`。重复执行不会起出两个进程。
4. 挂 `pm2 logs` 前台看日志。

### 生产环境禁止的做法

以下做法会绕开锁文件或绕开 pm2，产生的环境与生产实际运行的环境不一致：

- 禁止 `uv pip install <包名>`。它绕过 `uv.lock`，静默改版本；启动期依赖锁比对会拦住，但那时已经装脏了。
- 禁止 `python main.py` / `source .venv/bin/activate && python main.py`。没有 pm2 托管，进程退出不会拉起，
  日志也不进 `~/.pm2/logs/`。
- 禁止 `pm2 start` 其他入口或加多余的 python 参数。启动脚本里的那条命令是唯一写法的来源。

### 依赖与运行期前置

装环境之外，服务自己还会在启动期校验下面这些项，任一不通过即拒绝启动并一次性报完：

- `ffmpeg` 存在且编译了 `libopus` 编码器（不在 PyPI 依赖范畴，需系统包管理器安装）。
- Python ≥ 3.11。
- `AUDIO_DIR` / `DB_FILE` 可创建且可写。
- 已装包版本与 `uv.lock` 一致。

上游 TTS 服务需另行部署，本服务只是它的异步代理层。脚本全流程无交互，可在 CI 或容器里重跑。

## 配置

复制 `.env.example` 为 `.env` 后修改。字段说明见该文件内的注释。

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `6006` | 监听地址 |
| `SGLANG_URL` | `http://localhost:8000/v1/audio/speech` | 上游端点，必须写完整 http(s) 地址 |
| `AUDIO_DIR` | `static_audio` | 产物目录，启动期校验存在且可写 |
| `DB_FILE` | `tts_tasks.db` | SQLite 任务库 |
| `DISK_USAGE_START` | `0.80` | 起压线：已用率不高于此值时，成功产物不按时间删 |
| `DISK_USAGE_LIMIT` | `0.90` | 保留时间降到下限的已用率 |
| `DISK_USAGE_CRITICAL` | `0.95` | 强制回收线：超过后删到回落到 `DISK_USAGE_LIMIT` |
| `RETENTION_FLOOR_SECONDS` | `300` | 保留时间下限（秒） |
| `RETENTION_MAX_SECONDS` | `1800` | 保留时间上限（秒），在 `DISK_USAGE_START` 处生效 |

已用率按小数写（`0.9`，不是 `90`）。五个值在启动期校验，非法即拒绝启动。

### 包源

`uv.lock` 里写死了包源（当前是**清华镜像**），同步时按它下载，所以启动脚本**没有** `PYPI_INDEX`
之类的开关——给了也不生效。境外部署需重新生成锁文件并提交：

```bash
uv lock --index-url https://pypi.org/simple
```

## 启动期检查

配置不合法或运行前置缺失时，**一次性报完所有问题**并拒绝启动，不会拖到某个 TTS 任务中途才炸：

- `PORT` 范围、`SGLANG_URL` 形态
- `AUDIO_DIR` / `DB_FILE` 所在目录可创建且可写
- ffmpeg 存在**且编译了 libopus 编码器**——否则首个任务才会失败
- Python 版本下限
- 已装包的版本与 `uv.lock` 一致（绕开锁装过包会在这里被抓住）

依赖锁检查是**单向**比对：只查已装包的版本是否与锁一致，不查「锁里有但没装」。后者是安装问题，`uv sync` 保证它；真漏了会在 import 时报出明确的 `ModuleNotFoundError`。

## 依赖版本纪律

**直接依赖一律写死 `==`，整棵依赖树由 `uv.lock` 钉死。**

启动脚本用 `uv sync`（不带 `--locked` / `--frozen`）。它会先把锁对齐到 `pyproject.toml`
再同步，所以装出来的版本永远等于 `pyproject.toml` 的声明。

| 写法 | 锁与 pyproject 不一致时 | 生产装到的版本 |
|---|---|---|
| `uv sync` | 把锁对齐后照常装 | 等于 pyproject 声明 ← 启动脚本用这个 |
| `uv sync --frozen` | 按旧锁装，退出码 0，不报错 | **与 pyproject 声明不符** |
| `uv sync --locked` | 直接失败 | 装不上，服务起不来 |

`--locked` 不可取：它把「改过 `pyproject.toml` 但没跑 `uv lock`」这种仓库内疏忽
罚在生产部署上，服务直接起不来。生产该做的是把服务装对、跑起来。

`--frozen` 不可取：实测 `pyproject.toml` 写 `requests==2.34.1`、锁里是 `2.34.2` 时，
它装出 `2.34.2` 且退出码 0，正好是「跑错版本还不吭声」。

裸跑 `uv pip install <包名>` 会绕过锁、静默改版本。

升级依赖在**开发机**上做（不要在生产机上改依赖）：

```bash
# 1. 改 pyproject.toml 的版本号
uv lock --index-url https://pypi.tuna.tsinghua.edu.cn/simple
# 2. 装到本地并跑测试
uv sync --frozen
uv run pytest -q
# 3. 提交 pyproject.toml 与 uv.lock 两个文件
```

第 3 步不能省。虽然生产 `uv sync` 会自己把锁对齐，但那样跑出的版本对不上仓库里的锁，
下次谁也说不清生产到底装了什么。锁与 `pyproject.toml` 一起提交，仓库才是唯一事实源。

### Python 下限是 3.11 而非 3.10

启动期要解析 `uv.lock`，而读 TOML 的标准库 `tomllib` 自 3.11 才有。声明 3.10 会出现「声明支持、实际起不来」。

## 任务状态与清理

| 状态 | 含义 |
|---|---|
| `pending` | 已入队，等线程 |
| `processing` | 正在生成 |
| `success` | 完成，`result` 是 opus 下载路径 |
| `failed` | 失败，`result` 是原因 |

进程重启时，残留的 `pending` / `processing` 任务按**上游可达性**裁决：

1. 启动时探测 `SGLANG_URL`（2 秒超时）。只有连不上（连接被拒 / DNS 失败 / 超时）判不可达；
   收到任何 HTTP 响应（含 404 / 405 / 503）都算可达——上游加载模型期间回 503 是常态。
2. 上游可达：恢复为 `pending` 并重新派发。调用方仍在等，判死会让它整批重提，形成启动风暴。
3. 上游不可达：标 `failed`（`result=server restarted`）。整条链路都不在，重派只会立刻再失败。

重新派发需要原始请求体，所以提交时先把它落盘到 `AUDIO_DIR/<task_id>.req`。
没有 `.req` 的遗留任务（旧版本提交的、或已被清理）判 `failed`（`result=server restarted (request body missing)`），
不会永远停在 `pending`。

`.req` 与 `.txt` / `.wav` / `.opus` 一样受清理策略管辖。
写入走 `<task_id>.req.tmp.<pid>.<tid>` 再改名；进程被 kill 留下的半截临时文件在下次启动时清掉。
判据是修改时间超过 `STALE_TEMP_SECONDS`（1 小时），不按进程号探活——
Windows 上 `os.kill(pid, 0)` 会真的终止进程，pid 复用时可能杀到无关进程。

清理策略由磁盘用量驱动，每 60 秒巡检一次。用量取 `AUDIO_DIR` 所在文件系统的**整体**已用率。

| 已用率 | 成功产物保留 | 失败产物保留 |
|---|---|---|
| ≤ 80% | 不按时间删，一直留着 | 3 天 |
| 80%–90% | 从 30 分钟线性降到 5 分钟 | 3 天 |
| > 90% | 5 分钟 | 3 天 |
| > 95% | 强制回收，删到回落到 90% | 同样被强制回收，且先于成功产物被删 |

两条硬约束：

1. `pending` / `processing` 任务的文件永不删。TTS 单次超时上限是 3600 秒，这些文件可能正在写。
2. 强制回收无视保留时间，按「失败优先、同类最旧优先」删。删不够目标量时记 `shortfall_bytes`，不假装成功。

80% 是硬起点：越过它，超过 30 分钟的产物立即进入删除候选。否则用量会停在 80% 上方不回落。

## 同一 task_id 的执行互斥

同一 `task_id` 同时只允许一个执行体。提交接口与重启重派是两条独立进入路径，都在写
`<task_id>.wav` / `.opus`，并发重入会让产物互相覆盖。

执行中再次提交同一 `task_id` 时，最新请求体被记为待重跑，由当前执行体跑完后接着跑一轮
（日志 `task_queued_after_running` / `task_rerun`）。不丢弃后到者：提交接口已把库里的行重置为
`pending`，丢弃会让它永远停在 `pending`。

接着跑的轮数上限 `MAX_RERUN_ROUNDS`（3 轮）。超出后释放并记 `task_rerun_capped`，
调用方再提交即重新排队——避免调用方持续重试时长期占住线程与编码信号量。

执行体抛异常时无条件释放占位并清掉待重跑意图（日志由任务自身记 `task_failed`）。
不释放会让该 `task_id` 此后永远被当作「已在跑」，再也没人执行。

## 日志

日志经有界队列交给单个后台线程批量写 stdout（pm2 落到 `~/.pm2/logs/asyncProxyServer-out.log`）。

判据：

1. 业务线程永不阻塞在写日志上。下游写得慢只让队列积压。
2. 队列上限 10000 行。满了丢弃并计数，恢复后补一行 `[log-sink] 队列满，丢弃 N 行日志`。
3. 多值字段（`task_ids` 等）折成一行用逗号拼接，不再每项一行。一次批量查询带 146 个 id 时，
   行数从 444 行降到 2 行，id 一个不丢。
4. 退出时排空队列再关流，尾部日志不丢。

颜色输出已移除：进程由 pm2 托管、无 TTY，着色分支从来不会生效。

## 目录结构

```
asyncProxyServer/
├── main.py                    FastAPI 入口与启动期检查
├── env_config.py              配置与依赖锁检查
├── log_sink.py                日志折行渲染 + 有界队列批量落盘
├── restart_recovery.py        重启残留任务的裁决（纯函数 + 一条 UPDATE）
├── retention.py               磁盘用量 → 保留时间映射（纯函数）
├── tts_quality.py             时长挤碎 / 拉伸检测
├── conftest.py                让 test/ 下的测试能导入项目根模块
├── start_asyncProxyServer.sh  生产唯一入口：装依赖 + pm2 启动
├── pyproject.toml             依赖声明（钉版本）
├── uv.lock                    整棵依赖树的精确版本
├── .env.example               配置模板
└── test/
    ├── test_env_config.py     单测
    ├── test_log_format.py
    ├── test_log_sink.py
    ├── test_request_body_sweep.py
    ├── test_restart_dispatch.py
    ├── test_restart_recovery.py
    ├── test_retention.py      单测
    ├── test_stuck_pending.py
    ├── test_task_mutex.py
    ├── test_tts_quality.py
    ├── test_upstream_probe.py
    ├── __init__.py
    ├── testAsync.sh           端到端冒烟（需服务与上游均已起）
    └── testAsync6k.sh
```

源码平铺是 Python 惯例：模块间是平铺 import（`from tts_quality import ...`），建 `src/` 会破坏它。测试则收进 `test/`，靠根目录的 `conftest.py` 把项目根补回 `sys.path`——否则直接跑 `pytest` 会 `ModuleNotFoundError`，只有 `python -m pytest` 能通（它顺手把 cwd 塞进了 `sys.path`，属于掩盖问题）。

## 测试

```bash
uv run pytest -q
python -m unittest discover -s test -t . -p "test_*.py"
```

`test/test_env_config.py` 里的 `ShippedLockfileTests` 真读一次 `uv.lock` 与 `pyproject.toml`，守住「锁文件本身自洽」和「直接依赖都钉了版本」。
