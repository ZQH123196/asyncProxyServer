"""上游可达性探测：判据是「上游进程活着」，不是「现在能出音频」。"""

import pytest
import requests


@pytest.mark.parametrize("status_code", [200, 400, 404, 405, 500, 503])
def test_any_http_response_means_reachable(isolated_main, monkeypatch, status_code):
    # 怕的是：上游加载模型期间回 503 被判死，残留任务全部 failed，引爆调用方重提。
    class Response:
        def __init__(self):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(isolated_main.requests, "get", lambda *a, **k: Response())

    assert isolated_main._upstream_reachable() is True


@pytest.mark.parametrize(
    "error",
    [
        requests.ConnectionError("refused"),
        requests.Timeout("timeout"),
        requests.RequestException("boom"),
    ],
)
def test_connection_failure_means_unreachable(isolated_main, monkeypatch, error):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(isolated_main.requests, "get", boom)

    assert isolated_main._upstream_reachable() is False
