"""导出内联的**内存边界**（2026-10-26：rant 09:30:12 那一族的下一处持有者）。

`tests/test_server.py` 钉的是「导出是个自包含单文件」——**内容**对不对。
这里钉的是它**怎么被产出来的**：

1. 内联改成按块流式 ⇒ 峰值只与块大小有关，与论文有多少张图、图多大无关；
2. 内联总体积有上限（`PAPERSHELF_MAX_EXPORT_MB`），超限 413 且**错误里点名 `inline=0`**
   （点名的那条路必须真的能用，测试同时验它）；
3. 分享导出也能退出内联 —— 它原先连 `inline` 参数都没有，而无条件内联正是
   "不需要会话、有链接就能反复打"的那个端点；
4. 流式产物与改动前「整篇 `re.sub`」的结果**逐字节一致** —— 这是重写，内容一个字都不能变。

⚠️ **测法上的一条硬约束**：峰值不能通过 `TestClient` 量。它会把整个响应体收进
`r.text`，于是峰值里必然含全部内联内容，改不改流式都一样 —— 那样测出来的是
"客户端收了多少"，不是"服务端产了多少"。所以内存那条直接消费生成器、逐块丢弃。
"""

from __future__ import annotations

import base64
import re
import tracemalloc
from pathlib import Path

import pytest
from fastapi import HTTPException

from papershelf.pipeline.model import Block, Doc

MB = 1024 * 1024


