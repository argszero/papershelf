"""读模型 —— Doc/Block 与 SQLite 行之间的互换。

`pipeline.Doc` 是**转换期**的形态（内存、整篇）；库里是**服务期**的形态（按块行存）。
两者必须能无损往返，否则「块级修订」改完存回去就丢字段。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

from ..pipeline.markup import render_block
from ..pipeline.model import Block, Doc, table_zh_usable
from .db import dump_json, load_json, rows_to_list, tx, utcnow

# 免中文块（无译文的块）在阅读器里走「单栏横跨」渲染
from ..pipeline.validate import NO_ZH_TYPES  # noqa: F401  （下游 routers 会用到）


def save_doc(conn: sqlite3.Connection, paper_id: int, doc: Doc) -> None:
    """整篇覆盖写入（转换完成时调用；块级修订请用 `update_block`）。"""
    with tx(conn):
        conn.execute("DELETE FROM blocks WHERE paper_id=?", (paper_id,))
        conn.executemany(
            """INSERT INTO blocks (paper_id, id, ord, type, level, section, en, zh, zh_source, payload)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (paper_id, b.id, i, b.type, b.level, b.section, b.en, b.zh, b.zh_source,
                 dump_json(b.payload))
                for i, b in enumerate(doc.blocks)
            ],
        )
        conn.execute(
            """INSERT INTO docs (paper_id, meta, assets, block_count, version, created_at, updated_at)
               VALUES (?,?,?,?,1,?,?)
               ON CONFLICT(paper_id) DO UPDATE SET
                 meta=excluded.meta, assets=excluded.assets,
                 block_count=excluded.block_count, updated_at=excluded.updated_at""",
            (paper_id, dump_json(doc.meta), dump_json(doc.assets), len(doc.blocks), utcnow(), utcnow()),
        )


def load_doc(conn: sqlite3.Connection, paper_id: int) -> Doc | None:
    meta_row = conn.execute("SELECT * FROM docs WHERE paper_id=?", (paper_id,)).fetchone()
    if meta_row is None:
        return None
    rows = conn.execute(
        "SELECT * FROM blocks WHERE paper_id=? ORDER BY ord", (paper_id,)
    ).fetchall()
    blocks = [
        Block(id=r["id"], type=r["type"], en=r["en"] or "", zh=r["zh"] or "",
              zh_source=r["zh_source"] or "none", section=r["section"] or "",
              level=r["level"] or 0, payload=load_json(r["payload"], {}))
        for r in rows
    ]
    return Doc(meta=load_json(meta_row["meta"], {}), assets=load_json(meta_row["assets"], []),
               blocks=blocks)


def paper_public(r: dict[str, Any]) -> dict[str, Any]:
    """文献行的**公开形状**（登录态与匿名分享共用，必须一模一样）。

    ⚠️ 为什么放在这里而不是 `routers/papers.py`：⑩ 分享页要「和分享者看到的一模一样」，
    两条路径的 papers 数组若各拼一份，迟早会漂移（分享侧少字段 → 页面集体变
    `undefined`，而 TypeScript 不会报错）。**只留一个来源**。
    """
    return {
        "id": r["id"], "plan_id": r["plan_id"], "title": r["title"], "authors": r["authors"],
        "venue": r["venue"], "year": r["year"], "tags": load_json(r["tags"], []),
        "source": r["source"], "source_ref": r["source_ref"],
        "status": r["status"], "status_at": r["status_at"],
        "progress": r["progress"], "progress_mode": r["progress_mode"],
        # 「最近阅读」（⑰ 补充，2026-09-15）：只有滚动上报写它。
        # ⚠️ 别和 `status_at` 混用 —— 后者是**进入当前状态**的时间（翻「在读」时也只盖一次）；
        # 前者是**最后一次真正阅读**的时间。看板排序用后者，总览"停摆"提醒用前者。
        "last_read_at": r["last_read_at"] if "last_read_at" in r.keys() else None,
        "conv_state": r["conv_state"], "conv_error": r["conv_error"],
        "created_at": r["created_at"],
    }


