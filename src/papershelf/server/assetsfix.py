"""「修复插图」—— 把库里产物引用的图片，从**记录在案的来源目录**搬回这一篇自己的目录。

## 为什么需要它（而不是「重新提取」）

`#7`（`84e94bc`）修掉了「同一份 PDF 的第二篇命中解析缓存 ⇒ 插图全 404」：命中缓存时把
资产搬进本篇目录。但那段搬运用代码**只在整条转换里跑**（`converter._run`，全仓库唯一
调用点），所以它只救**接下来**要转换的篇目；已经 `done` 的篇目**永远不会再进 `_run`**
⇒ 磁盘上少一张图就是永久 404，且没有任何一条现存出口能把文件放回去：

| 出口 | 实际做什么 | 对已 `done` 的篇目 |
|---|---|---|
| `POST /papers/{id}/convert` | `enqueue(job=None)` + 整条管线 | 整篇重跑/重译（百万级 token），仍不补图 |
| `POST /papers/{id}/reextract` | 清解析缓存 + **`DELETE FROM notes/highlights`** | 批注全没，且 ≈整篇 token |
| `POST /papers/{id}/rebuild-refs` | 只重跑文末文献段 | 不碰插图 |

可发生的面不是"新用户"：`#7` 之前转换的存量篇目（生产上有）、`p<id>/assets/` 被磁盘清理
或恢复丢失、以及**同源那篇被删文献**（`_remove_paper_files` 整目录收走，缓存行却还在）。

## 它做什么（按顺序）

1. 读**库里已落库的产物**（`repo.load_doc`）—— 缺哪几张以**用户实际看到的块**为准，
   不以缓存为准；
2. 一张不缺 ⇒ 直接返回 `changed=False`（幂等：再点一次是 no-op）；
3. 缺的从 `doc.meta["assets_dir"]`（解析时记下的来源）复制回来；
4. 来源目录也没了（同源那篇被删）⇒ **就地重新解析**，但**先证明它解析出的是同一篇**：
   块 id 序列与块原文逐字相同才算数 —— 相同才敢按文件名搬图；不同则**放弃并说明**
   （见下面「为什么必须先证明身份」）。

## 三条不变量

- **只写文件，从不写 `docs` / `blocks` / `notes` / `highlights`**。批注锚在
  `(block_id, lang, start, end)` 上，而本函数**一个块都不改** ⇒「批注一条不动」
  不是靠小心，而是**结构上不可能变**（`tests/test_assets_rebuild.py` 逐字段钉住）。
- **零 LLM 调用**：插图是磁盘文件，不是译文 ⇒ `tokens_used` 一分不涨。
- **不覆盖已有文件**：只补缺的，`p<id>/assets` 里已在的图一律原样不动。

## 为什么必须先证明身份（来源目录没了的这一支）

资产文件名是**按页编号**的（`p1_img1.png` / `p3_deco1.png`），与篇无关 —— 于是
"往别处找一张同名文件搬过来"是**绝对不能做的事**：那不是"修好了"，而是**把别人的图
安到这篇上**（同一个计划里两篇都有第 1 页插图，这个名字必然重名）。`#8` 修的正是
这个失败模式在分享页上的版本：它的表现是"图换了一张"，而不是"图裂了"，肉眼极难发现。

所以来源只能是**记录在案的那一个目录**，或者"同一份 PDF 在这一个 `PARSE_VERSION`
下重新解析出来的产物"。后者要成立就必须先量：块 id 序列与块原文**逐字相同**。
量不过就停手并让用户走「重新提取」（它会让该篇的笔记与划痕作废、代价≈整篇校对+翻译
的 token）—— 那个代价大，但它是用户**自己选的**破坏性出口，而不是我们在背后替他猜。

⚠️ 已知边界：手工修订过块的篇目（㊼ 编辑此块）会因原文不再逐字相同而**走不过**这道
身份校验 ⇒ 这一支对它们放弃。这是有意的（宁可不修，也不猜），此时来源只剩「重新提取」。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ..pipeline import parse_pdf
from .repo import load_doc

log = logging.getLogger("papershelf.assetsfix")

# 「重新提取」的代价说明只写一处：终点（放弃）时把用户能做的下一步点出来，
# 而不是让他对着一张裂图自己想。数字取生产实测的量级（整篇校对 + 公式 + 翻译）。
_REEXTRACT_HINT = ("请用「重新提取」（它会重新解析并从零重跑，该篇的笔记与划痕会作废，"
                   "代价约为整篇原文校对 + 公式 LaTeX 化 + 翻译的 token）")


def _referenced_names(doc) -> list[str]:
    """产物里引用到的资产文件名（`payload["src"] == "assets/<name>"`）。"""
    names: set[str] = set()
    for b in doc.blocks:
        src = str((b.payload or {}).get("src") or "")
        if src.startswith("assets/"):
            names.add(src.split("/", 1)[1])
    return sorted(names)


def _same_layout(fresh, stored) -> bool:
    """重新解析的产物与库里的是不是同一篇：块 id 序列 + 块**原文**逐字相同。

    只比这两样：`zh` 会被翻译/人工修订改写（比它必然不等），`payload` 里带页码等
    产物信息（解析稳定，但没必要一起比）。id 与 `en` 相同 = 这一份 PDF 在这一个
    `PARSE_VERSION` 下产出的块序列完全一致 ⇒ 资产文件名指的必然是同一张图。
    """
    if [b.id for b in fresh.blocks] != [b.id for b in stored.blocks]:
        return False
    return [b.en for b in fresh.blocks] == [b.en for b in stored.blocks]


def rebuild_paper_assets(conn: sqlite3.Connection, paper_id: int, settings,
                         log_fn=None) -> dict[str, Any]:
    """把这一篇缺的插图补回来。返回 `{ok, changed, referenced, repaired, source, reason}`。

    `ok=False` 表示**没法修**（没有产物 / 没有来源也没法证明身份），调用方据此落
    `failed` 并把 `reason` 显示给用户；`ok=True, changed=False` 是幂等的 no-op。
    """
    log_fn = log_fn or log.info
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "该文献不存在", "referenced": 0, "repaired": 0}
    paper = dict(row)

    doc = load_doc(conn, paper_id)
    if doc is None:
        return {"ok": False, "referenced": 0, "repaired": 0,
                "reason": "该文献没有产物（转换尚未完成）"}

    wanted = _referenced_names(doc)
    assets_dir = settings.papers_dir / f"p{paper_id}" / "assets"
    if not wanted:
        return {"ok": True, "changed": False, "referenced": 0, "repaired": 0,
                "reason": "这篇的产物没有引用任何图片"}

    missing = [n for n in wanted if not (assets_dir / n).is_file()]
    if not missing:
        return {"ok": True, "changed": False, "referenced": len(wanted), "repaired": 0,
                "reason": "插图齐全（无需修复）"}

    # ── ① 记录在案的来源目录（解析时写进 `meta["assets_dir"]` 的那一个）──
    src_rel = str((doc.meta or {}).get("assets_dir") or "")
    src_dir = (settings.papers_dir / src_rel) if src_rel else None
    repaired: list[str] = []
    if src_dir is not None and src_dir.is_dir() \
            and src_dir.resolve() != assets_dir.resolve():
        repaired = _copy_into(src_dir, assets_dir, missing)
        if repaired:
            log_fn("来源目录 %s 补回 %d 张：%s", src_rel, len(repaired), ", ".join(repaired))

    still = [n for n in missing if not (assets_dir / n).is_file()]
    if not still:
        return {"ok": True, "changed": True, "referenced": len(wanted),
                "repaired": len(repaired), "source": "recorded"}

    # ── ② 来源目录也没了 ⇒ 重新解析，但**先证明是同一篇** ──
    pdf_path = Path(str(paper.get("pdf_path") or ""))
    if not pdf_path.is_file():
        return {"ok": False, "referenced": len(wanted), "repaired": len(repaired),
                "reason": ("记录在案的图片来源目录已经不在了，这篇也没有 PDF 原件可以重新生成"
                           f"（仍缺 {', '.join(still)}）：{_REEXTRACT_HINT}")}

    # 解析进**这篇自己的目录**下的临时子目录：`parse_pdf` 会把转正副本写到
    # `assets_dir.parent/normalized.pdf`，所以父目录也一并收在 scratch 里，
    # 免得一次失败的尝试顺手动到库里那份 `p<id>/normalized.pdf`。
    # 放在 `p<id>/` 内（而不是全局临时目录）：删文献（㉙）整目录 rmtree 时一起收走。
    scratch = settings.papers_dir / f"p{paper_id}" / "reparse"
    try:
        log_fn("来源目录不在了 → 为 paper=%s 就地重新解析并校验身份（零 token）", paper_id)
        fresh = parse_pdf(pdf_path, assets_dir=scratch / "assets")
        if not _same_layout(fresh, doc):
            return {"ok": False, "referenced": len(wanted), "repaired": len(repaired),
                    "reason": ("重新解析出的块与库里这份产物对不上（块 id 或原文不同），"
                               f"不敢按文件名把图安上去（仍缺 {', '.join(still)}）："
                               f"{_REEXTRACT_HINT}")}
        extra = _copy_into(scratch / "assets", assets_dir, still)
        repaired += extra
        still = [n for n in still if not (assets_dir / n).is_file()]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if still:
        return {"ok": False, "referenced": len(wanted), "repaired": len(repaired),
                "reason": (f"重新解析出的产物里也没有这些图（{', '.join(still)}），"
                           f"无法修复：{_REEXTRACT_HINT}")}
    return {"ok": True, "changed": True, "referenced": len(wanted),
            "repaired": len(repaired), "source": "reparse"}


def _copy_into(src_dir: Path, assets_dir: Path, names: list[str]) -> list[str]:
    """把 `names` 里能在 `src_dir` 找到的文件复制进 `assets_dir`；返回真的复制了的。

    - **不覆盖**目标已有的文件（幂等：第二次调用什么都不做）；
    - 源缺哪个就跳过哪个（由调用方决定"缺了算什么"）。
    """
    done: list[str] = []
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        src = src_dir / name
        dst = assets_dir / name
        if not src.is_file() or dst.is_file():
            continue
        shutil.copy2(src, dst)
        done.append(name)
    return done