def _payload(n: int) -> bytes:
    """可复现的 n 字节假图片内容（内容不参与判据，只要求原样读回）。"""
    return (bytes(range(256)) * (n // 256 + 1))[:n]


def _figure_blocks(names: list[str]) -> list[Block]:
    blocks = [
        Block(id="b-0001", type="p", en="Safety is important.", zh="安全性很重要。", zh_source="mt"),
    ]
    for i, name in enumerate(names, start=1):
        blocks.append(Block(id=f"b-{i + 1:04d}", type="figure", en=f"Fig. {i}.", zh=f"图 {i}。",
                            zh_source="mt",
                            payload={"src": f"assets/{name}", "caption": f"Fig. {i}."}))
    return blocks


def _seed_paper(settings, plan_id: int, blocks: list[Block]) -> int:
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        cur = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,status_at,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (plan_id, "测试文献", "upload", "reading", "2026-01-01T00:00:00+00:00", "done"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        save_doc(conn, pid, Doc(meta={"title_zh": "测试"}, blocks=blocks))
        return pid
    finally:
        conn.close()


def _write_assets(settings, pid: int, names: list[str], size: int) -> None:
    d = settings.papers_dir / f"p{pid}" / "assets"
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).write_bytes(_payload(size))


@pytest.fixture()
def seeded(client, settings, make_user):
    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]
    pid = _seed_paper(settings, plan_id, _figure_blocks(["p1_img1.png"]))
    return {"plan_id": plan_id, "pid": pid}


def _share_token(client, plan_id: int) -> str:
    return client.post(f"/api/plans/{plan_id}/shares", json={"hours": 1}).json()["token"]


def _anon(client):
    return client.__class__(client.app_ref)


def _data_uris(html: str) -> list[str]:
    return re.findall(r'src="data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,([^"]+)"', html)


# ── 1. 流式产物必须与改动前逐字节一致 ─────────────────────────────────────
def test_streamed_export_is_byte_identical_to_the_whole_string_rendering(client, settings, seeded):
    """重写式的改动，唯一的验收标准就是**内容没变**。

    参照实现 = 改动前那种「读进来 → base64 → `re.sub` 整篇重写」的写法，
    直接在测试里重新实现一遍，然后逐字节比对。块边界、mime、引号位置任何一处写错都会红。
    """
    pid = seeded["pid"]
    _write_assets(settings, pid, ["p1_img1.png"], 4096)
    raw = _payload(4096)

    streamed = client.get(f"/api/papers/{pid}/export?lang=dual&view=1").text
    plain = client.get(f"/api/papers/{pid}/export?lang=dual&view=1&inline=0").text
    expected = re.sub(
        r'src="(?:/papers/\d+/assets/)([^"]+)"',
        lambda m: f'src="data:image/png;base64,{base64.b64encode(raw).decode("ascii")}"',
        plain,
    )
    assert streamed == expected
    assert base64.b64decode(_data_uris(streamed)[0]) == raw


def test_base64_chunk_size_is_a_multiple_of_three():
    """块大小必须按 3 字节对齐。

    base64 是 3 字节一组的：块边界不对齐会在**流中间**塞进 `=` 填充，拼出来的
    data URI 从第一个填充处之后全是垃圾，而浏览器只会显示一张坏图（不报错）。
    """
    from papershelf.server.app import _INLINE_CHUNK

    assert _INLINE_CHUNK % 3 == 0, f"_INLINE_CHUNK={_INLINE_CHUNK} 不是 3 的整数倍"


def test_data_uri_round_trips_across_chunk_boundaries(client, settings, seeded):
    """大于一个块的资产：跨块拼回来的字节必须一字不差。"""
    pid = seeded["pid"]
    size = 5 * MB // 2                      # 2.5 MB：跨 768 KB 的块边界多次
    _write_assets(settings, pid, ["p1_img1.png"], size)

    html = client.get(f"/api/papers/{pid}/export?lang=dual&view=1").text
    assert base64.b64decode(_data_uris(html)[0]) == _payload(size)


# ── 2. 峰值内存不随图片总量增长 ──────────────────────────────────────────
def _peak_when_consuming(settings, pid: int, names: list[str]) -> int:
    """逐块消费生成器、每块只数长度就丢掉 —— 峰值反映的是**产出端**。"""
    from papershelf.server.app import iter_inlined_html

    html = "".join(f'<img src="/papers/{pid}/assets/{n}">' for n in names)
    tracemalloc.start()
    try:
        seen = 0
        for chunk in iter_inlined_html(html, pid, settings):
            seen += len(chunk)
        assert seen > 0
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_peak_memory_does_not_grow_with_the_number_of_assets(settings):
    """三倍资产 ⇒ 峰值基本不变。改动前这里是 4× 全篇总量。"""
    pid = 1
    two = [f"a{i}.png" for i in range(2)]
    six = [f"b{i}.png" for i in range(6)]
    _write_assets(settings, pid, two + six, MB)

    peak_two = _peak_when_consuming(settings, pid, two)
    peak_six = _peak_when_consuming(settings, pid, six)

    assert peak_six < peak_two * 1.6, (
        f"峰值随图片数量增长：2 张 {peak_two / MB:.2f} MB → 6 张 {peak_six / MB:.2f} MB，"
        "说明内联又把整篇资产留在内存里了"
    )
    assert peak_six < 3 * MB, f"6 MB 资产的内联峰值 {peak_six / MB:.2f} MB，远超一个块的大小"


# ── 3. 上限：413 + 点名 inline=0 ─────────────────────────────────────────
def test_export_over_the_cap_is_413_and_inline_zero_really_works(client, settings, seeded):
    pid = seeded["pid"]
    _write_assets(settings, pid, ["p1_img1.png"], 2 * MB)
    settings.max_export_mb = 1

    r = client.get(f"/api/papers/{pid}/export?lang=dual&view=1")
    assert r.status_code == 413
    assert "PAPERSHELF_MAX_EXPORT_MB" in r.text
    assert "inline=0" in r.text                 # 错误里点名的那条路…

    ok = client.get(f"/api/papers/{pid}/export?lang=dual&view=1&inline=0")
    assert ok.status_code == 200                # …必须真的能用
    assert _data_uris(ok.text) == []
    assert f"/papers/{pid}/assets/p1_img1.png" in ok.text


def test_the_cap_boundary_is_strictly_greater_than(settings):
    """边界是 `>`，不是 `>=`：正好等于上限要放行。

    ⚠️ 这条**故意不用端点**：`dual` 模式下同一个图注块会在左右两栏各渲染一个 `<img>`，
    于是一张资产被内联**两次**（既有行为，本轮不改布局）。拿端点来量边界就会一边
    数布局、一边判体积，测出来的东西说不清。这里用一份自己构造的单引用文档。
    """
    from papershelf.server.app import inline_asset_bytes, stream_inlined_export

    pid = 1
    _write_assets(settings, pid, ["edge.png"], MB)
    html = f'<img src="/papers/{pid}/assets/edge.png">'
    settings.max_export_mb = 1

    assert inline_asset_bytes(html, pid, settings) == MB
    body = "".join(stream_inlined_export(html, pid, settings))     # 恰好等于上限：放行
    assert "data:image/png;base64," in body

    (settings.papers_dir / f"p{pid}" / "assets" / "edge.png").write_bytes(_payload(MB + 1))
    with pytest.raises(HTTPException):
        stream_inlined_export(html, pid, settings)                 # 多 1 字节：拒绝


def test_dual_mode_inlines_a_figure_twice(client, settings, seeded):
    """钉住一个**既有**事实（本轮不修，但要写下来）：dual 模式每个图注块两栏各一张。

    它让"资产总量"在 dual 导出里翻倍，是上限最容易低估的一处。改布局不在本轮范围内，
    但哪天有人动它，这条会提醒他导出体积的算式也跟着变了。
    """
    pid = seeded["pid"]
    _write_assets(settings, pid, ["p1_img1.png"], 4096)
    assert len(_data_uris(client.get(f"/api/papers/{pid}/export?lang=dual").text)) == 2
    assert len(_data_uris(client.get(f"/api/papers/{pid}/export?lang=zh").text)) == 1


def test_the_cap_is_checked_before_any_asset_is_opened(settings, monkeypatch):
    """超限必须在**打开文件之前**判出来。

    先读后判等于没修：那个请求仍然要吃掉一份资产的常驻内存。
    """
    from papershelf.server.app import stream_inlined_export

    pid = 1
    _write_assets(settings, pid, ["big.png"], 2 * MB)
    settings.max_export_mb = 1

    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **kw):
        opened.append(self.name)
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", spy)
    with pytest.raises(HTTPException) as ei:
        stream_inlined_export(f'<img src="/papers/{pid}/assets/big.png">', pid, settings)

    assert ei.value.status_code == 413
    assert opened == [], f"超限请求仍然打开了资产文件：{opened}"


