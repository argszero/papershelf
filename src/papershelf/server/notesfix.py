"""「修复小字注区」—— 解析 v16 的存量出口（**批注一条不丢**，2026-09-21）。

## 为什么要它（而不是「重新提取」）

解析 v16 修掉了小字注区（表注 / 脚注 / 作者单位）被当正文渲染的四件事：六行被
`" ".join(text.split())` 拼成一段、整块字号比正文小这个事实没记、行首的上标小标被拉平、
注上那条**页中**横线没照抄（宿主 2026-09-21 截图：「这一段提取的不对」「主要是样式不对」）。

但**存量产物不会自己变好**：`doc_cache` 的指纹混了解析版本号，而它只在**下一次转换**时
才会被查到。原来的唯一出口「重新提取」会 `DELETE FROM notes/highlights` —— 注意是**整篇**
的批注都没了（不是"注区那两块"的），对已经读过并做了批注的篇目不可接受
（宿主 2026-09-19 立的规矩：「想办法在不影响笔记的情况下……」，2026-09-21 再次授权）。

## 这个操作做什么（按顺序）

1. **重跑一次解析**（CPU、零 token、零落库）拿注区的新形状 —— 判据要用 PDF 的**行**与
   字号（库里只存块文本，块 bbox 只对图块保留），所以这一步必须回到 PDF 原件；
2. 把新解析出来的注区块**配回库里现有的块**：**同页 + 去掉空白后文字逐字相同**
   （这条判据恰好就是"只动空白"的同义词；认不出、或匹配到多个 ⇒ **跳过并记日志**，绝不猜）；
3. **只改写这几个块**（`en` + `payload["note"]/["markers"]/["rule"]`）：块 id 不变
   ⇒ 别的块上的批注天然不受影响（`blocks.ord` 也不变）；
4. 钉在被改写块上的批注按 `blockops.remap_span_ws`（只动空白的映射）搬家 ——
   中文侧是**重新翻译**，新旧译文没有字级对应，按"锚点落空"处置（划痕删、笔记转文献级
   且保住内容）；详见 `_apply` 里的注释；
5. 只翻译这几个块（≈1.5k tokens/块，全库命中 2 处）—— 译不出就**保留旧译文**并挂
   「待校对」（旧译文与原文只差空白，读起来照样对，不该因为重译失败而丢掉中文）；
6. **块 + 批注在同一个事务里提交**（`tx` 不可重入；崩在中途不会留下"锚点指向别的字"）。

## 边界

- 幂等：第二次跑时库里的 `en` 已与新解析逐字相同 ⇒ `changed=False`，不落库、不调模型；
- 不动 `doc_cache`、不动 PDF、不涨 `PARSE_VERSION`（解析器自己的版本号是另一条路，互不干扰）；
- 不做「反向」修复：新解析认为不是注区的块，即使库里带着 `note` 戳也不动它
  （本工具只做"补上注区"，不做"撤销注区"——撤销没有对应的存量病灶）。
"""

from __future__ import annotations

import dataclasses
import logging
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..pipeline.model import Block
from ..pipeline.parse import parse_pdf
from ..pipeline.validate import expects_chinese
from . import blockops
from .db import dump_json, tx
from .repo import load_doc
# ⚠️ 复用「修复参考文献」那条路上的同一个翻译器工厂（按**计划级术语表**构造，与转换路径
#    同源）—— 再写一份，两处的术语表口径迟早漂开（决策⑫）。
from .refsfix import _make_translator as _plan_translator

log = logging.getLogger("papershelf.notesfix")

_WS_RE = re.compile(r"\s+")


def tight(text: str | None) -> str:
    """去掉**所有**空白后的文本 —— 判"这两块是不是同一块（只差了空白）"的钥匙。"""
    return _WS_RE.sub("", text or "")


def _page(b: Block) -> Any:
    return (b.payload or {}).get("page")


def _fresh_blocks(pdf: Path) -> list[Block]:
    """重跑一次解析拿注区的新形状（CPU、零 token、零落库）。

    ⚠️ 落在**临时目录**里：`parse_pdf` 会写图片资产与（页面旋转时的）`normalized.pdf`；
    往真的 `p<id>/assets` 里写只会白覆盖一份已经落库的产物 —— 整份丢弃。
    """
    with tempfile.TemporaryDirectory(prefix="ps-notesfix-") as td:
        doc = parse_pdf(pdf, assets_dir=Path(td) / "assets")
    return list(doc.blocks)


