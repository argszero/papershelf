"""arXiv **图片**下载这一层：坏图不许被当成好的缓存下来（2026-10-26）。

`tests/test_arxiv.py` 把 `_download_image` **整个打桩掉了**（`lambda url, target: None`），
所以这一层原来一行覆盖都没有 —— 与 `tests/test_arxiv_download.py` 修 PDF 那次同一种盲区：
夹具把出事的那层换掉，测试全绿，出事的正是被换掉的那层。这份文件补的就是那层，
而且**不打桩传输层**：真起一个本机 HTTP 服务、真 `httpx` 去连。

修复前的实测（都在旧代码上跑出来的，`fig.png` 是目标文件名）：

| 服务器给的                        | 抛异常 | 最终路径上留下           |
|-----------------------------------|--------|--------------------------|
| 完整 PNG                          | 否     | 完整图片 ✓               |
| 200 + HTML 错误页（87 字节）      | 否     | 一页 HTML 存成 `fig.png` |
| 404                               | 否     | 什么都没有，**静默**     |
| 200，不给 `Content-Length` 就关连接 | 否     | **半截图**（2008→1004）  |

后三行原先都没有出路：调用方的命中判据只有 `local.exists()` ⇒ 坏文件**永久**当缓存，
再也不会重下。这份文件钉住的就是这三行。

⚠️ 第三行（不给 `Content-Length` 就断开）是**只看魔数抓不到**的那一类：截断的 PNG
开头仍是 `\\x89PNG`。真正的判据是 Pillow 的 `verify()`（实测 90% 截断也抓得出来）。
"""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from PIL import Image

from papershelf.server import arxiv

SVG_FIGURE = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<rect width="10" height="10"/></svg>'
)
HTML_PAGE = (
    b"<!DOCTYPE html>\n<html><head><title>403 Forbidden</title></head>"
    b"<body>Request blocked.</body></html>"
)


