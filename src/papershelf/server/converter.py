"""转换 worker —— 把管线接到服务端（决策② 全自动无人值守）。

职责边界（刻意收窄）：
- 本模块**只负责调度与状态**（`conv_state` 状态机、错误记录、token 记账）；
- 解析/公式/翻译/校验的实际逻辑全在 `pipeline/`，一行都不重复实现。

后台线程执行（§5.6 的 DB 轮询提案的等价物，单体里更简单）：
FastAPI 的 `BackgroundTasks` 在**响应后**跑，不阻塞请求。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from ..pipeline import Doc, LLMConfig, Latexizer, Translator, en_html, parse_pdf
from ..pipeline.equations import looks_math
from ..pipeline.metadata import MetadataExtractor
from ..pipeline.validate import Report, expects_chinese, validate
from .config import get_settings
from .db import dump_json, load_json, tx, utcnow
from .repo import save_doc
from .titlemeta import is_placeholder_title

log = logging.getLogger("papershelf.converter")

# 全局并发上限（待定项 5 的初值）：自托管机器不大，默认 2。
# ⚠️ **惰性构造**：模块导入时求值的话，`Settings` 读的是导入那一刻的环境变量 ——
#    测试里按用例改 `PAPERSHELF_MAX_CONCURRENCY` 会毫无效果（两条不同的路径）。
_sem: threading.Semaphore | None = None
_sem_lock = threading.Lock()


def concurrency_semaphore() -> threading.Semaphore:
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.Semaphore(max(1, int(get_settings().max_concurrency)))
        return _sem


def claim_paper(conn: sqlite3.Connection, paper_id: int) -> bool:
    """**认领**：`queued → doing`（原子）。False = 这篇不归我跑（别人先抢到/已跑完）。

    `server/queue.py` 直接复用它 —— **互斥点只能有一份实现**，两处各写一遍必然漂移。
    没有它，同一篇会被跑两遍：双倍 token，且两份产物互相覆盖。
    """
    with tx(conn):
        cur = conn.execute(
            "UPDATE papers SET conv_state='doing', updated_at=? WHERE id=? AND conv_state='queued'",
            (utcnow(), paper_id),
        )
    return cur.rowcount == 1


def _set_state(conn: sqlite3.Connection, paper_id: int, state: str,
               error: str | None = None, tokens: int = 0) -> None:
    with tx(conn):
        conn.execute(
            """UPDATE papers SET conv_state=?, conv_error=?, updated_at=?,
                      tokens_used = tokens_used + ?,
                      conv_attempts = conv_attempts + ?
               WHERE id=?""",
            (state, error, utcnow(), tokens, 1 if state == "doing" else 0, paper_id),
        )


def convert_paper(paper_id: int, fingerprint: str | None = None) -> None:
    """完整流程：解析 → 公式 LaTeX 化 → 翻译 → 校验 → 落库。

    任何异常都必须落到 `conv_state=failed`（绝不抛出到请求线程）；
    失败**不阻塞**其它文献（§5.7）。

    ## ⚠️ 顺序铁律：**先拿到并发槽位，再认领状态**

    最初写成「先 `claim_paper()` 再 `with concurrency_semaphore()`」，本地一跑就现形：
    `PAPERSHELF_MAX_CONCURRENCY=2` 而队列有 8 篇时，**8 篇全部立刻被写成 `doing`**，
    然后 6 篇在信号量上排队 —— 界面上 8 篇"转换中"，实际只有 2 篇在跑；此刻重启，
    6 篇里还没轮到的那几篇**又变成僵尸 `doing`**。这与事故本身是同一个失败模式
    （"谎报进度 + 重启即丢"），只是快了一拍。

    所以：阻塞等槽位期间，`conv_state` 必须**原样停在 `queued`**（诚实地说"还没轮到我"）。
    认领放在槽位之内 —— 同一时刻最多 `max_concurrency` 篇处于"已认领"中途，
    窗口极小且语义诚实。
    """
    settings = get_settings()
    from .db import connect

    conn = connect(settings)
    try:
        paper = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        if paper is None:
            return
        if paper["conv_attempts"] >= settings.max_conv_attempts:
            _set_state(conn, paper_id, "failed", "超过最大重试次数")
            return
        if paper["conv_state"] not in ("queued", "doing"):
            return                      # 已 done/failed/none 的行不该被任何路径重跑

        with concurrency_semaphore():
            # 再读一次：等槽位期间这篇可能已被别的路径跑完（去重的关键窗口）
            fresh = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            if fresh is None or fresh["conv_state"] not in ("queued", "doing"):
                return
            if not claim_paper(conn, paper_id):
                log.info("跳过 paper=%s（已被其它路径认领）", paper_id)
                return
            try:
                doc, tokens = _run(conn, dict(fresh), settings, fingerprint)
            except Exception as exc:                       # noqa: BLE001
                log.exception("转化失败 paper=%s", paper_id)
                _set_state(conn, paper_id, "failed", f"{type(exc).__name__}: {exc}")
                return

            save_doc(conn, paper_id, doc)
            _writeback_meta(conn, paper_id, dict(fresh), doc.meta, tokens)
    finally:
        conn.close()


def _writeback_meta(conn: sqlite3.Connection, paper_id: int, paper: dict[str, Any],
                    meta: dict[str, Any], tokens: int) -> None:
    """收尾：`conv_state=done` + token 记账 + ⑲ 元数据写回（决策⑲）。

    ## ⚠️ 这里踩过一个把占位值锁死的坑（2026-09-12）

    原先是 `title = COALESCE(NULLIF(title,''), ?)`，意图「别覆盖用户手填的标题」。
    但上传时 `papers.title` 已被写成**文件名**（`Path(name).stem`）——
    `NULLIF('original','')` 仍是 `'original'`（非空），`COALESCE` 于是取旧值，
    **LLM 抽到的真标题永远写不进去**。生产 12 篇全叫 `original` 就是这么来的。

    `COALESCE` 分不清「用户填的」与「上传时的占位符」。改用显式标记列
    `papers.title_is_placeholder`（`_is_placeholder_title`）：导入时置 1、
    用户在界面上改过标题就置 0。**不能靠 `pdf_path` 推导** —— 落盘文件名带时间戳前缀
    （`2026-09-11T143352+0000_original.pdf`），`stem` 与 `title` 永不相等，
    生产 12 篇会全部被判成"用户填的"而永远拒绝覆盖（这是同日踩的第二个坑，详见
    `_is_placeholder_title` 的 docstring）。
    """
    fields: dict[str, Any] = {
        "conv_state": "done",
        "conv_error": None,
        "updated_at": utcnow(),
    }
    # 标题：占位值可被覆盖，用户手填值绝不覆盖（⑲「可手动改」）
    #
    # ⚠️ 写进真标题的同时**必须把占位标记撤掉**（`title_is_placeholder=0`，宿主 2026-09-13
    # 定的口径：「回填成功 = 已确认」）。这一列的名字就是它的语义 ——
    # 「标题还是导入时那个文件名」，既然已经不是了，就该是 0。原先只改标题不动标记，
    # 结果**列在说谎**，有两个真实代价（都在生产上现形）：
    #   ① `backfill-meta --only-missing` 的判据是「标记为 1 且没作者」→ 已回填的 12 篇
    #      每跑一次都会被当成"缺元数据"重抽一遍（每篇 ≈1.6k tokens，白烧）；
    #   ② 用户看到的"已回填"与系统认为的"仍待回填"是两回事，没人能一眼看出差别。
    # 代价是：**重跑转换不再覆盖已抽出的标题**（保护范围从"用户手填"扩到"已确认"）。
    # 这是刻意的 —— 抽到的标题就是这篇的定稿；要改就手工改，与用户手填同一条路。
    new_title = meta.get("title_zh") or meta.get("title_en") or ""
    if new_title and _is_placeholder_title(paper):
        fields["title"] = new_title
        fields["title_is_placeholder"] = 0
    # 作者/期刊/年份：只在"当前为空"且"抽到了值"时写入
    for col, keys in (("authors", ("authors",)), ("venue", ("journal", "venue"))):
        if (paper.get(col) or "").strip():
            continue
        for key in keys:
            val = meta.get(key)
            if isinstance(val, str) and val.strip():
                fields[col] = val.strip()
                break
    if paper.get("year") is None and meta.get("year") is not None:
        fields["year"] = meta["year"]
    # 标签：同规矩 —— 用户已填的优先（⑲ 自由标签）
    tags = meta.get("tags")
    if isinstance(tags, list) and tags and not load_json(paper.get("tags"), []):
        fields["tags"] = dump_json([str(t) for t in tags])

    sets = ", ".join(f"{k}=?" for k in fields)
    with tx(conn):
        conn.execute(
            f"UPDATE papers SET {sets}, tokens_used = tokens_used + ? WHERE id=?",
            (*fields.values(), tokens, paper_id),
        )


def _is_placeholder_title(paper: dict[str, Any]) -> bool:
    """`title` 是不是「导入时随手写下、用户从未改过」的占位值。

    判据本体住在 `titlemeta.py`（`db._migrate` 也要用，放这里会成环）。
    本函数只是把 `papers` 行映射过去，方便调用点少写两个字段名。
    """
    return is_placeholder_title(paper.get("title"),
                                flag=paper.get("title_is_placeholder", ...),
                                source_ref=paper.get("source_ref"),
                                pdf_path=paper.get("pdf_path"))


def _run(conn: sqlite3.Connection, paper: dict, settings, fingerprint: str | None) -> tuple[Doc, int]:
    """真正跑管线，返回 (Doc, tokens)。"""
    # ── ⓪ 未配 LLM 就别开始 ──────────────────────────────────────────────
    # 踩过：不前置检查时，整篇会一路跑到最后的校验，因为"一个块都没译"而报
    # 「标记保真校验未通过 · 疑似漏译」→ 用户去查翻译质量问题，而真正的原因是
    # 环境变量没配。这里的错误会原样进 `conv_error` 显示在界面上（⑪），
    # 所以必须点名"该改哪个变量"。
    if not (settings.llm_base_url and settings.llm_api_key):
        raise RuntimeError("服务端未配置 LLM（PAPERSHELF_LLM_BASE_URL / PAPERSHELF_LLM_API_KEY），"
                           "无法翻译与公式 LaTeX 化；配置后点「重试」即可")

    # ── ① 解析 ──
    if paper["source"] == "arxiv":
        doc = _from_arxiv(paper, settings)
    else:
        pdf_path = Path(paper["pdf_path"] or "")
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF 不存在：{pdf_path}")
        # 缓存去重（待定项 7）：同一份 PDF 的解析结果复用，省下解析与公式 LaTeX 化的开销
        cached = None
        if fingerprint:
            row = conn.execute("SELECT doc_json FROM doc_cache WHERE fingerprint=?",
                               (fingerprint,)).fetchone()
            cached = row["doc_json"] if row else None
        if cached:
            doc = Doc.from_json(cached)
        else:
            doc = parse_pdf(pdf_path, assets_dir=settings.papers_dir / f"p{paper['id']}/assets")
            # arXiv/上传的元数据提示：文件名常含标题，交给后续元数据抽取
            if paper.get("source_ref"):
                doc.meta.setdefault("source_ref", paper["source_ref"])

    tokens = 0

    # ── ② 公式 LaTeX 化（决策㉓）—— 必须在翻译之前 ──
    cfg = LLMConfig(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                    model=settings.llm_model)
    # 只把**看起来含数学**的块送 LaTeX 化（宽进判据，见 `looks_math`）。
    # 全文无公式的论文在这里完全不会碰 LLM —— 转换仍然应当成功（只是没译文）。
    if any(looks_math(b.en, block_type=b.type)
           for b in doc.blocks if not b.payload.get("latexized")):
        lz = Latexizer(cfg)
        lz.latexize(doc.blocks, log=lambda *a: log.info(*a))
        tokens += lz.tokens_used

    # 解析+公式的成果先缓存（下次同 PDF 可直接复用）
    if fingerprint:
        with tx(conn):
            conn.execute(
                """INSERT INTO doc_cache (fingerprint, doc_json, created_at) VALUES (?,?,?)
                   ON CONFLICT(fingerprint) DO UPDATE SET doc_json=excluded.doc_json""",
                (fingerprint, doc.to_json(), utcnow()),
            )

    # ── ②b 元数据与标签自动抽取（决策⑲）──
    # 放在翻译**之前**：这些是首屏信息，用户打开文献库时就要看到；
    # 万一后面翻译抛异常，元数据也已经写进 doc.meta 了。
    # ⚠️ 必须早于下面的 doc_cache 失效判断之外 —— 它是**与解析无关**的一步。
    mx = MetadataExtractor(cfg)
    doc.meta.update(mx.extract(doc.blocks, candidates=doc.meta.get("title_candidates"),
                               log=lambda *a: log.info(*a)))
    tokens += mx.tokens_used

    # ── ③ 翻译（术语表来自计划设置，决策⑫）──
    row = conn.execute("SELECT glossary FROM plan_settings WHERE plan_id=?",
                       (paper["plan_id"],)).fetchone()
    glossary = json.loads(row["glossary"]) if row and row["glossary"] else []
    tr = Translator(cfg, glossary)
    texts = tr.translate_blocks(doc.blocks, log=lambda *a: log.info(*a))
    tokens += tr.tokens_used
    for b, text in zip(Translator.ordered(doc.blocks), texts):
        if b.zh_source == "human":
            continue                      # ⑯：人工修订不被覆盖
        if text and (expects_chinese(b.en, block_type=b.type) or text != b.en):
            b.zh, b.zh_source = text, ("mt" if expects_chinese(b.en, block_type=b.type) else "none")

    # ── ④ 标记保真校验（决策④）──
    # ⚠️ 必须 typeset=False：校验器比对的是带标记的 LaTeX 源码（\tag{12} 写在里面），
    #    渲染成 MathML 后编号会变，校验就变成苹果对橘子。
    report = validate(en_html(doc, typeset=False), _zh_html(doc, typeset=False))
    enforce_report(report, doc)
    return doc, tokens


def enforce_report(report: Report, doc: Doc) -> None:
    """校验报告的出口裁决：**只有结构性问题才阻塞**，漏译降级为「待校对」。

    结构性破损（丢块/多块/重复/乱序/标签不配对）→ 抛异常，交给用户重试入口；
    「疑似漏译」**不能**在这里判死：翻译器已经为它跑满 2 轮定点重译，并把仍不合格
    的块标成了 `needs_review`（决策⑯ 的收敛保证）—— 走到这里说明**已经收敛**，
    再抛异常等于把「已完成的几百块译文」连同那几块一起报废。

    ⚠️ 生产实测（2026-09-11）：一篇 400 块论文只有 2 块重试后仍无中文，
    整篇却 `conv_state=failed`、`tokens_used` 还是 0，用户看到的是「转换失败」
    而不是「2 块待校对」。这与 `docs/design.md` §5.5「降级 → 回落原文本并标
    needs_review，**绝不阻塞管线**」直接矛盾。
    """
    structural = (report.missing or report.extra or report.duplicated
                  or report.out_of_order or report.tag_imbalance)
    if structural:
        raise RuntimeError("标记保真校验未通过：" + report.summary().replace("\n", " ")[:400])
    if report.untranslated:
        log.warning("标记保真校验：%d 块重试后仍无中文 → 标「待校对」，不阻塞管线（%s）",
                    len(report.untranslated), ", ".join(report.untranslated[:8]))
        for bid in report.untranslated:
            b = next((x for x in doc.blocks if x.id == bid), None)
            if b is not None:
                b.payload["needs_review"] = True


def _zh_html(doc: Doc, *, typeset: bool = False) -> str:
    """中文视图的 HTML —— **仅供校验使用**，所以默认 `typeset=False`。

    ⚠️ 踩过：`typeset` 参数是在校验要求下加进来的（校验器比的是 LaTeX 源码），
    但调用点先加、函数签名后忘，于是**任何零 LLM 的论文**（无公式、无待译块）
    都会在最后一步 `TypeError` 落成「转换失败」—— 而带公式的论文因为更早的
    路径不会走到这里，问题就藏住了。改这里时务必连带跑一次无公式的小 PDF。
    """
    from ..pipeline import document_html

    return document_html(doc.blocks, lang="zh", title=doc.meta.get("title_zh") or "",
                         lang_attr="zh-CN", typeset=typeset)


def _from_arxiv(paper: dict, settings) -> Doc:
    """⑱：arXiv 路线。**优先官方 HTML**（原生 LaTeX，零 LLM 成本且保真）。"""
    from .arxiv import import_arxiv_doc

    return import_arxiv_doc(paper, settings)
