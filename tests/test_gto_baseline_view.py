"""GTO 基线台静态前端: 只测服务本身, 不测页面内 React 渲染(浏览器环境)。"""

import urllib.error
import urllib.request

import pytest

from texas_agent.gto_baseline_view import GtoBaselineView


@pytest.fixture
def view():
    v = GtoBaselineView(port=0)   # 端口 0 = 随机可用端口
    yield v
    v.close()


def test_serves_index(view):
    port = view._srv.server_port
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read()
    assert "GTO 基线台".encode() in page
    assert b'<script src="./support.js">' in page


def test_serves_support_js(view):
    port = view._srv.server_port
    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/support.js", timeout=5)
    assert resp.headers["Content-Type"].startswith("application/javascript")
    assert b"dc-runtime" in resp.read()


def test_rejects_path_traversal(view):
    port = view._srv.server_port
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/../gto_baseline_view.py", timeout=5)
    assert exc.value.code == 404