def _png(width: int = 40, height: int = 30) -> bytes:
    """一张**真的、完整的** PNG（Pillow 本来就是本项目的运行时依赖）。"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ── 本机 stub：真的 HTTP，不是 mock ──────────────────────────────────────
class _Stub:
    """一次测试用的响应脚本：内容、给不给 Content-Length、状态码。"""

    payload: bytes = b""
    status: int = 200
    declare_length: bool = True    # False ⇒ 不给 Content-Length 就关连接（半截收尾）
    hits: int = 0                  # 收到多少次 GET


def _make_handler(stub: _Stub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:                       # noqa: N802
            stub.hits += 1
            if stub.status != 200:
                # 不写响应体：客户端在 `raise_for_status()` 就抛了、不会读它，
                # 写了只会让 stub 线程打一段 BrokenPipe 的噪声。
                self.send_response(stub.status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            if stub.declare_length:
                self.send_header("Content-Length", str(len(stub.payload)))
                self.end_headers()
                self.wfile.write(stub.payload)
            else:
                # 不给长度，写完一半就关 —— 实测 `httpx.get` **不抛异常**，
                # 认为响应正常结束（这就是"半截图被缓存"的现场）。
                self.close_connection = True
                self.end_headers()
                self.wfile.write(stub.payload[: len(stub.payload) // 2])

        def log_message(self, *a) -> None:              # 别把访问日志刷进测试输出
            pass

    return Handler


@pytest.fixture()
def stub():
    s = _Stub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(s))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield s, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _target(tmp_path: Path, name: str = "fig.png") -> Path:
    d = tmp_path / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


# ── 1. 失败路径：最终路径上一个文件都不许留，`.part` 也不许留 ────────────
def test_a_truncated_body_without_content_length_leaves_nothing(stub, tmp_path):
    """服务器不给长度、写到一半关连接 —— 半截图不许落盘。

    这是修复前**唯一连魔数都看着正常**的那条：截断的 PNG 开头仍是 `\\x89PNG`，
    所以判据必须真的去验证图片结构，不能只看头几个字节。
    """
    s, base = stub
    s.payload, s.declare_length = _png(), False
    target = _target(tmp_path)

    with pytest.raises(RuntimeError):
        arxiv._download_image(f"{base}/fig.png", target)

    assert not target.exists(), "半截图落在了最终路径上"
    assert not list(target.parent.glob("*.part")), "失败的下载留下了 .part"


def test_an_html_page_landing_with_200_leaves_nothing(stub, tmp_path):
    """反爬/错误页以 200 落地时，不许被存成 `fig.png`。"""
    s, base = stub
    s.payload = HTML_PAGE
    target = _target(tmp_path)

    with pytest.raises(RuntimeError):
        arxiv._download_image(f"{base}/fig.png", target)

    assert not target.exists()


def test_a_non_200_raises_instead_of_being_ignored(stub, tmp_path):
    """非 200 必须抛：原先只是「不写文件」，连日志都不会有。"""
    s, base = stub
    s.status = 404
    target = _target(tmp_path)

    with pytest.raises(httpx.HTTPStatusError):
        arxiv._download_image(f"{base}/fig.png", target)

    assert not target.exists()


# ── 2. 成功路径：字节一致，且不留 .part ─────────────────────────────────
def test_a_complete_image_lands_byte_identical(stub, tmp_path):
    s, base = stub
    s.payload = _png()
    target = _target(tmp_path)

    arxiv._download_image(f"{base}/fig.png", target)

    assert target.read_bytes() == s.payload, "`os.replace` 不许改动内容"
    assert not list(target.parent.glob("*.part"))


# ── 3. 缓存命中判据本身 ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "data",
    [
        b"",
        HTML_PAGE,
        _png()[: int(len(_png()) * 0.9)],
        bytes(range(256)) * 2,
    ],
    ids=["empty", "html-page", "png-truncated-to-90pct", "random-bytes"],
)
def test_is_usable_image_rejects_each_kind_of_damage(data, tmp_path):
    p = _target(tmp_path, "bad.png")
    p.write_bytes(data)
    assert arxiv._is_usable_image(p) is False


def test_is_usable_image_accepts_a_complete_image(tmp_path):
    p = _target(tmp_path)
    p.write_bytes(_png())
    assert arxiv._is_usable_image(p) is True


def test_is_usable_image_accepts_an_svg_figure(tmp_path):
    """SVG 是文本，栅格库不认 ⇒ 必须**单独放过**。

    这条是防回归的：若把 SVG 一并交给 Pillow，`Image.open` 必抛
    `UnidentifiedImageError`，arXiv 那些 SVG 插图会被一律判成坏图、
    永远缓存不上 —— 那比原来的 bug 更糟。
    """
    p = _target(tmp_path, "fig.svg")
    p.write_bytes(SVG_FIGURE)
    assert arxiv._is_usable_image(p) is True


# ── 4. 存量治愈：老代码留在盘上的坏文件会被重下 ─────────────────────────
def test_a_poisoned_asset_is_downloaded_again(stub, monkeypatch, settings):
    """盘上已经躺着一页 HTML（旧代码的产物）⇒ 下一篇导入要**真的重下**。"""
    s, base = stub
    s.payload = _png()

    html = ('<section><p>Body text.</p>'
            '<figure><img src="fig1.png" /><figcaption>Figure 1.</figcaption></figure>'
            '</section>')
    poisoned = settings.papers_dir / "p1" / "assets" / "fig1.png"
    poisoned.parent.mkdir(parents=True, exist_ok=True)
    poisoned.write_bytes(HTML_PAGE)                      # 旧代码留下的坏缓存

    real_stream = httpx.stream

    def local_stream(method, url, **kw):
        return real_stream(method, str(url).replace("https://arxiv.org", base), **kw)

    monkeypatch.setattr(httpx, "stream", local_stream)

    arxiv._from_html(html, "2405.04434", "", {"id": 1}, settings)

    assert s.hits == 1, "坏缓存没有被重下"
    assert poisoned.read_bytes() == s.payload, "重下之后盘上还不是一张好图"
    assert arxiv._is_usable_image(poisoned) is True