def _match(blocks: list[Block], fresh: list[Block],
           *, log: Callable[..., None] = print) -> list[tuple[Block, Block]]:
    """新解析出的注区块 → 库里对应的块（同页 + 去空白后文字相同）。

    ⚠️ 判据不按块 id 配：库里的 id 是**当年那次解析**发的（参考文献重建会发新号、
    旧碎片被删，号会留下空洞），拿它当钥匙等于假设"两次解析的编号一模一样"。
    文字（去空白）才是这件事的真钥匙 —— 而它刚好与"只动空白"这个前提同义。
    """
    index: dict[tuple[Any, str], list[Block]] = {}
    for b in blocks:
        index.setdefault((_page(b), tight(b.en)), []).append(b)
    pairs: list[tuple[Block, Block]] = []
    for nb in fresh:
        if not isinstance((nb.payload or {}).get("note"), dict):
            continue
        cands = index.get((_page(nb), tight(nb.en)), [])
        if len(cands) != 1:
            log(f"    ⚠️ 注区块 {nb.id}（第 {_page(nb)} 页）在库里匹配到 {len(cands)} 个块"
                f" → 跳过（不改、不猜）")
            continue
        old = cands[0]
        if (old.en or "") == nb.en:
            continue                              # 库里已经是新形状 ⇒ 幂等
        pairs.append((old, nb))
    return pairs


