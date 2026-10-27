"""分享页图片 URL 的**篇身份**（2026-10-27）。

病灶：资产文件名**按页编号**（`p1_img1.png` / `p1_deco1.png`），与篇无关；而分享页的
图片前缀里**没有 paper id**，于是服务端只能按 id 升序遍历分享计划内所有文献、返回
第一个有该文件的那篇 —— 同一个计划里两篇都有第 1 页插图时，**编号小的那篇永远赢**，
读者看到的是另一篇的图。

失败模式是「换成别人的图」，不是「图片 404」：文件永远找得到，只是找错了人。
所以这里钉的是**字节归属**，不是状态码。

四件事：

1. 分享页取图拿到的是**它要的那一篇**的字节 —— 两个方向都断言。单向断言在旧的
   「总是返回编号最小那篇」实现下照样会绿，那样等于没测。
2. URL 里的 paper id **必须属于本次分享的计划** —— 别的计划里的资产一律 404，
   哪怕磁盘上有同名文件。这条是安全边界，不是顺手校验。
3. 内联导出**仍然真的内联了字节** —— 改了前缀却没改正则，症状是「静默地不再内联
   任何图片」，不报错、不出异常。
4. 旧前缀的路由**继续服务** —— 已经交到读者手里的 `inline=0` 导出件不能一夜失效。
"""

from __future__ import annotations

import base64
import re

import pytest


def _png(seed: int) -> bytes:
    """按 seed 生成**字节可区分**的最小 PNG（不需要能解码，只要求不同）。"""
    return b"\x89PNG\r\n\x1a\n" + b"PAPERSHELF-OWNER-%d" % seed + bytes([seed]) * 64


def _write_asset(settings, pid: int, data: bytes) -> None:
    d = settings.papers_dir / f"p{pid}" / "assets"
    d.mkdir(parents=True, exist_ok=True)
    (d / "p1_img1.png").write_bytes(data)


def _seed_paper(settings, plan_id: int, title: str) -> int:
    """直接落库一篇 `done` 的文献，带一张第 1 页插图（文件名与别的篇**同名**）。"""
    from papershelf.pipeline.model import Block, Doc
    from papershelf.server.db import connect
    from papershelf.server.repo import save_doc

    conn = connect(settings)
    try:
        cur = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,status_at,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (plan_id, title, "upload", "reading", "2026-01-01T00:00:00+00:00", "done"),
        )
        pid = int(cur.lastrowid)
        conn.commit()
        save_doc(conn, pid, Doc(meta={"title_zh": title}, blocks=[
            Block(id="b-0001", type="p", en=f"Body of {title}.", zh="正文。",
                  zh_source="mt", payload={"page": 1}),
            Block(id="b-0002", type="figure", en=f"Fig. 1 of {title}.", zh="图 1。",
                  zh_source="mt",
                  payload={"src": "assets/p1_img1.png", "page": 1,
                           "caption": f"Fig. 1 of {title}."}),
        ]))
        return pid
    finally:
        conn.close()


def _seed_outside_paper(settings, make_user) -> int:
    """**另一个用户、另一个计划**里的一篇，磁盘上有同名资产。"""
    from papershelf.server.db import connect

    email, _ = make_user("other@tsinghua.edu.cn")
    conn = connect(settings)
    try:
        uid = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        plan_id = int(conn.execute("INSERT INTO plans (user_id,name) VALUES (?,?)",
                                   (uid, "别人的计划")).lastrowid)
        conn.commit()
    finally:
        conn.close()
    pid = _seed_paper(settings, plan_id, "别人的文献")
    _write_asset(settings, pid, _png(pid))
    return pid


def _anon(client):
    """匿名客户端：⑪ 持链接即可看，分享契约里没有会话。"""
    return client.__class__(client.app_ref)


@pytest.fixture()
def shared_pair(client, settings, make_user):
    """一个计划、两篇 `done` 文献，各自一张**同名** `p1_img1.png`，字节不同。"""
    email, pw = make_user("owner@tsinghua.edu.cn")
    client.post("/api/auth/login", json={"email": email, "password": pw})
    plan_id = client.post("/api/plans", json={"name": "精读"}).json()["id"]

    pids = []
    for title in ("第一篇", "第二篇"):
        pid = _seed_paper(settings, plan_id, title)
        _write_asset(settings, pid, _png(pid))
        pids.append(pid)

    tok = client.post(f"/api/plans/{plan_id}/shares", json={"hours": 1}).json()["token"]
    return tok, pids[0], pids[1]


