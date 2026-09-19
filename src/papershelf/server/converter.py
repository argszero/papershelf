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
import time
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

    ## 认领即计数（`conv_attempts + 1`）—— 2026-09-15 修

    原先计数写在 `_set_state(..., "doing")` 里，而 `doing` 的转换**是由本函数做的**
    （`_set_state` 只会被 `failed` 调用）→ 那条 `1 if state == "doing" else 0` 的分支
    **从来没被走到**。实测后果（宿主机上跑一条转换即可复现）：

    - `papers.conv_attempts` **恒为 0**，`max_conv_attempts`（默认 3）这道成本护栏
      **从未生效** —— 一篇必然失败（例如 LLM 欠费 402）的文献可以被人无限次重跑；
    - 每篇日志都写「第 1 次尝试」，**在撒谎** —— 排障时会得出"它只试过一次"的错误结论。

    计数语义 = **"这篇开始跑第几次了"**，所以它应该落在"开始"那一步，也就是认领。
    检查仍在 `convert_paper` 开头（先看 `>= max` 再认领），因此第 `max+1` 次会被挡下。
    人工主动入口（`/convert` 重试、`/reextract`）会把计数**归零** —— 护栏防的是
    "自动重试烧钱"，不是防用户（`docs/design.md` 的原文即「超限置 failed 待人工重试」，
    人工重试若不能重置计数，那句话就是空话）。
    """
    with tx(conn):
        cur = conn.execute(
            """UPDATE papers SET conv_state='doing', conv_attempts=conv_attempts+1,
                      updated_at=? WHERE id=? AND conv_state='queued'""",
            (utcnow(), paper_id),
        )
    return cur.rowcount == 1


def _set_state(conn: sqlite3.Connection, paper_id: int, state: str,
               error: str | None = None, tokens: int = 0) -> None:
    """只改状态与错误（**不再碰 `conv_attempts`** —— 它由 `claim_paper` 记，见其 docstring）。

    终态（`done`/`failed`）**一并清掉 `pending_job`**：那一列回答的是"轮到它时做什么"，
    事情已经做完了（或已知做不成），留着只会让**下一次**排队被误当成上一次的活
    （「修文献 → 用户又点重新转换 → 实际又跑了一遍修文献」是这条列最可能的坏结局）。
    """
    with tx(conn):
        conn.execute(
            """UPDATE papers SET conv_state=?, conv_error=?, updated_at=?,
                      tokens_used = tokens_used + ?, pending_job=NULL
               WHERE id=?""",
            (state, error, utcnow(), tokens, paper_id),
        )


def enqueue(conn: sqlite3.Connection, paper_id: int, job: str | None = None,
            *, reset_attempts: bool = False) -> None:
    """把一篇文献**排进队列**（`conv_state='queued'`），并声明这次要跑什么。

    **所有**排队入口都走这里（导入/arXiv 的 INSERT 除外 —— 它们新插的行本来就是
    `queued` + `pending_job=NULL`）。为什么要一个函数：`pending_job` 是**跨请求存活**的，
    而"重新提取"与"重建参考文献"都排同一列车 —— 若某个入口只改 `conv_state` 而不声明
    `job`，上一轮残留的 `pending_job` 就会把这次转换劫持成另一次操作。
    一处声明，别处不可能漏（`tests/test_refs_rebuild.py` 钉住"重试入口必须清掉旧 job"）。

    `job=None` = 完整转换。`job="refs"` = 只重建文末参考文献（`server/refsfix.py`）。
    `reset_attempts=True` = 人工入口（重试/重新提取/重建参考文献）把 `conv_attempts` 归零，
    理由见 `retrigger`：护栏防的是自动重试烧钱，不是防用户。
    """
    cols = "conv_state='queued', conv_error=NULL, pending_job=?"
    args: list[Any] = [job]
    if reset_attempts:
        cols += ", conv_attempts=0"
    args.append(paper_id)
    with tx(conn):
        conn.execute(f"UPDATE papers SET {cols} WHERE id=?", args)


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
            # 从「认领后」到「落库」的全过程，额外抄一份到 `<data>/logs/p<id>.log`
            # （运维按篇排障：容器日志是混流的，事后问"第 12 篇经历了什么"要翻半天）。
            from .logging_setup import paper_log

            t0 = time.monotonic()
            with paper_log(paper_id, settings.logs_dir, enabled=settings.log_per_paper):
                if (fresh["pending_job"] or "") == "refs":
                    _run_refs_fix(conn, paper_id, t0)
                    return
                log.info("paper=%s 开始转换（第 %s 次尝试，来源=%s，PDF=%s）", paper_id,
                         int(fresh["conv_attempts"]) + 1, fresh["source"],
                         Path(str(fresh["pdf_path"] or "")).name or "—")
                try:
                    doc, tokens = _run(conn, dict(fresh), settings, fingerprint)
                except Exception as exc:                   # noqa: BLE001
                    log.exception("paper=%s 转换失败（%.1fs）", paper_id, time.monotonic() - t0)
                    _set_state(conn, paper_id, "failed", f"{type(exc).__name__}: {exc}")
                    return

                save_doc(conn, paper_id, doc)
                _writeback_meta(conn, paper_id, dict(fresh), doc.meta, tokens)
                log.info("paper=%s 转换完成：%d 块 / %d tokens / 用时 %.1fs（状态已置 done）",
                         paper_id, len(doc.blocks), tokens, time.monotonic() - t0)
    finally:
        conn.close()


def _run_refs_fix(conn: sqlite3.Connection, paper_id: int, t0: float) -> None:
    """`pending_job='refs'` 的那条支路：**只重建文末参考文献**，不重跑管线。

    ## 为什么它必须挂在同一个队列/状态机上（而不是自己起一个线程）

    这条活是**长任务**（生产那篇 288 条、≈30 万 tokens、好几分钟），所以：
    - 不能塞在请求线程里（㉗ 的教训：任务活在 HTTP 请求里 = 一重启就人间蒸发）；
    - 必须和转换**共用并发闸门**（`concurrency_semaphore`），否则一次点击能让
      LLM 侧并发翻倍；
    - 必须能被启动恢复捞回来（`doing → queued` 只看 `conv_state`，`pending_job` 原样留着，
      重启后它仍然知道自己该干"修文献"而不是"重跑整篇"）。

    复用 `convert_paper` 的外壳（等槽位 → 认领 → 记日志 → 落终态）是唯一不重复实现
    这些不变量、也不把转换路径搅乱的做法。
    """
    from .refsfix import rebuild_paper_refs

    log.info("paper=%s 开始重建参考文献（只动文末文献段；正文、笔记与划痕都不动）", paper_id)
    try:
        result = rebuild_paper_refs(conn, paper_id, log=log.info)
    except Exception as exc:                           # noqa: BLE001
        log.exception("paper=%s 重建参考文献失败（%.1fs）", paper_id, time.monotonic() - t0)
        _set_state(conn, paper_id, "failed", f"{type(exc).__name__}: {exc}")
        return
    tokens = int(result.get("tokens", 0) or 0)
    if not result.get("ok"):
        # `ok=False` = 这篇根本没有产物（转换还没完成 / 已被删）。不是失败，
        # 但也**不能**写 `done`：那会让界面显示"转换完成"而实际一片空白。
        _set_state(conn, paper_id, "failed", str(result.get("reason") or "没有可修复的产物"))
        return
    if not result.get("changed"):
        log.info("paper=%s 重建参考文献：无事可做（%s），未改动库也未调模型",
                 paper_id, result.get("reason"))
    else:
        log.info("paper=%s 重建参考文献完成：%s 条条目（余下未切出的碎片 %s 个）/ %d tokens / "
                 "批注搬家 %s 条（落空 %s 条）/ 用时 %.1fs",
                 paper_id, result.get("entries"), result.get("refs"), tokens,
                 result.get("anchors_moved"), result.get("anchors_dropped"),
                 time.monotonic() - t0)
    _set_state(conn, paper_id, "done", None, tokens=tokens)


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


def _pending_proofread_pages(doc: Doc, ok_pages) -> list[int]:
    """①c 还需要校对的页 = **有抽取结果的页** − 已校对过（`ok_pages`）的页。

    ⚠️ 判据是"**还剩哪些页**"，不是"有没有页校过"：预算用尽 / 上游 504 都会留下
    "校了一半"的 `ok_pages`；按"有页校过就整篇跳过"处理的话，剩下的页**永远不会**被校对，
    而界面上却显示"已完成"（比报错更坏 —— 静默的半成品）。
    空白页（没有块的页）不算 —— agent 没有可核对的东西，永远标记不了它们。
    """
    have = {int(b.payload.get("page") or 0) for b in doc.blocks} - {0}
    return sorted(have - {int(p) for p in (ok_pages or ())})


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
            log.info("① 解析：命中解析缓存（fingerprint=%s…），跳过 PDF 解析", fingerprint[:12])
        else:
            t_parse = time.monotonic()
            doc = parse_pdf(pdf_path, assets_dir=settings.papers_dir / f"p{paper['id']}/assets")
            # arXiv/上传的元数据提示：文件名常含标题，交给后续元数据抽取
            if paper.get("source_ref"):
                doc.meta.setdefault("source_ref", paper["source_ref"])
            log.info("① 解析完成：%s 页 / %d 块 / %d 图 / 用时 %.1fs",
                     doc.meta.get("pages"), len(doc.blocks), len(doc.assets),
                     time.monotonic() - t_parse)

    tokens = 0

    # ── ①c 原文抽取校对（VLM agent，宿主 2026-09-14）—— 必须在公式 LaTeX 化与翻译之前 ──
    # 只有 PDF 路线有「渲染页图」可比对；arXiv 路线走官方 HTML（原生结构化，没有抽取误差）。
    # 命中解析缓存时**只补做没校过的页**：缓存里存的可能是校对前的旧产物（存量文献），
    # 但校对很贵（整页图 + 推理模型，一篇 37 页 ≈ 与整篇翻译同级），不能每次重跑都重买一遍 ——
    # 上次哪些页已校对成功记在 `doc.meta["proofread"]["ok_pages"]`（也随缓存落库）。
    proofread_pdf = None if paper["source"] == "arxiv" else Path(paper["pdf_path"] or "")
    # ⚠️ 整页旋转的页面（v12）：解析阶段已把 PDF **转正**并落成副本（`doc.meta["normalized_pdf"]`），
    # ①c 必须读**同一份** —— 它渲染页图、量疑似表区，坐标都得与解析产物对齐，否则
    # `read_page(region=…)` 会放大到错误的位置（比"看反方向"更糟：看反方向它自己转得过来，
    # 坐标错位会让它以为提示是假的）。副本不在时退回原件（不崩，只是校对质量下降）。
    normalized = doc.meta.get("normalized_pdf")
    if proofread_pdf is not None and normalized:
        norm = Path(normalized)
        if norm.exists():
            proofread_pdf = norm
        else:
            log.warning("转正副本不在（%s）→ ①c 将看原方向页图", norm)
    prev_pf = doc.meta.get("proofread") or {}
    done_pages = set(prev_pf.get("ok_pages") or ())
    want_pf = settings.proofread and proofread_pdf is not None and proofread_pdf.exists()
    pending = _pending_proofread_pages(doc, done_pages)
    # ⚠️ 判据必须是"**是不是所有页都校过**"，不能是"有没有页校过"：预算用尽 / 上游中断都会
    # 留下"校了一半"的 `ok_pages`，那时该做的是**接着校剩下的页**，而不是整篇跳过
    # （跳过 = 剩下的页永远没人校对，而且 UI 上看起来是"已完成"）。
    if want_pf and not pending:
        log.info("①c 原文校对：%d 页全部已校对过，跳过（不重复计费）", len(done_pages))
    elif want_pf:
        from ..pipeline.proofread import Proofreader

        t_pf = time.monotonic()
        pf_cfg = LLMConfig(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                           model=settings.proofread_model or settings.llm_model)
        pf = Proofreader(pf_cfg, dpi=settings.proofread_dpi,
                         max_tokens=settings.proofread_max_tokens,
                         max_rounds=settings.proofread_max_rounds,
                         token_budget=settings.proofread_token_budget,
                         retries=settings.proofread_retries,
                         thinking=settings.proofread_thinking)
        try:
            stats = pf.proofread_doc(doc, proofread_pdf, skip_pages=done_pages)
            tokens += stats.tokens
            # 续跑时把上一次的计数**累加**进来（否则界面上"文字修订 3 块"会突然变小，
            # 看起来像校对白做了）；`failed` 只记本次 —— 它决定下次是否还重试。
            def _cum(key: str, now: int) -> int:
                return (prev_pf.get(key) or 0) + now if done_pages else now

            ok_pages = sorted(done_pages | set(stats.ok_pages))
            doc.meta["proofread"] = {
                "pages": len(ok_pages),
                "ok_pages": ok_pages,
                "reordered": _cum("reordered", stats.reordered),
                "text_fixed": _cum("text_fixed", stats.text_fixed),
                "retyped": _cum("retyped", stats.retyped),
                "merged": _cum("merged", stats.merged),
                "split": _cum("split", stats.split),
                "deduped": _cum("deduped", stats.dropped),
                "rejected": _cum("rejected", stats.rejected),
                "rounds": _cum("rounds", stats.rounds),
                "tool_calls": _cum("tool_calls", stats.tool_calls),
                "failed": stats.failed,
                "tokens": _cum("tokens", stats.tokens),
                "unreviewed": stats.unreviewed,
                "notes": stats.notes,
                "stopped": stats.stopped,
            }
            log.info("①c 原文校对（agent）完成：%s / 本次 %d 页 / 用时 %.1fs",
                     stats.summary(), stats.pages, time.monotonic() - t_pf)
            for note in stats.notes:
                log.info("①c 校对记录：%s", note)
        except Exception as exc:                         # noqa: BLE001 — 校对失败不该毁掉整篇转换
            log.warning("①c 原文校对整体失败（已跳过，不影响其余步骤）：%s", exc)
    else:
        log.info("①c 原文校对：未启用或无 PDF 原件，跳过")

    # ── ② 公式 LaTeX 化（决策㉓）—— 必须在翻译之前 ──
    cfg = LLMConfig(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                    model=settings.llm_model)
    # 只把**看起来含数学**的块送 LaTeX 化（宽进判据，见 `looks_math`）。
    # 全文无公式的论文在这里完全不会碰 LLM —— 转换仍然应当成功（只是没译文）。
    if any(looks_math(b.en, block_type=b.type)
           for b in doc.blocks if not b.payload.get("latexized")):
        t_lz = time.monotonic()
        lz = Latexizer(cfg)
        stats = lz.latexize(doc.blocks, log=lambda *a: log.info(*a))
        tokens += lz.tokens_used
        log.info("② 公式 LaTeX 化完成：%d 块 / %d 切片 / 正式改写 %d / 重试 %d / 失败回落 %d / 用时 %.1fs",
                 stats.get("blocks", 0), stats.get("chunks", 0), stats.get("done", 0),
                 stats.get("retried", 0), stats.get("failed", 0), time.monotonic() - t_lz)
    else:
        log.info("② 公式 LaTeX 化：无含数学的块，跳过（未调用 LLM）")

    # 解析 + 原文校对 + 公式的成果先缓存（下次同 PDF 可直接复用）
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
    t_mx = time.monotonic()
    mx = MetadataExtractor(cfg)
    doc.meta.update(mx.extract(doc.blocks, candidates=doc.meta.get("title_candidates"),
                               log=lambda *a: log.info(*a)))
    tokens += mx.tokens_used
    log.info("②b 元数据抽取完成：title=%r / 用时 %.1fs",
             doc.meta.get("title_en") or doc.meta.get("title_zh"), time.monotonic() - t_mx)

    # ── ③ 翻译（术语表来自计划设置，决策⑫）──
    row = conn.execute("SELECT glossary FROM plan_settings WHERE plan_id=?",
                       (paper["plan_id"],)).fetchone()
    glossary = json.loads(row["glossary"]) if row and row["glossary"] else []
    t_tr = time.monotonic()
    tr = Translator(cfg, glossary)
    texts = tr.translate_blocks(doc.blocks, log=lambda *a: log.info(*a))
    tokens += tr.tokens_used
    log.info("③ 翻译完成：%d 块需译 / 待校对 %d 块 / 用时 %.1fs",
             len(tr.ordered(doc.blocks)), len(getattr(tr, "needs_review", []) or []),
             time.monotonic() - t_tr)
    for b, text in zip(Translator.ordered(doc.blocks), texts):
        if b.zh_source == "human":
            continue                      # ⑯：人工修订不被覆盖
        if text and (expects_chinese(b.en, block_type=b.type) or text != b.en):
            b.zh, b.zh_source = text, ("mt" if expects_chinese(b.en, block_type=b.type) else "none")

    # ── ④ 标记保真校验（决策④）──
    # ⚠️ 必须 typeset=False：校验器比对的是带标记的 LaTeX 源码（\tag{12} 写在里面），
    #    渲染成 MathML 后编号会变，校验就变成苹果对橘子。
    t_v = time.monotonic()
    report = validate(en_html(doc, typeset=False), _zh_html(doc, typeset=False))
    enforce_report(report, doc)
    log.info("④ 标记保真校验：通过（漏译 %d 块已标待校对）/ 用时 %.1fs",
             len(report.untranslated), time.monotonic() - t_v)
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
