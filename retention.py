"""产物保留策略：把磁盘用量归一化成保留时间。

判据（本项目定案）：

1. 用量 ≤ `start`：成功产物不按时间删。磁盘当缓存用。
2. `start` < 用量 ≤ `limit`：保留时间从 `max_seconds` 线性降到 `floor_seconds`。
3. 用量 > `limit`：保留 `floor_seconds`。
4. 用量 > `critical`：强制回收。按「失败优先、类内最旧优先」删到用量回到 `limit`，
   此时不看保留时间。
5. 失败产物常规保留 `FAILED_RETENTION_SECONDS`（3 天，debug 用）；强制回收时先于成功产物被删。

`start` 是硬起点：一旦越过，超过 `max_seconds` 的产物立即进入删除候选。
这是把用量拉回 `start` 的必要条件——否则用量停在 start 上方永不回落。

映射是可测的纯函数；磁盘探测与文件删除在调用方（main.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 失败任务的音频/文本保留：3 天（debug 用）。容量压力下优先回收，不受此值保护。
FAILED_RETENTION_SECONDS = 3 * 24 * 60 * 60

# 配置项名与字段一一对应；校验信息必须点名环境变量，否则运维不知该改哪一项。
_ENV_NAMES = {
    "start": "DISK_USAGE_START",
    "limit": "DISK_USAGE_LIMIT",
    "critical": "DISK_USAGE_CRITICAL",
    "floor_seconds": "RETENTION_FLOOR_SECONDS",
    "max_seconds": "RETENTION_MAX_SECONDS",
}


@dataclass(frozen=True)
class RetentionPolicy:
    """保留策略的取值。

    构造时不校验，由 `validation_problems()` 统一报告，调用方一次报完所有问题。
    """

    start: float
    limit: float
    critical: float
    floor_seconds: int
    max_seconds: int

    def validation_problems(self) -> list[str]:
        """返回全部非法项，供启动期一次性打印。合法时返回空列表。"""
        problems: list[str] = []
        in_range: list[bool] = []
        for field in ("start", "limit", "critical"):
            value = getattr(self, field)
            in_range.append(0 < value < 1)
            if not in_range[-1]:
                problems.append(
                    f"{_ENV_NAMES[field]}={value:g} 不在 (0,1) 区间"
                    "（按已用率的小数表示，例如 0.9）"
                )
        # 区间外的值再报顺序错误只是噪音，用户改完区间自然消失。
        if all(in_range):
            if self.start >= self.limit:
                problems.append(
                    f"{_ENV_NAMES['start']}={self.start:g} 必须小于 "
                    f"{_ENV_NAMES['limit']}={self.limit:g}"
                )
            if self.limit >= self.critical:
                problems.append(
                    f"{_ENV_NAMES['critical']}={self.critical:g} 必须大于 "
                    f"{_ENV_NAMES['limit']}={self.limit:g}"
                )
        if self.floor_seconds <= 0:
            problems.append(f"{_ENV_NAMES['floor_seconds']}={self.floor_seconds} 必须为正整数秒")
        elif self.max_seconds < self.floor_seconds:
            problems.append(
                f"{_ENV_NAMES['max_seconds']}={self.max_seconds} 不能小于 "
                f"{_ENV_NAMES['floor_seconds']}={self.floor_seconds}"
            )
        return problems

    def success_retention_seconds(self, usage: float) -> float | None:
        """成功产物在给定用量下保留多少秒。None 表示不按时间删。"""
        if usage <= self.start:
            return None
        if usage >= self.limit:
            return float(self.floor_seconds)
        pressure = (usage - self.start) / (self.limit - self.start)
        return self.max_seconds - pressure * (self.max_seconds - self.floor_seconds)

    def reclaim_target_bytes(self, total: int, used: int) -> int:
        """超过 `critical` 时要释放多少字节。未超过时返回 0。"""
        if total <= 0:
            return 0
        if used / total <= self.critical:
            return 0
        # 目标是把用量降回 limit。没必要一次压到 start。
        return max(0, used - int(total * self.limit))

    def describe(self) -> str:
        return (
            f"磁盘清理=用量≤{self.start:.0%} 不删成功产物；"
            f"{self.start:.0%}–{self.limit:.0%} 保留 {self.max_seconds}→{self.floor_seconds} 秒；"
            f">{self.limit:.0%} 保留 {self.floor_seconds} 秒；"
            f">{self.critical:.0%} 强制回收到 {self.limit:.0%}"
        )


def is_expired(
    failed: bool, mtime: float, now: float, success_retention: float | None
) -> bool:
    """产物是否已过保留期。

    失败产物固定按 FAILED_RETENTION_SECONDS；成功产物按当前用量对应的保留时间，
    None 表示不按时间删。
    """
    lifetime = FAILED_RETENTION_SECONDS if failed else success_retention
    if lifetime is None:
        return False
    return now - mtime > lifetime
