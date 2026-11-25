import pytest

from log_sink import format_event


def test_batch_task_ids_render_into_one_line():
    # 怕的是：146 个 id 逐行渲染出 444 行、约 12KB，写满日志管道并反压业务线程。
    task_ids = [f"ch01-seg{i:03d}-MOSS-voice" for i in range(146)]

    lines = format_event("batch_status_lookup", {"count": 146, "task_ids": task_ids}, "12:00:00")

    assert len(lines) <= 2
    joined = "\n".join(lines)
    assert all(task_id in joined for task_id in task_ids)


def test_scalar_fields_stay_on_the_head_line():
    lines = format_event(
        "task_submitted", {"task_id": "ch01-seg001", "body_bytes": 16813}, "12:00:00"
    )

    assert len(lines) == 1
    assert "task_id=ch01-seg001" in lines[0]
    assert "body_bytes=16813" in lines[0]


def test_event_without_payload_is_a_single_line():
    lines = format_event("heartbeat", {}, "12:00:00")

    assert len(lines) == 1
    assert "heartbeat" in lines[0]


def test_string_payload_is_not_treated_as_multi_value():
    # 字符串是可迭代的：若误判成列表，会被逐字符折行。
    lines = format_event("cleanup_error", {"error": "删除 x 失败"}, "12:00:00")

    assert len(lines) == 1
    assert "error=删除 x 失败" in lines[0]


def test_task_id_string_is_not_split_into_characters():
    lines = format_event("task_start", {"task_id": "ch01-seg001"}, "12:00:00")

    assert "task_id=ch01-seg001" in lines[0]
