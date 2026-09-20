"""块的**手写编辑**：插入 / 编辑 / 删除 —— 宿主 2026-09-20 的定义：

> 所以，实际上不是拆分，是可以在任意 block 的前面或者后面添加新的 block。同样，合并也不是
> 单独的操作。只要支持新 block 的插入、老 block 的编辑、老 block 的删除。就相当于实现了
> block 的手动拆分和合并。并且可以确保只影响当前 block 的笔记，其前后未动的 block 的笔记
> 可以不影响。

所以这里**刻意没有** `split` / `merge` 这两个动作：拆分 = 「在它后面插入一块」+「把切出去的字
从原块删掉（编辑）」；合并 = 「把下一块的字接进上一块（编辑）」+「删掉下一块」。两个组合都由
用户自己拼，程序不猜"这两段其实是一句话"（猜错就是替用户改了原文）。

## 三条不变量（每条都对应一种"会丢批注"的失败模式）

1. **只碰被编辑那一块的批注，前后邻居一条不动。**
   编辑走 `UPDATE blocks ... WHERE id=?`（不是整篇重写），邻居的块 id 与文字都没变
   ⇒ 钉在它们上面的 `(block_id, lang, start, end)` 天然不受影响。
   插入/删除必然让后续块的 `ord` 变化（阅读顺序），但批注锚的是**块 id**，
   而笔记列表的排序本来就该跟着原文位置走（㉟）。
2. **坐标不猜**（`remap_span`）：文字改了以后，被编辑块自己的批注用
   「最长公共前缀 + 最长公共后缀」做**确定性**映射 —— 这正是"剪切/粘贴"的形状
   （拆分 = 去掉尾巴、合并 = 接上尾巴，两端逐字未动）。落进被替换掉那段文字里的批注
   **不会被硬贴到相邻字符上**：划痕删掉，笔记**内容保留**并转成「文献级笔记」，
   连同它的原文摘录一起在返回值里报出来 —— 用户看得见丢了什么，最坏也只是重新划一次。
3. **绝不静默丢用户写的字**：删块时该块的笔记**一条都不删**（`content` 是用户自己写的，
   比它的锚点值钱），只把锚点清空；指向划痕的 `hl_id` 置 NULL（与 `delete_highlight`
   同一句语义：擦掉荧光笔 ≠ 撕掉批注）。

## 与「重新提取」的区别

`reextract` 会删掉整篇的笔记与划痕（重解析后块 id 与文本都变了）。本模块**永远不重编号**：
已有块 id 一个不改，新块取「现有最大号 + 1」。所以手写编辑在**有批注的篇目**上也是安全的
（这正是宿主 2026-09-19 以来一以贯之的那条硬不变量）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from ..pipeline.model import Block, Doc, make_block_id
from .db import dump_json, load_json, tx
from .repo import load_doc, write_doc

log = logging.getLogger("papershelf.blockops")

# ── 两个名单（宿主 2026-09-20：「图片说明的 block，也应该支持『编辑此块』和『重译此块』」）──
#
# 这两个名单必须分开，因为它们回答的是**两个不同的问题**：
#
# `TEXT_TYPES`：「这一块的文字在 `en`/`zh` 里吗？」⇒ 在 ⇒ 「编辑此块」开放。
#   图注块（`figure`）**在里面**：它的文字就是图注，正是 `en`/`zh` 两栏（图片本体另在
#   `payload["src"]`，与 `deco`/`table`/`eq` 那种"内容全在 payload"的块不是一回事）。
#   其余类型（`table`/`deco`/`eq`）的文字在 `payload`（网格 / 无字 / LaTeX 源码）——
#   拿 `en`/`zh` 去改它们改的是**看不见的东西**，所以不开放。
#   `h1` 不在其中：正文不产生 h1 块（`parse._finalize` 把 h1 收进论文标题，标题改元数据）。
TEXT_TYPES = ("p", "h2", "h3", "h4", "abstract", "refs", "ref", "figure")

# `NEW_TYPES`：**手工新建块或改类型**时可选的名字 ⇒ `figure` 不在其中。
#   一个块是不是图，取决于 `payload["src"]` 有没有图片（渲染端按它出 `<img>`）——
#   凭一个下拉框造出 `figure` 只会得到"有图注、没图"的空壳；反过来把正文改成图注
#   则会让图**从页面上消失**（图还在库里，只是没人渲染它了）。所以类型在两侧都不许跨。
NEW_TYPES = ("p", "h2", "h3", "h4", "abstract", "refs", "ref")

# 「重译此块」对哪些类型开放：**可能有中文**的块（`validate.expects_chinese` 可能为真，
# 具体还取决于文字本身）。名单外的是 `refs`（文献碎片，整块免中文 —— 给它按钮只会
# 点出一条 400）、`deco`（无字）、`eq`（公式）、`meta`/`h1`（正文不产生）。
# 表格与文献条目在名单里：它们各走自己的翻译通道（决策㊴/㊹），端点会连
# `payload` 里的网格 / 标题一起回写。
RETRANSLATE_TYPES = ("p", "h2", "h3", "h4", "abstract", "figure", "table", "ref")

_ID_RE = re.compile(r"^b-(\d+)$")


# ── 坐标映射 ───────────────────────────────────────────────────────────────
def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def remap_span(old: str, new: str, start: int, end: int) -> tuple[int, int] | None:
    """把 `old` 上的字符区间 `[start, end)` 映射到 `new`；**映射不了返回 `None`**。

    规则 = 最长公共前缀 + 最长公共后缀（**确定性**，且恰好是"剪切/粘贴"这类编辑的形状）：

    - 前缀之前、后缀之后的文字**逐字未动** ⇒ 偏移要么原位不动、要么整体平移；
    - 落在中间那段被替换掉的文字里 ⇒ `None`（那段字已经不存在了。硬贴到相邻字符上
      就是"笔记指到了别的字上"—— 比丢了更难发现，所以宁可让调用方把它报出来）。

    区间与"被替换掉的那段"相交时的裁剪：起点落进去 → 夹到接缝；终点落进去 → 也夹到接缝；
    两边都夹进去 ⇒ 区间已空 ⇒ `None`。
    """
    if old == new:
        return (start, end) if end > start else None
    p = _common_prefix(old, new)
    s = _common_suffix(old[p:], new[p:])
    o_end = len(old) - s                     # old 里被替换掉的那段是 [p, o_end)
    delta = (len(new) - s) - o_end           # 后缀整体平移量

    def f(x: int, *, is_start: bool) -> int | None:
        """把 old 上的位置 `x` 映射到 new。`is_start` 决定**接缝上那一点**归哪边。

        ⚠️ 接缝有两处精细之处，都是"看起来对、结果指到别的字上"：

        1. `x == p == o_end` 是**纯插入点**。区间**起点**落在这里 ⇒ 它属于被挤到右边去的那段
           （`Hello world` → `Hello brave world`，划住 `world` 的区间必须跟着右移；沿用
           `x < p` 判定会把它贴到新插进来的 `brave` 上）；区间**终点**落在这里 ⇒ 它属于
           左边未动的那段（合并时"把下一块接上来"，原块末尾一个字都不该跟着走）。
           同一条规则管两头就会把 `Hello` 映射成 `(0, 11)` —— 顺手划住了新插的字。
        2. `x >= o_end` 才是后缀平移 —— 判据必须是 `>=`：`o_end` 本身就是"旧文字已经用完"的位置。
        """
        if x < p or (x == p and o_end == p and not is_start):
            return x
        if x >= o_end:
            return x + delta
        return None

    a = f(start, is_start=True)
    b = f(end, is_start=False)
    a = p if a is None else a
    b = p if b is None else b
    a = max(0, min(a, len(new)))
    b = max(0, min(b, len(new)))
    return (a, b) if b > a else None


# ── 批注的重新锚定（只作用于**这一块**）────────────────────────────────────
def _remap_block_anchors(conn: sqlite3.Connection, paper_id: int, block_id: str,
                         lang: str, old: str, new: str) -> dict[str, Any]:
    """把**这一块这一语言**上的划痕/笔记按 `remap_span` 挪到新文字上。

    调用方负责事务（`tx` 不可重入）。返回一份账：`moved` / `dropped`（每项带被删除的原文）。
    - 划痕映射不了 ⇒ **删除**（它只有坐标，文字没了就没有意义）；
      指向它的笔记 `hl_id` 置 NULL（笔记自己还有一份坐标，不该跟着消失，同 `delete_highlight`）；
    - 笔记映射不了 ⇒ **保留内容**，锚点清空成「文献级笔记」，并把它原本划住的原文写进 `quote`
      （用户据此能找回位置）。整块锚（`start IS NULL`）不需要动 —— 它指的是"这一块"。
    """
    moved = 0
    dropped: list[dict[str, Any]] = []

    rows = conn.execute(
        "SELECT id, start, end, color FROM highlights"
        " WHERE paper_id=? AND block_id=? AND lang=?", (paper_id, block_id, lang)
    ).fetchall()
    for r in rows:
        span = remap_span(old, new, int(r["start"]), int(r["end"]))
        if span is None:
            conn.execute("UPDATE notes SET hl_id=NULL WHERE hl_id=?", (r["id"],))
            conn.execute("DELETE FROM highlights WHERE id=?", (r["id"],))
            dropped.append({"kind": "highlight", "id": r["id"],
                            "quote": old[int(r["start"]):int(r["end"])]})
            continue
        conn.execute("UPDATE highlights SET start=?, end=? WHERE id=?", (span[0], span[1], r["id"]))
        moved += 1

    notes = conn.execute(
        "SELECT id, start, end, quote FROM notes"
        " WHERE paper_id=? AND block_id=? AND lang=?", (paper_id, block_id, lang)
    ).fetchall()
    for r in notes:
        if r["start"] is None or r["end"] is None:
            continue                                   # 整块锚：跟着块走，不用改
        span = remap_span(old, new, int(r["start"]), int(r["end"]))
        if span is None:
            quote = r["quote"] or old[int(r["start"]):int(r["end"])]
            conn.execute(
                "UPDATE notes SET block_id=NULL, lang=NULL, start=NULL, end=NULL, hl_id=NULL,"
                " quote=? WHERE id=?", (quote, r["id"]))
            dropped.append({"kind": "note", "id": r["id"], "quote": quote})
            continue
        conn.execute("UPDATE notes SET start=?, end=? WHERE id=?", (span[0], span[1], r["id"]))
        moved += 1

    return {"moved": moved, "dropped": dropped}


# ── 编辑已有块 ─────────────────────────────────────────────────────────────
def edit_block(conn: sqlite3.Connection, paper_id: int, block_id: str, *,
               en: str | None = None, zh: str | None = None, type: str | None = None,
               reconciled: bool = False) -> dict[str, Any]:
    """改**一个块**的文字（`en` / `zh`）与类型。`None` = 这个字段不动。

    语义（每条的"为什么"）：

    - 传了 `zh` ⇒ `zh_source='human'`（⑯：重跑不得覆盖人工修订），与块级修订一致；
      带 `reconciled` ⇒ 顺手撤掉「待校对」。
    - **只改了原文（`en`）而没给译文** ⇒ 老译文与新原文已经对不上了：保留它（用户可能还要
      自己改），但挂上「待校对」（`needs_review`）—— 这是**如实**标记，而不是悄悄留一段对不上的中文。
    - **两栏各按自己的坐标系重新锚定**（`en` 的变化只动 `lang='en'` 的批注，`zh` 的变化只动
      `lang='zh'` 的）：中英没有字级对应，按比例换算就是猜（㉛ 的同一条理由）。
    - **图注块（`figure`）照样能编辑**（宿主 2026-09-20：「图片说明的 block，也应该支持
      『编辑此块』和『重译此块』」）—— 它的文字就是 `en`/`zh` 两栏的图注。改原文时
      `payload["caption"]` 一起跟着改（那是解析/①c 读的原始记录，不写会分叉）；但**类型不许改**
      （图之所以是图，取决于 `payload["src"]`）。
    """
    row = conn.execute("SELECT * FROM blocks WHERE paper_id=? AND id=?",
                       (paper_id, block_id)).fetchone()
    if row is None:
        return {"ok": False, "code": "not_found", "reason": "块不存在"}
    if en is None and zh is None and type is None:
        return {"ok": False, "code": "bad_args", "reason": "没有要修改的内容"}
    if type is not None and type not in NEW_TYPES:
        return {"ok": False, "code": "bad_args", "reason": f"类型只能是 {'/'.join(NEW_TYPES)}"}
    if type is not None and (row["type"] or "") == "figure":
        # 图注块的类型不能改：它是图**因为它有 `payload["src"]`**，不是因为名字叫 figure。
        # 允许改成 `p` 的后果是图**从页面上消失**（图还在库里，只是没有渲染分支认它了）——
        # 一种"保存成功但内容没了"的静默损失，与批注那三条不变量同一个取向：宁可拒绝。
        return {"ok": False, "code": "bad_args",
                "reason": "图注块的类型不能改（图片由 payload.src 决定）；要改文字直接改图注"}

    old_en, old_zh = row["en"] or "", row["zh"] or ""
    new_en = old_en if en is None else en
    new_zh = old_zh if zh is None else zh
    payload = load_json(row["payload"], {})

    if new_en != old_en and (row["type"] or "") == "figure":
        # ⚠️ 图注的**原文有两个记录位**，手改必须一起写，否则会分叉：
        #   · `en` = 渲染与翻译判定读的那一份（`markup.render_block`、`expects_chinese`）；
        #   · `payload["caption"]` = 解析时的原始记录，**①c 校对 agent 读它**
        #     （`proofread` 里 `b.payload.get("caption")`），`zh` 为空时渲染也回落它。
        # 只改 `en` 的表现是"中文栏还显示改之前那句英文图注"（回落取的是旧 caption）。
        payload["caption"] = new_en

    # ⚠️ `sets` 与 `args` **必须同序构建**：先把 payload 的所有增删定下来，再按
    #    `SET a=?, b=?` 的字面顺序填参数。分开写（先攒 sets 再 append args）会在
    #    "只有部分列要改"时错位 —— `payload` 会拿到 `"human"`、`zh_source` 会拿到一段 JSON，
    #    且**不报错**（列值类型都是 TEXT），测试量到"zh_source 变成了一串 JSON"才发现。
    if zh is not None and reconciled:
        payload.pop("needs_review", None)              # 「确认无误」→ 撤掉待校对
    if new_en != old_en and zh is None and new_zh.strip():
        # 只改原文、译文没跟着改 ⇒ 两者已经对不上：保留老译文（用户可能还要自己改），
        # 但**如实**挂上「待校对」。悄悄留一段对不上的中文比标出来坏得多。
        payload["needs_review"] = True

    sets = ["en=?", "zh=?", "payload=?"]
    args: list[Any] = [new_en, new_zh, dump_json(payload)]
    if zh is not None:
        sets.append("zh_source=?")
        args.append("human")
    if type is not None and type != (row["type"] or ""):
        sets.append("type=?")
        args.append(type)

    moved = 0
    dropped: list[dict[str, Any]] = []
    with tx(conn):
        if new_en != old_en:
            acc = _remap_block_anchors(conn, paper_id, block_id, "en", old_en, new_en)
            moved += acc["moved"]
            dropped += acc["dropped"]
        if new_zh != old_zh:
            acc = _remap_block_anchors(conn, paper_id, block_id, "zh", old_zh, new_zh)
            moved += acc["moved"]
            dropped += acc["dropped"]
        conn.execute(f"UPDATE blocks SET {', '.join(sets)} WHERE paper_id=? AND id=?",
                     args + [paper_id, block_id])

    log.info("  · 编辑块 %s：原文 %d→%d 字、译文 %d→%d 字；批注搬迁 %d、作废 %d",
             block_id, len(old_en), len(new_en), len(old_zh), len(new_zh), moved, len(dropped))
    return {"ok": True, "id": block_id, "anchors_moved": moved, "anchors_dropped": dropped,
            "zh_stale": bool(new_en != old_en and zh is None and new_zh.strip())}


# ── 插入新块 ───────────────────────────────────────────────────────────────
def _fresh_id(doc: Doc) -> str:
    """取一个没用过的块 id = **现有最大号 + 1**（绝不重编号已有块 —— 批注锚的就是它）。"""
    used = {b.id for b in doc.blocks}
    n = 0
    for b in doc.blocks:
        m = _ID_RE.match(b.id)
        if m:
            n = max(n, int(m.group(1)))
    while True:
        n += 1
        cand = make_block_id(n)
        if cand not in used:
            return cand


def insert_block(conn: sqlite3.Connection, paper_id: int, *,
                 after: str | None = None, before: str | None = None,
                 en: str = "", zh: str = "", type: str | None = None,
                 level: int | None = None) -> dict[str, Any]:
    """在 `after` 之后（或 `before` 之前）插入一个新块；返回新块。

    新块**继承参照块的 `section` 与 `payload.page`**（否则它会掉出 PDF 分页，
    跑到"无页码"的那一组里去），但**不继承** `band`/`rule`/`shade`/`seam` ——
    那四戳描述的是"这一行在 PDF 上的版面位置"与"它与前一块的关系"，手写的新块没有这回事。
    类型默认跟着参照块（标题后面插一个标题最自然），参照块不是文字块时退回 `p`。
    `zh` 为空 ⇒ 挂「待校对」（这一段还没有中文），前端可以就地「重译此块」。
    """
    doc = load_doc(conn, paper_id)
    if doc is None:
        return {"ok": False, "code": "no_doc", "reason": "该文献没有产物（转换尚未完成）"}
    if type is not None and type not in NEW_TYPES:
        return {"ok": False, "code": "bad_args", "reason": f"类型只能是 {'/'.join(NEW_TYPES)}"}
    idx = None
    for key, value, delta in (("after", after, 1), ("before", before, 0)):
        if value is None:
            continue
        at = next((i for i, b in enumerate(doc.blocks) if b.id == value), None)
        if at is None:
            return {"ok": False, "code": "not_found", "reason": f"没有块 {value}"}
        idx = at + delta
    if idx is None:
        return {"ok": False, "code": "bad_args", "reason": "必须指定 after 或 before（在哪一块的前/后插入）"}

    ref = doc.blocks[idx - 1] if idx > 0 else doc.blocks[idx]
    # ⚠️ 用 `NEW_TYPES`（不是 `TEXT_TYPES`）：在图注块旁边插入时**不能**跟着它变成 `figure`
    # —— 新块没有 `payload["src"]`，只会渲染成"有字没图"的空壳（见上面两个名单的说明）。
    typ = type or (ref.type if ref.type in NEW_TYPES else "p")
    level_ = ref.level if level is None else level
    if not typ.startswith("h"):
        level_ = 0
    page = (ref.payload or {}).get("page")
    payload: dict[str, Any] = {"page": page} if isinstance(page, int) else {}
    if not zh.strip():
        payload["needs_review"] = True
    blk = Block(id=_fresh_id(doc), type=typ, level=level_, section=ref.section,
                en=en, zh=zh, zh_source="human" if zh.strip() else "none", payload=payload)
    doc.blocks.insert(idx, blk)
    with tx(conn):
        write_doc(conn, paper_id, doc)
    log.info("  · 插入块 %s（%s 之后，%d 字/%d 字）", blk.id, ref.id, len(en), len(zh))
    return {"ok": True, "id": blk.id, "after": ref.id if idx > 0 else None,
            "before": None if idx > 0 else doc.blocks[idx + 1].id}


# ── 删除块 ─────────────────────────────────────────────────────────────────
def delete_block(conn: sqlite3.Connection, paper_id: int, block_id: str) -> dict[str, Any]:
    """删掉一个块。**笔记一条都不删**（见模块开头的不变量 3），划痕随块一起收。

    返回值把这三种后果分开报出来（`notes_unanchored` / `highlights_deleted` / `quotes`）——
    合并 = 编辑 + 删除，用户需要知道"删掉的那块上原本有 2 条笔记，已变成文献级笔记"。
    """
    doc = load_doc(conn, paper_id)
    if doc is None:
        return {"ok": False, "code": "no_doc", "reason": "该文献没有产物（转换尚未完成）"}
    blk = next((b for b in doc.blocks if b.id == block_id), None)
    if blk is None:
        return {"ok": False, "code": "not_found", "reason": "块不存在"}

    notes = conn.execute(
        "SELECT id, start, end, quote FROM notes WHERE paper_id=? AND block_id=?",
        (paper_id, block_id)).fetchall()
    with tx(conn):
        hls = conn.execute("SELECT id FROM highlights WHERE paper_id=? AND block_id=?",
                           (paper_id, block_id)).fetchall()
        for r in hls:
            conn.execute("UPDATE notes SET hl_id=NULL WHERE hl_id=?", (r["id"],))
        conn.execute("DELETE FROM highlights WHERE paper_id=? AND block_id=?", (paper_id, block_id))
        quotes: list[str] = []
        for n in notes:
            text = (n["quote"] or ((blk.en or "")[int(n["start"]):int(n["end"])]
                                  if n["start"] is not None and n["end"] is not None else ""))
            conn.execute(
                "UPDATE notes SET block_id=NULL, lang=NULL, start=NULL, end=NULL, hl_id=NULL,"
                " quote=? WHERE id=?", (text or None, n["id"]))
            if text:
                quotes.append(text[:80])
        doc.blocks = [b for b in doc.blocks if b.id != block_id]
        write_doc(conn, paper_id, doc)
    log.info("  · 删除块 %s：划痕 %d 条随块收走，笔记 %d 条转为文献级（内容保留）",
             block_id, len(hls), len(notes))
    return {"ok": True, "id": block_id, "notes_unanchored": len(notes),
            "highlights_deleted": len(hls), "quotes": quotes}