def plan_public(row: dict[str, Any], paper_count: int = 0, done_count: int = 0) -> dict[str, Any]:
    """计划行的公开形状（登录态 `GET /api/plans` 与分享端点共用）。"""
    return {
        "id": row["id"], "name": row["name"], "goal": row["goal"],
        "description": row["description"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "paper_count": paper_count, "done_count": done_count,
    }


def plan_stats(conn: sqlite3.Connection, plan_id: int) -> tuple[int, int]:
    """`(总篇数, 已精读篇数)` —— 侧栏进度卡与计划卡都要这个口径。"""
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status IN ('read','reviewed') THEN 1 ELSE 0 END) AS done
           FROM papers WHERE plan_id=?""",
        (plan_id,),
    ).fetchone()
    return (row["total"] or 0, row["done"] or 0)


def doc_public(conn: sqlite3.Connection, paper_id: int,
               asset_prefix: str = "", html: bool = True) -> dict[str, Any] | None:
    """阅读器取数的形状：块级 JSON（决策⑳）+ 元信息 + 资产（+ 可选的服务端渲染 HTML）。

    为什么把**渲染好的 HTML 也发过来**（而不是让前端自己排版）：
    - 公式是**服务端 LaTeX → MathML**（`pipeline/mathml.py`，遗留项 8）——
      前端只负责把这段 HTML 塞进 DOM（`dangerouslySetInnerHTML`，内容来自后端的块 JSON），
      于是**前端零数学运行时、零字体、零 CDN**，屏幕上看到的就是导出 HTML 里的同一套渲染；
    - 图片 `src` 需要按访问方式改写（`/papers/<id>/assets/`、分享 token 同理），
      前端去拼这些路径只会与服务端规则漂移。

    原始 `en`/`zh` 文本仍然发出（编辑器要按纯文本编辑），HTML 只是派生态。
    """
    doc = load_doc(conn, paper_id)
    if doc is None:
        return None
    marks = marks_by_block(list_highlights(conn, paper_id))
    blocks: list[dict[str, Any]] = []
    for b in doc.blocks:
        item = public_block(b, asset_prefix=asset_prefix, marks=marks.get(b.id))
        if not html:                      # 只发纯文本（无需渲染时省掉一遍 LaTeX→MathML）
            item.pop("en_html", None)
            item.pop("zh_html", None)
        blocks.append(item)
    return {"paper_id": paper_id, "meta": doc.meta, "assets": doc.assets, "blocks": blocks}


def public_block(b: Block, *, asset_prefix: str = "",
                 marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """单块的**公开形状** —— 必须与 `doc_public` 里逐块组装的那份逐字段一致。

    ⚠️ 为什么要有这个函数（踩过）：前端改完一块后若只收到 `{id, zh}`，
    它只能就地改纯文本字段，而 `zh_html` 仍是**改之前**服务端渲染的 HTML，
    于是「已保存」的译文在屏幕上纹丝不动（看起来像没生效，其实库里已经是新的）。
    改块和重译都要回一份完整块对象，前端整块替换 → 不存在新旧混杂。

    ⚠️ `anchors=True` 与这里**必须成对**（㉛）：前端要拿 DOM 选区换算字符偏移，
    只能靠渲染器吐的零宽锚点当尺子。少了它，划痕还能看见、但**再划新的一道就会错位**
    —— 一种"看起来能用"的静默故障（`tests/test_markup.py` 钉住了这条）。
    反过来，prompt/导出/校验路径不传 → 产物里一个多余 span 都没有。
    """
    src = b.payload.get("src") if isinstance(b.payload, dict) else None
    if asset_prefix and src and not str(src).startswith(("http://", "https://", "/", "data:")):
        b = dataclasses.replace(
            b, payload={**b.payload, "src": f"{asset_prefix.rstrip('/')}/{str(src).split('/')[-1]}"}
        )
    # ⚠️ 标题只给**内部** HTML（`wrap=False`）：阅读器自己渲 `<h2 class="doc-h lvlN" data-b=…>`，
    # 再套一层服务端的 `<h2 class="sec">` 就会得到 `<h2><h2>`（浏览器会把内层甩到外面）。
    # 见 `markup.render_block` 的 `wrap` 参数与 `web/src/pages/Reader.tsx` 的标题分支。
    wrap = not b.type.startswith("h")
    item = {
        "id": b.id, "type": b.type, "level": b.level, "section": b.section,
        "en": b.en, "zh": b.zh, "zh_source": b.zh_source,
        "payload": b.payload,
        "needs_review": bool(b.payload.get("needs_review")),
        "no_zh": b.type in NO_ZH_TYPES,
        "en_html": render_block(b, lang="en", marker=False, typeset=True, marks=marks,
                                anchors=True, wrap=wrap),
        "zh_html": render_block(b, lang="zh", marker=False, typeset=True, marks=marks,
                                anchors=True, wrap=wrap),
    }
    if b.type == "table":
        # 对照模式渲不渲**第二张表**（左英右中）由服务端说了算（决策㊵）：
        # 判据是"中文网格是否真的可用"（形状一致 + 不是逐格照抄英文，见 `model.table_zh_usable`）。
        # 前端自己再判一遍 = 第二份判据，迟早与渲染漂开（漂开的表现是右边多出一张
        # 与左边一模一样的表，或该有的中文表不出现）。
        item["table_zh"] = table_zh_usable(b)
    return item


def get_block(conn: sqlite3.Connection, paper_id: int, block_id: str) -> Block | None:
    row = conn.execute("SELECT * FROM blocks WHERE paper_id=? AND id=?",
                       (paper_id, block_id)).fetchone()
    if row is None:
        return None
    return Block(id=row["id"], type=row["type"], en=row["en"] or "", zh=row["zh"] or "",
                 zh_source=row["zh_source"] or "none", section=row["section"] or "",
                 level=row["level"] or 0, payload=load_json(row["payload"], {}))


def update_block(conn: sqlite3.Connection, paper_id: int, block_id: str, *,
                 zh: str | None = None, zh_source: str | None = None,
                 payload_patch: dict[str, Any] | None = None) -> bool:
    """块级修订（决策⑯）：人工改 `zh` 时必须把 `zh_source` 置为 `human`，重跑不得覆盖。"""
    row = conn.execute("SELECT payload FROM blocks WHERE paper_id=? AND id=?",
                       (paper_id, block_id)).fetchone()
    if row is None:
        return False
    sets, args = [], []
    if zh is not None:
        sets.append("zh=?")
        args.append(zh)
        sets.append("zh_source=?")
        args.append(zh_source or "human")
    if payload_patch:
        payload = load_json(row["payload"], {})
        payload.update(payload_patch)
        payload.pop("needs_review", None) if payload_patch.get("reconciled") else None
        sets.append("payload=?")
        args.append(dump_json(payload))
    if not sets:
        return False
    args += [paper_id, block_id]
    with tx(conn):
        conn.execute(f"UPDATE blocks SET {', '.join(sets)} WHERE paper_id=? AND id=?", args)
    return True


_NOTE_ORDER = """
SELECT n.* FROM notes n
LEFT JOIN blocks b ON b.id = n.block_id AND b.paper_id = n.paper_id
WHERE n.paper_id = ?
ORDER BY
    -- ① 有落点的在前、无落点的（文献级笔记）在后：笔记列表是**顺着原文读**的，
    --    不按写入时间来（宿主 2026-09-15：「应该按照笔记关联的原文的位置为顺序」）。
    CASE WHEN n.block_id IS NULL OR b.ord IS NULL THEN 1 ELSE 0 END,
    -- ② 落点跟着文档顺序（`blocks.ord` 就是阅读顺序，㉜ 分栏感知已把它排好）。
    --    `b.ord IS NULL` 的兜底：块在重新解析后消失了，别让它插到最前面。
    COALESCE(b.ord, 0),
    -- ③ 同一段里：整段笔记在前，然后是划住某几个字的（`start` 非空）。
    CASE WHEN n.start IS NULL THEN 0 ELSE 1 END,
    -- ④ 再按字符位置；位置相同（两栏各有笔记）时原文列在前 —— 与并排阅读的视觉顺序一致。
    COALESCE(n.start, 0),
    CASE n.lang WHEN 'en' THEN 0 WHEN 'zh' THEN 1 ELSE 2 END,
    n.id