# ── 4. 分享导出：也能退出内联，且依然不需要会话 ──────────────────────────
def test_share_export_can_opt_out_of_inlining(client, settings, seeded):
    """那个端点原先只能内联 —— 这是它自己的悬崖（无会话、有链接就能反复打）。"""
    pid = seeded["pid"]
    _write_assets(settings, pid, ["p1_img1.png"], 4096)
    tok = _share_token(client, seeded["plan_id"])

    r = _anon(client).get(f"/api/shares/{tok}/papers/{pid}/export?inline=0")
    assert r.status_code == 200
    assert _data_uris(r.text) == []
    assert f"/share/{tok}/assets/p1_img1.png" in r.text


def test_share_export_over_the_cap_is_413_not_500(client, settings, seeded):
    pid = seeded["pid"]
    _write_assets(settings, pid, ["p1_img1.png"], 2 * MB)
    settings.max_export_mb = 1
    tok = _share_token(client, seeded["plan_id"])

    r = _anon(client).get(f"/api/shares/{tok}/papers/{pid}/export")
    assert r.status_code == 413
    assert "inline=0" in r.text


def test_share_export_still_inlines_without_a_session(client, settings, seeded):
    """⑪ 的设计不变：分享导出**不需要会话**，默认仍然是单文件。

    内存要改，门槛一个字不许动 —— 这条就是当初钉住设计的那条断言的守门人。
    """
    pid = seeded["pid"]
    _write_assets(settings, pid, ["p1_img1.png"], 4096)
    tok = _share_token(client, seeded["plan_id"])

    r = _anon(client).get(f"/api/shares/{tok}/papers/{pid}/export")
    assert r.status_code == 200
    assert base64.b64decode(_data_uris(r.text)[0]) == _payload(4096)
