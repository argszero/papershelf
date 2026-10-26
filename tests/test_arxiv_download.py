"""arXiv **下载**这一层：半截文件不许被当成好的（2026-10-26）。

`tests/test_arxiv.py` 把 `_download_pdf` **整个打桩掉了**（`lambda *a, **k: pytest.fail(...)`），
所以这里原来一行覆盖都没有 —— 与 2026-09-11 发信那次同一种盲区：夹具把出事的那层
换掉，测试全绿，出事的是被换掉的那层。这份文件补的就是那层，而且**不打桩传输层**：
真起一个本机 HTTP 服务、真 `httpx` 去连，这样"流中途断掉"是内核把连接关掉的真行为，
不是 mock 里手写的假异常。

钉住的四件事：

1. 下载失败（中途断流 / 反爬 HTML 页）**不在最终路径留下任何文件**，`.part` 也不留；
2. 缓存命中判据是**完整**而不是"存在且够大" ⇒ 盘上的残篇会被**重下**（原来永远不重下）；
3. 一份完整的缓存**不触发重复下载**（别为了修 bug 变成每次都下）；
4. 完整下载的产物与服务器给的字节**逐字节一致**（`os.replace` 不许动内容）。
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from papershelf.server import arxiv


# ── 本机 stub：真的 HTTP，不是 mock ──────────────────────────────────────
class _Stub:
    """一次测试用的响应脚本：内容、声明的 Content-Length、以及从哪儿掐断。"""

    payload: bytes = b""
    drop_at: int | None = None     # 写到第 N 字节就关连接（真·中途断流）
    hits: int = 0                  # 收到多少次 GET


def _make_handler(stub: _Stub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:                       # noqa: N802
            stub.hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(stub.payload)))
            self.end_headers()
            body = stub.payload if stub.drop_at is None else stub.payload[: stub.drop_at]
            self.wfile.write(body)
            if stub.drop_at is not None:
                self.close_connection = True            # 提前关 ⇒ 客户端读到截断

        def log_message(self, *a) -> None:              # 别把访问日志刷进测试输出
            pass

    return Handler


@pytest.fixture()
def stub(monkeypatch):
    """本机 HTTP 服务 + 把 arxiv 里的 `https://arxiv.org` 改指向它。

    只换**主机名**，`_download_pdf` 自己的代码路径（`httpx.stream` / 分块读 /
    Content-Length 校验 / `os.replace`）全部真跑。
    """
    s = _Stub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(s))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    real_stream = httpx.stream

    def local_stream(method, url, **kw):
        return real_stream(method, str(url).replace("https://arxiv.org", base), **kw)

    monkeypatch.setattr(httpx, "stream", local_stream)
    try:
        yield s
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _pdf_bytes(min_size: int = 12_000) -> bytes:
    """一份**真的、完整的** PDF，且大于 `_MIN_PDF_BYTES`（否则缓存判据的尺寸底线会先挡掉）。"""
    import fitz

    doc = fitz.open()
    while True:
        for _ in range(4):
            page = doc.new_page()
            page.insert_text((72, 72), "Synthetic paper body. " * 20)
        data = doc.tobytes()
        if len(data) >= min_size:
            doc.close()
            assert data[:4] == b"%PDF" and b"%%EOF" in data[-1024:]
            return data


def _target(settings, name: str = "2405.04434.pdf") -> Path:
    d = settings.papers_dir / "p1"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


# ── 1. 失败路径：最终路径上一个文件都不许留 ──────────────────────────────
def test_a_mid_stream_drop_leaves_no_file_behind(stub, settings):
    """流写到一半连接断掉 —— 这就是"下了一半"的现场。"""
    stub.payload = _pdf_bytes()
    stub.drop_at = len(stub.payload) // 2
    target = _target(settings)

    with pytest.raises(Exception) as ei:
        arxiv._download_pdf("2405.04434", "", target)

    assert not target.exists(), "半截文件落到了最终路径 —— 下次会被当成缓存命中"
    assert not target.with_name(target.name + ".part").exists(), "临时文件没被清掉"
    assert "2405.04434" in str(target)  # 只用来确认路径确实是对的
    assert ei.value is not None


def test_an_anti_scrape_html_page_leaves_no_file_behind(stub, settings):
    """arXiv 回的是反爬 HTML 页（自身代码注释就写着会发生）。

    旧实现抛了 `RuntimeError` 却**把文件留在盘上**，而且通常 > 10 KB ⇒ 之后每次
    都跳过下载、在同一步抛同一个错，用户除了删文献重导没有出路。
    """
    stub.payload = b"<!doctype html><html><body>" + b"blocked " * 1500 + b"</body></html>"
    assert len(stub.payload) > arxiv._MIN_PDF_BYTES       # 前提：够大，旧判据会放行
    target = _target(settings)

    with pytest.raises(RuntimeError):
        arxiv._download_pdf("2405.04434", "", target)

    assert not target.exists()
    assert not target.with_name(target.name + ".part").exists()


# ── 2. 成功路径：逐字节一致，且不留 .part ────────────────────────────────
def test_a_complete_download_lands_byte_identical(stub, settings):
    stub.payload = _pdf_bytes()
    target = _target(settings)

    arxiv._download_pdf("2405.04434", "", target)

    assert target.read_bytes() == stub.payload        # os.replace 不许动内容
    assert not target.with_name(target.name + ".part").exists()


# ── 3. 缓存判据：残篇要重下，完整的不要重下 ──────────────────────────────
def _import_once(settings, monkeypatch) -> None:
    """跑一遍 `import_arxiv_doc` 的 PDF 回落分支，但把 HTML 与解析层打桩掉。

    只让**下载与缓存**这一层真跑：HTML 一律判为「不可用」，解析器换成空 Doc。
    """
    from papershelf.pipeline.model import Doc
    import papershelf.pipeline as pipeline

    monkeypatch.setattr(arxiv, "_fetch_html", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "parse_pdf", lambda *a, **k: Doc(meta={}, blocks=[]))
    arxiv.import_arxiv_doc({"id": 1, "source_ref": "2405.04434"}, settings)


def test_a_truncated_cache_is_downloaded_again(stub, settings, monkeypatch):
    """**核心回归**：盘上那份是残篇（头是 `%PDF`、够大、PyMuPDF 也能打开）⇒ 必须重下。

    故意截到 90%：文件仍**大于** `_MIN_PDF_BYTES`，所以"尺寸底线"不可能成为
    重下的理由 —— 让 `%%EOF` 判据独自把这件事扛下来。
    """
    whole = _pdf_bytes()
    truncated = whole[: int(len(whole) * 0.9)]
    assert len(truncated) > arxiv._MIN_PDF_BYTES
    assert truncated[:5].startswith(b"%PDF")
    assert b"%%EOF" not in truncated[-1024:]

    target = _target(settings)
    target.write_bytes(truncated)                    # 模拟旧代码留下的残篇
    stub.payload = whole
    stub.hits = 0

    _import_once(settings, monkeypatch)

    assert stub.hits == 1, "残篇被当成了缓存命中 —— 一次都没重下"
    assert target.read_bytes() == whole              # 真的换成完整那份了


def test_a_complete_cache_is_not_downloaded_again(stub, settings, monkeypatch):
    """修 bug 不许变成"每次都下"：完整缓存就该零请求。"""
    whole = _pdf_bytes()
    target = _target(settings)
    target.write_bytes(whole)
    stub.payload = whole
    stub.hits = 0

    _import_once(settings, monkeypatch)

    assert stub.hits == 0, f"完整缓存被重下了 {stub.hits} 次"
    assert target.read_bytes() == whole


# ── 4. 判据本身的单元测试 ────────────────────────────────────────────────
def test_is_complete_pdf_rejects_each_kind_of_damage(tmp_path):
    whole = _pdf_bytes()
    good = tmp_path / "good.pdf"
    good.write_bytes(whole)
    assert arxiv._is_complete_pdf(good)

    no_eof = tmp_path / "no_eof.pdf"
    no_eof.write_bytes(whole[:-2048])                       # 尾部被切
    assert not arxiv._is_complete_pdf(no_eof)

    not_pdf = tmp_path / "not_pdf.pdf"
    not_pdf.write_bytes(b"<html>" + b"x" * 12_000)
    assert not arxiv._is_complete_pdf(not_pdf)

    tiny = tmp_path / "tiny.pdf"
    tiny.write_bytes(b"%PDF-1.4\n%%EOF\n")                  # 完整但很小
    assert arxiv._is_complete_pdf(tiny)                     # 完整性本身判它过
    assert not arxiv._is_complete_pdf(tiny, min_bytes=arxiv._MIN_PDF_BYTES)

    assert not arxiv._is_complete_pdf(tmp_path / "missing.pdf")


def test_a_tiny_but_complete_download_is_not_rejected(stub, settings):
    """小而完整的 PDF 必须能落盘。

    `_MIN_PDF_BYTES` 是"像不像一篇论文"（判缓存复用），不是"下载完整不完整"；
    两者混在一个判据里会让小 PDF 从"每次重下"（旧行为）变成"直接失败"（回归）。
    """
    import fitz

    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Tiny but whole.")
    tiny = doc.tobytes()
    doc.close()
    assert len(tiny) < arxiv._MIN_PDF_BYTES
    stub.payload = tiny
    target = _target(settings, "tiny.pdf")

    arxiv._download_pdf("2405.04434", "", target)

    assert target.read_bytes() == tiny
