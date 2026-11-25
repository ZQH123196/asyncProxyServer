"""`.req.tmp` 孤儿文件回收。

怕的是：进程在「写临时文件」与「改名」之间被 kill，留下半截 `.req.tmp`。
它的扩展名不在清理白名单里，不主动扫就永久堆积。
判据用修改时间，不用进程号：Windows 上 `os.kill(pid, 0)` 会真的杀进程。
"""

import os
import time


def _touch(directory, name, content=b"x", age_seconds=0.0):
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(content)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def test_persist_writes_final_file_and_leaves_no_temp(isolated_main):
    main = isolated_main

    main._persist_request_body("t1", b'{"input":"hi"}')

    assert os.listdir(main.AUDIO_DIR) == ["t1.req"]
    assert main._load_request_body("t1") == b'{"input":"hi"}'


def test_stale_temp_is_removed(isolated_main):
    main = isolated_main
    os.makedirs(main.AUDIO_DIR, exist_ok=True)
    stale = _touch(main.AUDIO_DIR, "old.req.tmp.123.45", age_seconds=main.STALE_TEMP_SECONDS + 60)
    _touch(main.AUDIO_DIR, "keep.req", b"final")

    removed = main._sweep_stale_request_body_temps()

    assert removed == 1
    assert not os.path.exists(stale)
    assert os.path.exists(os.path.join(main.AUDIO_DIR, "keep.req"))


def test_fresh_temp_being_written_right_now_is_kept(isolated_main):
    # 本次启动之后才写的临时文件属于正在进行的提交，不能删。
    main = isolated_main
    os.makedirs(main.AUDIO_DIR, exist_ok=True)
    fresh = _touch(main.AUDIO_DIR, "live.req.tmp.1.2")

    assert main._sweep_stale_request_body_temps() == 0
    assert os.path.exists(fresh)


def test_sweep_is_safe_when_audio_dir_missing(isolated_main, monkeypatch):
    main = isolated_main
    monkeypatch.setattr(main, "AUDIO_DIR", os.path.join(main.AUDIO_DIR, "does-not-exist"))

    assert main._sweep_stale_request_body_temps() == 0


def test_sweep_never_kills_processes(isolated_main, monkeypatch):
    # 回归：上一版用 os.kill(pid, 0) 探活，Windows 上会 TerminateProcess 杀掉无关进程。
    main = isolated_main
    os.makedirs(main.AUDIO_DIR, exist_ok=True)
    _touch(main.AUDIO_DIR, "x.req.tmp.1.2", age_seconds=main.STALE_TEMP_SECONDS + 60)

    def forbidden(*args, **kwargs):
        raise AssertionError("回收临时文件不允许向任何进程发信号")

    monkeypatch.setattr(main.os, "kill", forbidden)

    assert main._sweep_stale_request_body_temps() == 1


def test_failed_persist_leaves_no_temp_behind(isolated_main, monkeypatch):
    main = isolated_main
    os.makedirs(main.AUDIO_DIR, exist_ok=True)

    def boom(src, dst):
        raise OSError("盘满了")

    monkeypatch.setattr(main.os, "replace", boom)

    try:
        main._persist_request_body("t2", b"{}")
    except OSError:
        pass

    assert os.listdir(main.AUDIO_DIR) == []