"""


def list_notes(conn: sqlite3.Connection, paper_id: int) -> list[dict[str, Any]]:
    """该文献的全部笔记，**按锚点在原文里的位置排序**（不是按写入时间）。

    排序键见 `_NOTE_ORDER` 的注释；最后一级 `n.id` 只为让顺序**确定**（同样位置的
    两条笔记不该因为查询计划不同而换位）。
    """
    return rows_to_list(conn.execute(_NOTE_ORDER, (paper_id,)).fetchall())


def marks_by_block(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """划痕按块分组 —— 渲染时一个块要一次性拿到**它自己的**全部划痕。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r["block_id"]), []).append(r)
    return out


def list_highlights(conn: sqlite3.Connection, paper_id: int) -> list[dict[str, Any]]:
    """该文献的全部划痕（㉛），按块 + 起点排序 —— 渲染与前端都依赖这个顺序。"""
    return rows_to_list(conn.execute(
        "SELECT * FROM highlights WHERE paper_id=? ORDER BY block_id, start", (paper_id,)
    ).fetchall())


def create_highlight(conn: sqlite3.Connection, paper_id: int, *, block_id: str, lang: str,
                     start: int, end: int, color: str) -> dict[str, Any]:
    """新建划痕（自带事务）。语义见 `insert_highlight`。"""
    with tx(conn):
        return insert_highlight(conn, paper_id, block_id=block_id, lang=lang,
                                start=start, end=end, color=color)


