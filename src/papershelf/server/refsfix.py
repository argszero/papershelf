"""「修复参考文献」——在**已经落库的产物**上重跑条目合并（解析 v14 的存量出口）。

## 为什么需要它（而不是「重新提取」）

解析 v14 修掉了「参考文献只有第一段能合并」（连号判据原先钉死起点 = 1，而跨页的
页眉/页脚会把文献流截成好几段 ⇒ 只有含 `[1]` 的那一段成功）。但**存量产物不会自己
变好**：`doc_cache` 的指纹混了解析版本号，而它只在**下一次转换**时才会被查到。

原来的唯一出口是「重新提取」，可那对**已经读过并做了批注**的篇目不可接受：
`reextract` 会删掉 `notes` / `highlights`（块 id 与文本都会变，留着只会指到别的字上，
比"没了"更坏）。宿主 2026-09-19 原话：

> push 上生产，生产上你第一篇 pdf 我已经添加了不少笔记了，想办法在不影响笔记的情况下，
> 可以手动改一下后面的 References？

## 这个操作做什么（按顺序）

1. `parse.rebuild_ref_entries` 重跑**文末材料段**的碎片合并 —— **正文块一个字节不动**；
2. 新 `ref` 块取「现有最大号 + k」的新 id ⇒ **正文块 id 全部不变**，钉在正文上的
   笔记/划痕天然不受影响（它们在参考文献之前，`blocks.ord` 也不变）；
3. 钉在**被合并掉的那些碎片**上的笔记/划痕，按 `parse.RefSpan` 换算到新条目上
   （`_move_anchors`：块 id + 字符区间，中文侧还要过一遍"标题被换成中文"的长度差）；
4. 只翻译**新增的 `ref` 块**（第三条通道，只译标题）—— 已有译文一律不碰
   （`zh_source == "human"` 的人工修订更不会）；
5. **块 + 批注 + `docs` 元数据在同一个事务里提交**（`repo.write_doc` + `tx`）：
   崩在中途也不会留下"锚点指向已删块"的半成品。

## 边界

- 只对**切得出条目**的碎片段动手；切不出的（无编号 / APA 体例）**原样返回**，
  所以本操作对它们是无操作，不是"清掉重来"；
- 幂等：合并过一次之后再跑，`refs` 段已经没有了 ⇒ 块序列一模一样 ⇒ 直接跳过
  （不落库、不调模型，返回 `changed=False`）；
- 不动 `doc_cache`、不动 `PARSE_VERSION`、不动 PDF —— 下次「重新提取」会按新解析器
  重做一遍，那是另一条路，两者互不干扰。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Callable

from ..pipeline.model import Block
from ..pipeline.parse import RefSpan, rebuild_ref_entries
from ..pipeline.translator import Translator, title_span
from ..pipeline.validate import expects_chinese
from .db import tx
from .repo import load_doc, write_doc

log = logging.getLogger("papershelf.refsfix")


def rebuild_paper_refs(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    translate: bool = True,
    translator: Any | None = None,
    log: Callable[..., None] = print,
) -> dict[str, Any]:
    """把一篇文献的文末参考文献**就地**重建成整条 `ref` 条目；批注跟着搬家。

    返回一份可打印的账（`changed` / `entries` / `anchors_moved` / `tokens` …）；
    `changed=False` 表示无事可做（没有可合并的碎片，或已经修过）。
    """
    doc = load_doc(conn, paper_id)
    if doc is None:
        return {"ok": False, "reason": "该文献没有产物（转换尚未完成）"}

    before = _count(doc.blocks)
    if before["refs"] == 0:
        return {"ok": True, "changed": False, "reason": "文末材料段没有可合并的碎片",
                **before, "entries": before["ref"], "tokens": 0,
                "refs_left": 0, "anchors_moved": 0, "anchors_dropped": 0}

    # ⚠️ `from_index=0`：不依赖 `meta["refs_start"]`（旧产物未必有），
    #    非 `refs` 块一律原样透传 ⇒ 与"只处理文末段"等价，且对解析期没识别出
    #    文末材料的篇目也照样能修（那正是"没修好"的一种成因）。
    blocks, moves = rebuild_ref_entries(doc.blocks, 0)
    if [b.id for b in blocks] == [b.id for b in doc.blocks]:
        return {"ok": True, "changed": False, "reason": "已经修过（没有可再合并的碎片）",
                **before, "entries": before["ref"], "tokens": 0,
                "refs_left": before["refs"], "anchors_moved": 0, "anchors_dropped": 0}

    after = _count(blocks)
    new_ids = [b.id for b in blocks if b.id not in {x.id for x in doc.blocks}]
    log(f"  · 碎片 {before['refs']} → 条目 {after['ref'] - before['ref']} 条"
        f"（新块 {len(new_ids)} 个；余下未切出的碎片 {after['refs']} 个）")

    tokens = 0
    if translate and new_ids:
        tr = translator if translator is not None else _make_translator(conn, paper_id)
        if tr is None:
            log("  ⚠️ 未配置 LLM / 拿不到术语表 → 只重排结构，**不翻译**（新条目将没有中文）")
        else:
            texts = tr.translate_blocks(blocks, only=set(new_ids), log=log)
            # `_translate_ref` 已经把中文写进 `b.zh`；这里只补 `zh_source`
            # —— 与 `converter` 的收尾同一套（`mt` = 机器译文，⑯ 的人工修订据此豁免）。
            for b, text in zip(tr.ordered(blocks), texts):
                if b.id in new_ids and text and b.zh_source != "human":
                    b.zh, b.zh_source = text, ("mt" if expects_chinese(b.en, block_type=b.type)
                                               else "none")
            tokens = int(getattr(tr, "tokens_used", 0) or 0)
            bad = set(getattr(tr, "needs_review", None) or []) & set(new_ids)
            for b in blocks:
                if b.id in bad:
                    b.payload["needs_review"] = True
            done = sum(1 for b in blocks if b.id in new_ids and (b.zh or "").strip())
            log(f"  · 新条目译文 {done}/{len(new_ids)} 条（待校对 {len(bad)} 条）")

    doc.blocks = blocks
    if "refs_count" in doc.meta:
        doc.meta["refs_count"] = after["ref"] + after["refs"]
    by_id = {b.id: b for b in blocks}
    with tx(conn):
        moved, dropped = _move_anchors(conn, paper_id, moves, by_id, log=log)
        write_doc(conn, paper_id, doc)
    log(f"  · 批注搬家：{moved} 条已随碎片搬到新条目，{dropped} 条落空（已记日志）")
    # ⚠️ `**before` 里的 `refs` 是**动手之前**的碎片数；日志要的是"还剩几个没切出来"
    #    ⇒ 另给一个 `refs_left`。2026-09-19 在生产日志里实测到过这条口径错误：
    #    它把 309（修之前）印成「余下未切出的碎片」，而真实剩余是 14 —— 这种错会让人
    #    以为"修了等于没修"，比不打印更坏。
    return {"ok": True, "changed": True, **before, "entries": after["ref"], "tokens": tokens,
            "refs_left": after["refs"], "anchors_moved": moved, "anchors_dropped": dropped}


def _count(blocks: list[Block]) -> dict[str, int]:
    return {"refs": sum(1 for b in blocks if b.type == "refs"),
            "ref": sum(1 for b in blocks if b.type == "ref")}


def _make_translator(conn: sqlite3.Connection, paper_id: int) -> Translator | None:
    """按**计划级术语表**（决策⑫）造一个翻译器 —— 与转换路径同源。"""
    import json

    from ..pipeline.translator import LLMConfig

    row = conn.execute("SELECT plan_id FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        return None
    cfg = LLMConfig.from_env()
    if not cfg.api_key:
        return None
    g = conn.execute("SELECT glossary FROM plan_settings WHERE plan_id=?",
                     (row["plan_id"],)).fetchone()
    return Translator(cfg, json.loads(g["glossary"]) if g and g["glossary"] else [])


# ── 批注搬家 ──────────────────────────────────────────────────────────────
def _move_anchors(conn: sqlite3.Connection, paper_id: int,
                  moves: dict[str, list[RefSpan]], by_id: dict[str, Block],
                  *, log: Callable[..., None] = print) -> tuple[int, int]:
    """把钉在**被合并掉的碎片**上的笔记/划痕搬到新条目上。

    坐标语义（㉛）：`(block_id, lang, start, end)`，`start/end` 是**裸文本**字符偏移。
    - `lang == "en"` → 直接过 `RefSpan` 的空白归一化映射（精确）；
    - `lang == "zh"` → 免中文碎片的中文栏是**原文回落**，所以先在英文坐标系里定位，
      再按"标题那一段换成了中文"的长度差平移（`_to_zh`）。没译出来的条目坐标不变。
    - 整块锚（`start IS NULL`）→ 只换块 id，落到覆盖碎片开头的那一条上。
    """
    moved = dropped = 0
    for table in ("notes", "highlights"):
        rows = conn.execute(
            f"SELECT id, block_id, lang, start, end FROM {table} WHERE paper_id=?",
            (paper_id,)).fetchall()
        for r in rows:
            spans = moves.get(r["block_id"])
            if not spans:
                continue
            if r["start"] is None or r["end"] is None:
                who = _span_for(spans, 0)
                if who is None:
                    dropped += 1
                    continue
                conn.execute(f"UPDATE {table} SET block_id=? WHERE id=?",
                             (who.new_id, r["id"]))
                moved += 1
                continue
            who = _span_for(spans, r["start"])
            if who is None:
                dropped += 1
                log(f"    ⚠️ {table}#{r['id']} 的锚点落在碎片之外（块 {r['block_id']} 已合并）→ 未搬家")
                continue
            s, e = who.move(int(r["start"]), int(r["end"]))
            if r["lang"] == "zh":
                s, e = _to_zh(by_id.get(who.new_id), s, e)
            conn.execute(f"UPDATE {table} SET block_id=?, start=?, end=? WHERE id=?",
                         (who.new_id, s, e, r["id"]))
            moved += 1
            log(f"    · {table}#{r['id']}：{r['block_id']} {r['lang']} {r['start']}–{r['end']}"
                f" → {who.new_id} {s}–{e}")
    return moved, dropped


def _span_for(spans: list[RefSpan], offset: int) -> RefSpan | None:
    """碎片内偏移 `offset` 落在哪个新块里（都不命中就退到最靠后那个起点）。"""
    best: RefSpan | None = None
    for sp in spans:
        if sp.start <= sp.base + offset < sp.end:
            return sp
        if sp.base <= sp.base + offset and (best is None or sp.start > best.start):
            best = sp
    return best if best is not None else (spans[0] if spans else None)


def _to_zh(b: Block | None, s: int, e: int) -> tuple[int, int]:
    """英文坐标系区间 → 中文栏坐标系（标题换成中文后长度变了）。

    `ref` 块的中文 = 原条目里**把标题那一段换成中文**（`ref_zh_text`），所以映射恰好是
    一条"分段平移"：标题之前不动、标题之后平移 `len(title_zh) - len(title_en)`，
    落进标题里的字符夹到标题开头。没译出来（中文栏回落原文）时坐标原样返回。
    """
    if b is None or not (b.zh or "").strip():
        return s, e
    pay = b.payload or {}
    t_en, t_zh = pay.get("title_en") or "", pay.get("title_zh") or ""
    span = title_span(b.en, t_en) if t_en else None
    if span is None:
        return s, e
    a, z = span
    tail = b.en[z - 1] if 0 < z <= len(b.en) and b.en[z - 1] in ".。" else ""
    delta = len(t_zh) + len(tail) - (z - a)

    def f(p: int) -> int:
        if p <= a:
            return p
        if p >= z:
            return min(p + delta, len(b.zh))
        return a

    ns, ne = f(s), f(e)
    return ns, max(ne, ns)