def rebuild_paper_notes(
    conn: sqlite3.Connection,
    paper_id: int,
    *,
    translate: bool = True,
    translator: Any | None = None,
    dry_run: bool = False,
    log: Callable[..., None] = print,
) -> dict[str, Any]:
    """把一篇文献里的**小字注区**就地补成 v16 的形状；批注跟着搬家（见模块文档）。"""
    doc = load_doc(conn, paper_id)
    if doc is None:
        return {"ok": False, "reason": "该文献没有产物（转换尚未完成）"}
    row = conn.execute("SELECT pdf_path FROM papers WHERE id=?", (paper_id,)).fetchone()
    pdf = Path(row["pdf_path"]) if row is not None and row["pdf_path"] else None
    if pdf is None or not pdf.exists():
        return {"ok": False, "reason": f"找不到 PDF 原件（{pdf}）—— 重跑解析需要它"}

    fresh = _fresh_blocks(pdf)
    pairs = _match(doc.blocks, fresh, log=log)
    counts = {"notes": len(pairs), "anchors_moved": 0, "anchors_dropped": 0, "tokens": 0}
    if not pairs:
        return {"ok": True, "changed": False, "reason": "没有需要修的小字注区（或已经修过）",
                **counts}
    if dry_run:
        for old, nb in pairs:
            log(f"    · {old.id}（第 {_page(nb)} 页）：{len(old.en)} 字 → "
                f"{len(nb.en.split(chr(10)))} 行、字号比 {nb.payload['note']['em']}、"
                f"标记 {nb.payload['markers']}")
        return {"ok": True, "changed": False, "dry_run": True, **counts}

    # ── 翻译（只翻这几个块）──────────────────────────────────────────────
    texts: dict[str, str] = {}
    bad: set[str] = set()
    tokens = 0
    if translate:
        tr = translator if translator is not None else _plan_translator(conn, paper_id)
        if tr is None:
            log("  ⚠️ 未配置 LLM → 只修结构：中文栏保留旧译文（它只差空白，读起来照样对）")
        elif not any(o.zh_source != "human" for o, _ in pairs):
            log("  · 需要修的注区全部是**人工修订**过的块 → 不调模型（保持人手写的那段中文）")
        else:
            # ⚠️ **人工修订过的块不送模型**（决策⑯：重跑不得冲掉人工修订；与 `refsfix`
            #    的「已有译文与 `zh_source='human'` 不碰」同一取向）。注区块这次变的是
            #    空白与渲染戳，**内容没变** ⇒ 人写的那段中文依然对；把它重译一遍不但
            #    白花钱，还会把人手写的东西换成机翻，并顺手把「已人工修订」标记降级成 `mt`
            #    （㊽ 修过同型缺陷：跑了半天没变化，标记却没了）。旧译文不是按行分的也**不改** ——
            #    "中文栏按同样行分"是给机器译文的结构要求，手写的怎么断句是人的事。
            #    这里不送 ⇒ 下面 `got is None` ⇒ 保留旧中文与来源，中文锚点也一并不动。
            tmp = [dataclasses.replace(o, en=n.en, zh="", zh_source="none")
                   for o, n in pairs if o.zh_source != "human"]
            ids = {b.id for b in tmp}
            out = tr.translate_blocks(tmp, only=ids, log=log)
            for b, t in zip(tr.ordered(tmp), out):
                if (t or "").strip():
                    texts[b.id] = t
            bad = set(getattr(tr, "needs_review", None) or []) & ids
            tokens = int(getattr(tr, "tokens_used", 0) or 0)
            log(f"  · 译文 {len(texts)}/{len(tmp)} 块（待校对 {len(bad)}）"
                f"，{tokens} tokens")

    # ── 落库（块 + 批注同一次事务）────────────────────────────────────────
    moved = dropped = 0
    with tx(conn):
        for old, nb in pairs:
            old_en, old_zh = old.en or "", old.zh or ""
            got = texts.get(old.id)               # None = 没译（未配置 LLM / 该块译失败）
            new_zh = got or old_zh                # 译不出 → 保留旧译文（同一段内容，只差空白）
            # 英文侧：只动空白 ⇒ 用 `remap_span_ws`（"映射不了"的三条政策与编辑块一字不差）
            acc = blockops.remap_block_anchors(conn, paper_id, old.id, "en", old_en, nb.en,
                                              mapper=blockops.remap_span_ws)
            moved += acc["moved"]
            # ⚠️ `blockops.remap_block_anchors` 回的是**明细表**（每条带被删的原文），
            #    与 `refsfix._move_anchors` 回的计数不是一个东西 —— 这里要的是条数。
            dropped += len(acc["dropped"])
            if got:
                # 中文侧是**重新翻译**：新旧译文没有字级对应。这里**故意传空串**把所有
                # 中文锚点送进"映射不了"那一支（划痕删、笔记转文献级且留住内容）——
                # 硬用 `remap_span` 会在两段中文里找到一小截公共前缀（都从 `a ` 起），
                # 于是笔记被贴到**别的字**上，比丢了更难发现（㉛ 的同一条理由）。
                acc = blockops.remap_block_anchors(conn, paper_id, old.id, "zh", old_zh, "",
                                                   mapper=blockops.remap_span_ws)
                moved += acc["moved"]
                dropped += len(acc["dropped"])

            payload = dict(old.payload or {})
            payload["note"] = nb.payload["note"]
            payload["markers"] = nb.payload["markers"]
            if nb.payload.get("rule") and not payload.get("rule"):
                payload["rule"] = nb.payload["rule"]
            if got and old.id not in bad:
                payload.pop("needs_review", None)   # 重译且判定合格 ⇒ 撤掉旧标记
            elif got or not new_zh.strip():
                # 重译了但判定不合格；或**新旧都没有中文**（真·漏译）⇒ 如实挂上。
                payload["needs_review"] = True
            # else：没重译、手里还留着旧译文 ⇒ 标记**保持原样**（不动它）——
            # 旧译文与原文只差空白，为"没按行分"刷一个黄标 = 无意义的黄标
            # （与翻译侧"结构要求不进待校对"同一取向，见 `translator._check` 的
            #  `lines_must_match=False`）。

            zh_src = old.zh_source
            if got:
                zh_src = "mt" if expects_chinese(nb.en, block_type=nb.type) else "none"
            conn.execute(
                "UPDATE blocks SET en=?, zh=?, zh_source=?, payload=?"
                " WHERE paper_id=? AND id=?",
                (nb.en, new_zh, zh_src, dump_json(payload), paper_id, old.id),
            )
            log(f"  · {old.id}（第 {_page(nb)} 页）：{len(old.en)} 字一段 → "
                f"{len(nb.en.split(chr(10)))} 行（字号比 {nb.payload['note']['em']}，"
                f"批注搬迁 {acc['moved']}、作废 {len(acc['dropped'])}）")

    return {"ok": True, "changed": True, "notes": len(pairs), "tokens": tokens,
            "anchors_moved": moved, "anchors_dropped": dropped,
            "translated": len(texts), "needs_review": len(bad)}