def insert_highlight(conn: sqlite3.Connection, paper_id: int, *, block_id: str, lang: str,
                     start: int, end: int, color: str) -> dict[str, Any]:
    """新建划痕；与既有划痕重叠的部分**被新划痕切掉**（后划的优先）。

    为什么不允许重叠（而不是叠两层底色）：两种半透明色叠在一起会混出第三种颜色，
    而颜色本身**不带含义**，混色之后连"这是哪支笔"都答不上来；何况点"删除"时
    该删哪一条也说不清。而"再划一遍就是重划"是所有人对笔的直觉 —— 照直觉做。

    裁切有四种结局：完全覆盖 → 删；只剩左段 / 只剩右段 → 改端点；
    被新划痕从中间劈开 → 改端点 + 补一条右段（**沿用原来的颜色**，它不是新划的）。

    ⚠️ **调用方负责事务**（`with tx(conn)`）：`tx` 不可重入 —— 嵌一层会在内层就提交掉。
    "给选区写笔记时顺手补一道划痕"必须与笔记插入同一个事务，所以这里不能再自己开事务。
    """
    rows = conn.execute(
        "SELECT id, start, end, color FROM highlights"
        " WHERE paper_id=? AND block_id=? AND lang=?", (paper_id, block_id, lang)
    ).fetchall()
    for r in rows:
        s0, e0 = int(r["start"]), int(r["end"])
        if e0 <= start or s0 >= end:
            continue                          # 不相交
        left = (s0, start) if s0 < start else None
        right = (end, e0) if e0 > end else None
        if left and right:
            conn.execute("UPDATE highlights SET end=? WHERE id=?", (left[1], r["id"]))
            conn.execute(
                "INSERT INTO highlights (paper_id, block_id, lang, start, end, color,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (paper_id, block_id, lang, right[0], right[1], r["color"], utcnow()))
        elif left:
            conn.execute("UPDATE highlights SET end=? WHERE id=?", (left[1], r["id"]))
        elif right:
            conn.execute("UPDATE highlights SET start=? WHERE id=?", (right[0], r["id"]))
        else:
            conn.execute("DELETE FROM highlights WHERE id=?", (r["id"],))
    cur = conn.execute(
        "INSERT INTO highlights (paper_id, block_id, lang, start, end, color, created_at)"
        " VALUES (?,?,?,?,?,?,?)", (paper_id, block_id, lang, start, end, color, utcnow()))
    hl_id = int(cur.lastrowid)
    return dict(conn.execute("SELECT * FROM highlights WHERE id=?", (hl_id,)).fetchone())


def find_highlight(conn: sqlite3.Connection, paper_id: int, *, block_id: str, lang: str,
                   start: int, end: int) -> dict[str, Any] | None:
    """**正好**盖住这一段的划痕（两端全等才算；只是重叠不算）。"""
    row = conn.execute(
        "SELECT * FROM highlights WHERE paper_id=? AND block_id=? AND lang=? AND start=? AND end=?",
        (paper_id, block_id, lang, start, end),
    ).fetchone()
    return dict(row) if row else None


def ensure_highlight(conn: sqlite3.Connection, paper_id: int, *, block_id: str, lang: str,
                     start: int, end: int, color: str) -> dict[str, Any]:
    """这一段上**要有**一道划痕：已经有了就用它，没有才划一道。

    「给选区写笔记 → 顺手自动高亮」用这个（宿主 2026-09-13：*选中添加笔记时，应该同时自动高亮*）。
    为什么先查再建、而不是直接 `insert_highlight`：后者对**同区间**的处理是
    "删掉旧的、插一条新的" —— 于是这道划痕换了个 id，而**别的笔记还锚在旧 id 上**
    （它们的 `hl_id` 当场变成悬空引用，卡片上的颜色圆点也跟着没了）。
    前端正常路径会把已有那道划痕的 id 带上来，走到这里的是"这一段还没划过"的情形；
    但同一区间**已经**有一道时也必须认旧的那一道。

    ⚠️ **调用方负责事务**，理由同 `insert_highlight`。
    """
    return find_highlight(conn, paper_id, block_id=block_id, lang=lang, start=start, end=end) \
        or insert_highlight(conn, paper_id, block_id=block_id, lang=lang,
                            start=start, end=end, color=color)


def set_highlight_color(conn: sqlite3.Connection, hl_id: int, color: str) -> dict[str, Any] | None:
    with tx(conn):
        conn.execute("UPDATE highlights SET color=? WHERE id=?", (color, hl_id))
        row = conn.execute("SELECT * FROM highlights WHERE id=?", (hl_id,)).fetchone()
    return dict(row) if row else None


def delete_highlight(conn: sqlite3.Connection, hl_id: int) -> bool:
    """删划痕 —— 顺手把指向它的笔记**解绑**（笔记有自己的一份坐标，不该跟着消失）。

    笔记的锚点是它自己的 `(block_id, lang, start, end)`：划痕只是一个视觉上的兄弟。
    删划痕却把笔记一起删掉，等于"擦掉荧光笔 = 撕掉批注"，用户会认为是丢数据。
    """
    with tx(conn):
        conn.execute("UPDATE notes SET hl_id=NULL WHERE hl_id=?", (hl_id,))
        cur = conn.execute("DELETE FROM highlights WHERE id=?", (hl_id,))
    return cur.rowcount > 0
