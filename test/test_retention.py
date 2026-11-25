import unittest

from retention import FAILED_RETENTION_SECONDS, RetentionPolicy, is_expired


def _policy(**overrides) -> RetentionPolicy:
    """默认策略 = 80% 起压，90% 到 5 分钟，95% 强制回收，30 分钟封顶。"""
    values = {
        "start": 0.80,
        "limit": 0.90,
        "critical": 0.95,
        "floor_seconds": 300,
        "max_seconds": 1800,
    }
    values.update(overrides)
    return RetentionPolicy(**values)


class SuccessRetentionTests(unittest.TestCase):
    """成功产物的保留时间：80% 以下不删，80–90% 线性压缩，90% 以上钉 5 分钟。"""

    def test_usage_at_or_below_start_never_expires(self):
        # 磁盘有余量时产物一直留着，这是策略的核心：磁盘当缓存用。
        self.assertIsNone(_policy().success_retention_seconds(0.0))
        self.assertIsNone(_policy().success_retention_seconds(0.80))

    def test_usage_at_or_above_limit_uses_floor(self):
        self.assertEqual(_policy().success_retention_seconds(0.90), 300.0)
        self.assertEqual(_policy().success_retention_seconds(0.97), 300.0)

    def test_midpoint_is_linear_between_max_and_floor(self):
        # 85% 正好一半压力：1800 - 0.5 × (1800 - 300) = 1050 秒。
        # 浮点减法在 0.85-0.80 上有尾差，断言到毫秒即可。
        self.assertAlmostEqual(_policy().success_retention_seconds(0.85), 1050.0, places=3)

    def test_custom_thresholds_change_the_curve(self):
        # 五个参数都是可配的：换一组值，曲线必须跟着换，不能写死默认值。
        custom = _policy(start=0.5, limit=0.7, floor_seconds=60, max_seconds=600)

        self.assertIsNone(custom.success_retention_seconds(0.5))
        self.assertAlmostEqual(custom.success_retention_seconds(0.6), 330.0, places=3)
        self.assertEqual(custom.success_retention_seconds(0.7), 60.0)


class ExpiryTests(unittest.TestCase):
    """过期判定：失败产物固定 3 天，成功产物按当前用量对应的保留时间。"""

    def test_success_artifact_never_expires_when_retention_is_none(self):
        self.assertFalse(is_expired(False, mtime=0, now=10**9, success_retention=None))

    def test_success_artifact_expires_right_after_its_retention(self):
        self.assertFalse(is_expired(False, mtime=0, now=300, success_retention=300))
        self.assertTrue(is_expired(False, mtime=0, now=301, success_retention=300))

    def test_failed_artifact_keeps_three_days_even_under_pressure(self):
        # 失败产物是 debug 材料：80–90% 的压缩不作用于它，只有强制回收才动它。
        self.assertFalse(
            is_expired(True, mtime=0, now=FAILED_RETENTION_SECONDS, success_retention=300)
        )
        self.assertTrue(
            is_expired(True, mtime=0, now=FAILED_RETENTION_SECONDS + 1, success_retention=300)
        )


class ReclaimTargetTests(unittest.TestCase):
    """超限回收目标：只有越过 critical 才需要释放空间，且只回收到 limit。"""

    def test_below_critical_needs_no_reclaim(self):
        self.assertEqual(_policy().reclaim_target_bytes(total=1000, used=950), 0)

    def test_above_critical_frees_down_to_limit(self):
        self.assertEqual(_policy().reclaim_target_bytes(total=1000, used=960), 60)


class PolicyValidationTests(unittest.TestCase):
    """非法阈值必须在启动期一次报完，且点名对应的环境变量。"""

    def test_default_policy_is_valid(self):
        self.assertEqual(_policy().validation_problems(), [])

    def test_start_must_be_below_limit(self):
        problems = _policy(start=0.92).validation_problems()
        self.assertTrue(any("DISK_USAGE_START" in p for p in problems))

    def test_critical_must_be_above_limit(self):
        problems = _policy(critical=0.90).validation_problems()
        self.assertTrue(any("DISK_USAGE_CRITICAL" in p for p in problems))

    def test_ratios_must_stay_inside_the_unit_interval(self):
        problems = _policy(start=0.0).validation_problems()
        self.assertTrue(any("DISK_USAGE_START" in p for p in problems))

    def test_out_of_range_ratio_does_not_also_report_ordering_noise(self):
        # 1.5 已经不在区间内，再报「必须小于 limit」只是噪音。
        problems = _policy(start=1.5).validation_problems()
        self.assertEqual(len(problems), 1)

    def test_floor_must_be_positive(self):
        problems = _policy(floor_seconds=0).validation_problems()
        self.assertTrue(any("RETENTION_FLOOR_SECONDS" in p for p in problems))

    def test_max_must_not_be_below_floor(self):
        problems = _policy(max_seconds=60, floor_seconds=300).validation_problems()
        self.assertTrue(any("RETENTION_MAX_SECONDS" in p for p in problems))