# ── 1. 拿到的必须是「它要的那一篇」的字节，两个方向都验 ─────────────────────
def test_share_reader_is_served_the_figure_of_the_paper_it_asked_for(shared_pair, client):
    """单向断言在旧实现下也是绿的（旧实现恒返回编号最小那篇）—— 所以两边都断言。"""
    tok, first, second = shared_pair
    anon = _anon(client)

    for pid in (first, second):
        doc = anon.get(f"/api/shares/{tok}/papers/{pid}").json()
        fig = next(b for b in doc["blocks"] if b["type"] == "figure")
        got = anon.get(fig["payload"]["src"])
        assert got.status_code == 200
        assert got.content == _png(pid), (
            f"分享页看第 {pid} 篇，取到的却是别人的字节 "
            f"（返回 {got.content[:40]!r}，应为 {_png(pid)[:40]!r}）"
        )


def test_share_asset_url_names_the_paper_it_belongs_to(shared_pair, client):
    tok, first, second = shared_pair
    doc = _anon(client).get(f"/api/shares/{tok}/papers/{second}").json()
    fig = next(b for b in doc["blocks"] if b["type"] == "figure")
    assert fig["payload"]["src"] == f"/share/{tok}/papers/{second}/assets/p1_img1.png"


# ── 2. 计划归属是安全边界 ────────────────────────────────────────────────
def test_share_assets_never_reach_outside_the_shared_plan(shared_pair, settings, make_user, client):
    """带 id 不等于可以取任何 id —— 没有这条检查，一个分享链接就能读别的计划。"""
    tok, _first, _second = shared_pair
    outside = _seed_outside_paper(settings, make_user)

    on_disk = settings.papers_dir / f"p{outside}" / "assets" / "p1_img1.png"
    assert on_disk.is_file() and on_disk.read_bytes() == _png(outside), \
        "文件必须真的在磁盘上，否则这个 404 什么也证明不了"

    r = _anon(client).get(f"/share/{tok}/papers/{outside}/assets/p1_img1.png")
    assert r.status_code == 404
    assert r.content != _png(outside)


# ── 3. 内联导出必须仍然真的内联（前缀与正则的锁步） ────────────────────────
def test_share_inline_export_still_inlines_the_bytes(shared_pair, client):
    """改了前缀却漏改 `_ASSET_SRC` 的症状：**静默地一张图都不内联**，不报错。"""
    tok, _first, second = shared_pair
    r = _anon(client).get(f"/api/shares/{tok}/papers/{second}/export?lang=dual")
    assert r.status_code == 200

    uris = re.findall(r'src="data:image/(?:png|jpeg|gif|webp|svg\+xml);base64,([^"]+)"', r.text)
    assert uris, "分享内联导出没有内联任何图片 —— 检查 app.py 的 _ASSET_SRC 是否跟上了新前缀"
    assert any(base64.b64decode(u) == _png(second) for u in uris)
    assert 'src="/share/' not in r.text         # 没有残留未内联的分享链接


def test_share_export_without_inlining_points_at_the_pid_bearing_url(shared_pair, client):
    tok, _first, second = shared_pair
    anon = _anon(client)
    r = anon.get(f"/api/shares/{tok}/papers/{second}/export?lang=dual&inline=0")
    assert f'src="/share/{tok}/papers/{second}/assets/p1_img1.png"' in r.text
    assert anon.get(f"/share/{tok}/papers/{second}/assets/p1_img1.png").content == _png(second)


# ── 4. 旧前缀继续服务（兼容，不是修复） ──────────────────────────────────
def test_the_legacy_share_asset_url_keeps_serving(shared_pair, client):
    """已经交到读者手里的 `inline=0` 导出件不能一夜失效。

    这条 URL 天生说不出「这是哪一篇的图」，所以断言到此为止：**能取到一张同名图**。
    它恒返回编号最小那篇的字节 —— 那正是要退役的歧义，服务端修不了，只能不再生成。
    """
    tok, first, _second = shared_pair
    r = _anon(client).get(f"/share/{tok}/assets/p1_img1.png")
    assert r.status_code == 200
    assert r.content == _png(first)
