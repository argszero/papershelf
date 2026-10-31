"""文献：导入（PDF 上传 / arXiv）、列表、状态与进度、转换触发。

管线只在本模块被调度：转换是**后台任务**（决策② 全自动无人值守），
状态机落在 `papers.conv_state`（`none → queued → doing → done|failed`，§5.6）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request,
                     UploadFile, status)
from pydantic import BaseModel

from ...pipeline import Doc, en_html, looks_math, parse_pdf
from ...pipeline.validate import expects_chinese
from ..config import Settings, get_settings
from ..converter import convert_paper, enqueue
from ..db import dump_json, rows_to_list, tx, utcnow
from ..repo import doc_public, paper_public
from ..security import current_user, get_conn, require_paper, require_plan

log = logging.getLogger("papershelf.papers")

router = APIRouter(prefix="/api", tags=["papers"])


class ArxivIn(BaseModel):
    ref: str                      # arXiv ID 或 URL（⑱）


class PaperPatch(BaseModel):
    title: str | None = None
    authors: str | None = None
    venue: str | None = None
    year: int | None = None
    tags: list[str] | None = None
    status_: str | None = None       # unread|reading|read|reviewed（⑰）
    progress: int | None = None      # 0-100（⑰）
    # **谁在报这个数字**（2026-09-19，宿主：「还需要支持手动修改进度」）：
    #   · 缺省 `"scroll"` = **阅读器滚动上报**（唯一的自动来源）—— 走 ⑰ 的"只增不减"兜底；
    #   · `"user"` = **用户手改**（阅读器那行「进度 N%」点开就地改）—— **无条件生效，含改小**。
    # 为什么必须由调用方显式声明，而不是猜：两者是**同一个字段的两种含义**，靠值本身分不清
    #   （手改 80→30 与"用户从底部滚回顶部"发的是同一个 `{"progress": 30}`）。猜错的后果是
    #   把 ⑰ 那条护栏拆掉 —— 而它挡的是"进度条自己往回退"这个看起来像坏了的现象。
    progress_by: str | None = None   # scroll | user


def _pdf_fingerprint(path: Path) -> str:
    """`doc_cache` 的键 = PDF 内容 hash **+ 解析版本号**。

    ⚠️ 为什么不能只用 PDF hash（踩过类）：解析器变了（例如本次给每个块加 `payload.page`）
    而 PDF 一字未改时，缓存会永远命中旧的解析产物 —— 新代码看着没问题，效果却不出现，
    排查会一路查到"代码明明写了"。混入 `PARSE_VERSION` 后，解析语义一变即自动重建。
    """
    from ...pipeline import PARSE_VERSION

    h = hashlib.sha256()
    h.update(f"parse-v{PARSE_VERSION}\x00".encode())
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_UPLOAD_NAME_ATTEMPTS = 8


def _open_exclusive_target(directory: Path, name: str) -> tuple[Path, Any]:
    """独占创建一个落盘文件，返回 `(路径, 已打开的写句柄)`；同名同秒也不再共用一条路径。

    ## 它补的是哪一段（2026-10-31 实测）

    原先路径是 `directory / f"{utcnow().replace(':', '')}_{name}"`，而 `utcnow()` 是
    `isoformat(timespec="seconds")` —— **只到秒**。于是「同一 UTC 秒 + 同一个 basename」
    的两篇拿到**同一条路径**，后一份 `open("wb")` 先把前一份截断成 0 字节、再写自己：
    实测同一秒内 **6 次串行**上传 `same.pdf` → **6 行 papers / 1 个文件**，文件里只剩
    最后一份的字节，前 5 行的 `pdf_path` 全指向它，而 6 个请求**全部 201**、日志全绿。
    转换 / 导出 / 「重新提取」读的都是 `pdf_path` ⇒ 那几篇会**静默变成另一篇的副本**。

    这**不是竞态**：同一秒的多次**串行**上传照样撞，`<input type="file" multiple>` 里
    选两个同名 PDF（同一个请求的 `files` 里两个元素）更是**确定性**撞 —— 那条路连并发
    都不需要。所以修法不是加锁，而是让「落盘名唯一」在**文件系统层**成立：`O_CREAT|O_EXCL`
    的创建要么成功，要么说明这个名字已被占，换个名字重试。

    ⚠️ 第一条仍用原来的名字（`<秒级时间戳>_<basename>`），只在**撞上**时才追加短随机
    后缀 —— 常见情形下盘上文件名与修复前一致，按时间戳找文件的习惯不受影响。

    ⚠️ 路径与句柄一起返回，也是给调用方的一道结构保证：调用方拿到的路径**从创建那一刻
    起就属于自己**，所以「写失败就把半截文件删掉」只可能删掉自己这份（同秒另一个请求
    即使 413，也不会顺手删掉前一份已经写好的文件）。
    """
    stamp = utcnow().replace(":", "")
    for attempt in range(_UPLOAD_NAME_ATTEMPTS):
        suffix = "" if attempt == 0 else f"-{uuid4().hex[:8]}"
        candidate = directory / f"{stamp}{suffix}_{name}"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        return candidate, os.fdopen(fd, "wb")
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                        f"无法为 {name} 分配落盘文件名（重试 {_UPLOAD_NAME_ATTEMPTS} 次仍被占用）")


@router.post("/plans/{plan_id}/papers/upload", status_code=201)
async def upload(
    plan_id: int,
    background: BackgroundTasks,
    files: list[UploadFile] = File(...),
    tags: str = Form(""),
    background_convert: bool = Form(True),
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict[str, Any] = Depends(current_user),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    require_plan(conn, plan_id, user)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    # 上传体积上限（MB → 字节）。原先**没有任何上限**：任何大小的文件都收。
    limit = max(1, int(settings.max_upload_mb)) * 1024 * 1024
    created: list[dict[str, Any]] = []
    for uf in files:
        name = Path(uf.filename or "upload.pdf").name
        # **流式落盘**（2026-10-26 生产事故）：原先 `data = await uf.read()` 把整份 PDF
        # 读进内存再 `write_bytes`，于是上传 200 MB 论文 = 200 MB 的常驻内存尖峰，
        # 而 glibc 峰值过后还不归还内核 ⇒ 连传几篇就把整台机器打挂。
        # 现在按 1 MB 一块搬运：峰值只与块大小有关，与文件大小无关。
        # 顺带：`%PDF` 魔数在**第一块**上判、越过上限立刻 413，两种失败都把写了一半的
        # 文件删掉（否则 `papers_dir` 里留下一堆没人认领的孤儿）。
        # **落盘名独占创建**（2026-10-31）：文件名原先只由「秒级时间戳 + basename」决定，
        # 同一秒的同名上传会共用一条路径、后一份把前一份的字节覆盖掉。现在路径在
        # `_open_exclusive_target` 里独占拿到（撞名就换后缀），下面的 `unlink` 因此
        # **只可能删掉本请求自己创建的那个文件** —— 同秒另一个请求 413 也删不到别人的。
        written = 0
        target, handle = _open_exclusive_target(settings.papers_dir, name)
        try:
            with handle:
                while True:
                    chunk = await uf.read(1 << 20)
                    if not chunk:
                        break
                    if written == 0 and not chunk[:5].startswith(b"%PDF"):
                        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} 不是 PDF 文件")
                    written += len(chunk)
                    if written > limit:
                        raise HTTPException(
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"{name} 超过上传上限 {settings.max_upload_mb} MB"
                            f"（配置项 PAPERSHELF_MAX_UPLOAD_MB）")
                    handle.write(chunk)
            if written == 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} 是空文件")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        with tx(conn):
            cur = conn.execute(
                """INSERT INTO papers (plan_id, title, source, source_ref, pdf_path, tags,
                                       status, status_at, conv_state, created_at, updated_at,
                                       title_is_placeholder)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,1)""",
                (plan_id, Path(name).stem, "upload", name, str(target), dump_json(tag_list),
                 "unread", utcnow(), "queued", utcnow(), utcnow()),
            )
            paper_id = int(cur.lastrowid)
        log.info("paper=%s 已入库（用户 %s 上传 %s，%.1f MB，plan=%s），排队等待转换",
                 paper_id, user["email"], name, written / 1e6, plan_id)
        created.append(paper_public(dict(conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())))
        if background_convert:
            background.add_task(convert_paper, paper_id, _pdf_fingerprint(target))
    return created


@router.post("/plans/{plan_id}/papers/arxiv", status_code=201)
def import_arxiv(
    plan_id: int,
    body: ArxivIn,
    background: BackgroundTasks,
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict[str, Any] = Depends(current_user),
) -> dict[str, Any]:
    """⑱：优先抓 arXiv 官方 HTML（`arxiv.org/html/<ID>v<N>`），无则回落 PDF。"""
    ref = body.ref.strip()
    with tx(conn):
        cur = conn.execute(
            # `title` 先写 `arXiv:<ref>` 占位（`title_is_placeholder=1`），
            # 转换时由抽取的真标题覆盖 —— 否则"arXiv:2603.19455"会一直当标题显示。
            """INSERT INTO papers (plan_id, title, source, source_ref, status, status_at,
                                   conv_state, created_at, updated_at, title_is_placeholder)
               VALUES (?,?,?,?,?,?,?,?,?,1)""",
            (plan_id, f"arXiv:{ref}", "arxiv", ref, "unread", utcnow(), "queued",
             utcnow(), utcnow()),
        )
        paper_id = int(cur.lastrowid)
    log.info("paper=%s 已入库（arXiv:%s，plan=%s），排队等待转换", paper_id, ref, plan_id)
    background.add_task(convert_paper, paper_id, None)
    row = dict(conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())
    return paper_public(row)


@router.get("/plans/{plan_id}/papers")
def list_papers(
    plan_id: int,
    status_: str | None = None,
    q: str | None = None,
    sort: str = "created_desc",
    conn: sqlite3.Connection = Depends(get_conn),
    user: dict[str, Any] = Depends(current_user),
) -> list[dict[str, Any]]:
    require_plan(conn, plan_id, user)
    sql = "SELECT * FROM papers WHERE plan_id=?"
    args: list[Any] = [plan_id]
    if status_:
        sql += " AND status=?"
        args.append(status_)
    if q:
        sql += " AND (title LIKE ? OR authors LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    order = {
        "created_desc": "created_at DESC", "created_asc": "created_at ASC",
        "title": "title COLLATE NOCASE ASC", "year": "year DESC",
        "progress": "progress DESC",
    }.get(sort, "created_at DESC")
    sql += f" ORDER BY {order}"
    return [paper_public(dict(r)) for r in conn.execute(sql, args).fetchall()]


@router.patch("/papers/{paper_id}")
def patch_paper(paper_id: int, body: PaperPatch, conn: sqlite3.Connection = Depends(get_conn),
                user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    require_paper(conn, paper_id, user)
    # ⚠️ `body.x is not None` 分不清「没传这个字段」和「显式传 null 来清空」：
    #    编辑抽屉里把年份删空会发 `year: null`，旧逻辑静默忽略 → 界面上"保存成功但清不掉"。
    #    `model_fields_set` 才是"这次请求实际带了哪些字段"。
    provided = body.model_fields_set
    sets, args = [], []
    for field in ("title", "authors", "venue", "year"):
        if field not in provided:
            continue
        sets.append(f"{field}=?")
        args.append(getattr(body, field))
    # ⑲：用户一旦亲手填过标题，就把「占位值」标记撤掉 —— 从此自动抽取
    # （含「重跑转换」）都不得再覆盖它。反之不撤的话，用户填的标题会被下一次
    # 转换抽出来的真标题悄悄顶掉，等于白填。清空标题则视同仍是占位（没什么可保护的）。
    if (body.title or "").strip():
        sets.append("title_is_placeholder=0")
    if body.tags is not None:
        sets.append("tags=?")
        args.append(dump_json(body.tags))
    if body.status_ is not None:
        if body.status_ not in ("unread", "reading", "read", "reviewed"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "状态取值不合法")
        sets += ["status=?", "status_at=?"]
        args += [body.status_, utcnow()]
        # ⑰：手动标记「已读」→ 进度锁 100%，之后与实际滚动无关
        if body.status_ in ("read", "reviewed"):
            sets += ["progress=?", "progress_mode=?"]
            args += [100, "manual"]
        elif body.status_ == "unread":
            # 标回「未读」= 重新开始读 → 进度清零。
            # 否则会出现"未读 但 80%"的自相矛盾状态（进度只增不减后尤其明显）。
            sets += ["progress=?", "progress_mode=?"]
            args += [0, "auto"]
        elif body.progress is None:
            sets += ["progress_mode=?"]
            args.append("auto")
    if body.progress is not None:
        if not 0 <= body.progress <= 100:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "进度须在 0-100")
        if body.progress_by not in (None, "scroll", "user"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "progress_by 取值不合法")
        # 谁在报数（见 `PaperPatch.progress_by`）：`user` = 手改，无条件生效；否则是滚动上报。
        by_user = body.progress_by == "user"
        row = conn.execute("SELECT status, progress, progress_mode FROM papers WHERE id=?",
                           (paper_id,)).fetchone()
        # ⑰：手动锁定后，滚动上报不得把它降下来（手动优先）
        if not by_user and body.status_ is None and row and row["progress_mode"] == "manual":
            raise HTTPException(status.HTTP_409_CONFLICT, "已标记已读，进度已锁定")
        # ⑰ 补充（2026-09-15，宿主：「我有篇文章读了 13%，为什么『在读』还是 0 篇」）：
        # 这条路径 = **阅读器滚动上报**，也就是"此刻正在读"。所以：
        #  ① 记 `last_read_at`（「最近阅读」的**唯一**数据来源；改标题/改标签/拖看板都不写它，
        #     于是这个字段只可能表示"最后一次真正阅读"）。
        #  ② 还在「待读」就自动翻到「在读」——"开始读了"这件事滚动位置是知道的；
        #     而"读懂没有"仍然只有人知道，所以「已读/已整理」**不自动**，仍旧只能手动拖。
        #  ③ `status_at` **只在翻转的那一刻**盖一次，不能每次滚动都盖：
        #     否则它退化成"最近滚动时间"，看板列内排序与总览那条"停摆"提醒全会失真。
        # ⚠️ **手改不是"在读"的证据**：上面三条一条都不适用于 `progress_by="user"`
        #    （填一个数字不等于此刻在读、更不等于"动手改的这一下发生在阅读中"），
        #    否则「最近阅读」会变成一个"最近改过进度"的时间戳，它存在的意义就没了。
        if not by_user and body.status_ is None and row is not None:
            sets.append("last_read_at=?")
            args.append(utcnow())
            if row["status"] == "unread" and row["progress_mode"] == "auto":
                sets += ["status=?", "status_at=?"]
                args += ["reading", utcnow()]
        # ⚠️ auto 模式下进度**只增不减**（踩过）：前端滚动上报是节流的，
        #    从底部快速回到顶部时"最后由下往上"的那次上报会把 100% 拽回 3%，
        #    进度条像坏了一样往回退。自动累计的语义就是"读到过哪里"，
        #    回退没有任何含义；要往回改就手动把状态设为「在读」。
        #    ⚠️ 只跳过 `progress` 的写入 —— 上面的 `last_read_at` 与状态翻转照常生效
        #    （往下读还是翻回来，都是在读这篇）。
        # ⚠️ 这条跳过**只针对滚动上报**：手改（`by_user`）是宿主 2026-09-19 点单的那件事
        #    本身（"手动修改进度"），跳过就等于功能没做。宿主当日选 **B**：手改**不锁**
        #    （`progress_mode` 仍是 `auto`）—— 它只是"把这个数字修正一下"，滚动继续按
        #    "只增不减"累计；代价是改小之后继续往下滚会被再抬上去，宿主已知情并接受。
        if by_user:
            sets.append("progress=?")
            args.append(body.progress)
        elif body.status_ is None and row and row["progress_mode"] == "auto" \
                and body.progress <= int(row["progress"] or 0):
            pass
        else:
            sets.append("progress=?")
            args.append(body.progress)
    if sets:
        sets.append("updated_at=?")
        args += [utcnow(), paper_id]
        with tx(conn):
            conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id=?", args)
    return paper_public(dict(conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()))


@router.delete("/papers/{paper_id}", status_code=204)
def delete_paper(paper_id: int, conn: sqlite3.Connection = Depends(get_conn),
                 user: dict[str, Any] = Depends(current_user),
                 settings: Settings = Depends(get_settings)) -> None:
    """删除一篇文献（含译文块、笔记、磁盘上的 PDF 与图片资产）。

    DB 侧靠外键级联（`docs`/`blocks`/`notes` 都是 `ON DELETE CASCADE`），
    **磁盘侧必须自己收** —— 否则删完再导入同一篇，哈希缓存会命中**旧文件的解析产物**，
    用户看到的是"删了但没删干净"（图还在、页还在）。
    """
    paper = require_paper(conn, paper_id, user)
    with tx(conn):
        conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
    _remove_paper_files(settings, paper.get("pdf_path"), paper_id)


def _remove_paper_files(settings: Settings, pdf_path: str | None, paper_id: int) -> None:
    """删除磁盘产物，**失败只记日志不抛** —— 行已经从库里删了，此时 500 会让用户以为没删掉。

    两个位置：落盘的 PDF（`papers_dir/<时间戳>_<原名>.pdf`）与图片资产目录
    （`papers_dir/p<id>/`）。目录用 `shutil.rmtree` 整个收掉，不然会留一堆孤儿 PNG。
    """
    for target in ([Path(pdf_path)] if pdf_path else []) + [settings.papers_dir / f"p{paper_id}"]:
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except OSError as exc:                       # noqa: PERF203
            log.warning("删除磁盘产物失败（已忽略）：%s：%s", target, exc)


@router.get("/papers/{paper_id}/doc")
def get_doc(paper_id: int, conn: sqlite3.Connection = Depends(get_conn),
            user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """阅读器取数（决策⑳：块级 JSON 是单一事实来源）。"""
    paper = require_paper(conn, paper_id, user)
    data = doc_public(conn, paper_id, asset_prefix=f"/papers/{paper_id}/assets")
    if data is None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"转换尚未完成（当前状态：{paper['conv_state']}）")
    return data


@router.post("/papers/{paper_id}/convert", status_code=202)
def retrigger(paper_id: int, background: BackgroundTasks,
              conn: sqlite3.Connection = Depends(get_conn),
              user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """失败重试入口（§5.7：不阻塞其它文献）。

    `enqueue(..., job=None)` 是**必须显式写**的一步：把 `pending_job` 清成"完整转换"，
    否则上一次「重建参考文献」留下的 `job='refs'` 会让这次重试**又跑一遍修文献**
    （用户点的是"重新转换"，跑的是别的事 —— 静默且难查）。
    `conv_attempts` 归零：**人工主动重试不该被自动护栏挡住**。`docs/design.md` 的原话是
    「超限置 failed **待人工重试**」—— 人工重试若不能重置计数，这句话就是空话。
    ⚠️ 修 `claim_paper` 之前计数恒为 0，这条护栏从未生效（2026-09-15 一并修）。
    """
    paper = require_paper(conn, paper_id, user)
    if paper["conv_state"] == "doing":
        raise HTTPException(status.HTTP_409_CONFLICT, "该文献正在转换中")
    enqueue(conn, paper_id, reset_attempts=True)
    fingerprint = None
    if paper["pdf_path"] and Path(paper["pdf_path"]).exists():
        fingerprint = _pdf_fingerprint(Path(paper["pdf_path"]))
    background.add_task(convert_paper, paper_id, fingerprint)
    return {"ok": True, "conv_state": "queued"}


@router.post("/papers/{paper_id}/reextract", status_code=202)
def reextract(paper_id: int, background: BackgroundTasks,
              conn: sqlite3.Connection = Depends(get_conn),
              user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """**重新提取**（宿主 2026-09-15）：清掉解析缓存，从头再跑一遍管线。

    ## 为什么必须有这个入口：`doc_cache` 原本**没有失效机制**

    指纹 = `sha256("parse-v{PARSE_VERSION}" + PDF 字节)`，PDF 一字未改就**永远命中**；
    唯一的失效手段是改代码里的 `PARSE_VERSION` —— 那是**全库一起失效**，代价是所有篇目
    一起重做 LaTeX 化与翻译。于是「这一篇的解析/校对结果我不满意，重来一次」这类
    **单篇诉求没有任何出口**（界面上的「重新转换」同样会命中缓存，等于什么都没重来）。

    ## 语义 = **全部作废，从零重跑**（宿主选 A）

    - `doc_cache` 行删除 → 重新解析、**全页重跑 ①c 校对**（`ok_pages` 就存在缓存里）、
      公式重新 LaTeX 化、按需重译；
    - `notes` / `highlights` **立即删除**：它们锚在 `(block_id, lang, start, end)` 上，
      新解析出来的块 id 与文本都会变，留着只会指到别的字上 —— 那比"没了"更坏
      （用户看到自己的批注挂在不相关的句子上）；
    - 译文：新转换成功时由 `save_doc` 整篇覆盖；`zh_source='human'` 的人工修订只在
      **同一次运行的翻译循环内**豁免，跨运行不保留 → 一并作废（决策⑯ 的既定例外）；
    - `conv_attempts` 归零：这是用户主动发起的新一轮，不该被上一轮的失败次数挡住；
    - 磁盘上的旧图片资产**不删**：新解析会按 `p{page}_img{n}` 同名覆盖，删了反而让
      "转换期间/转换失败"时旧图裂掉（残留的孤儿 PNG 无害）。

    ⚠️ 这是**有 token 代价**的操作（≈ 校对 + 公式 + 翻译整篇），确认框里说清。
    """
    paper = require_paper(conn, paper_id, user)
    if paper["conv_state"] == "doing":
        raise HTTPException(status.HTTP_409_CONFLICT, "该文献正在转换中")

    fingerprint = None
    pdf = Path(paper["pdf_path"]) if paper["pdf_path"] else None
    if pdf is not None and pdf.exists():
        fingerprint = _pdf_fingerprint(pdf)

    with tx(conn):
        if fingerprint:
            conn.execute("DELETE FROM doc_cache WHERE fingerprint=?", (fingerprint,))
        conn.execute("DELETE FROM notes WHERE paper_id=?", (paper_id,))
        conn.execute("DELETE FROM highlights WHERE paper_id=?", (paper_id,))
    # 排队**在事务之外**用共用入口：`job=None` 清掉可能残留的 `pending_job`
    # （同 `retrigger` 的理由 —— 这里刻意点明是"完整转换"，不是"修文献"）。
    enqueue(conn, paper_id, reset_attempts=True)
    log.info("paper=%s 重新提取：解析缓存已清（fingerprint=%s…）/ 笔记与划痕作废 → 重新排队",
             paper_id, fingerprint[:12] if fingerprint else "无（arXiv 路线）")
    background.add_task(convert_paper, paper_id, fingerprint)
    return {"ok": True, "conv_state": "queued", "cache_cleared": bool(fingerprint)}


@router.post("/papers/{paper_id}/rebuild-refs", status_code=202)
def rebuild_refs(paper_id: int, background: BackgroundTasks,
                 conn: sqlite3.Connection = Depends(get_conn),
                 user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """**重建参考文献**（㊹ 修订的存量出口，2026-09-19，宿主点单）。

    宿主原话：「生产上你第一篇 pdf 我已经添加了不少笔记了，想办法在不影响笔记的情况下，
    可以手动改一下后面的 References？」

    ## 它与「重新提取」的区别（这是整个入口存在的理由）

    | | 重新提取 | 重建参考文献 |
    |---|---|---|
    | 跑什么 | 解析 → 公式 → ①c 校对 → 翻译 → 校验（整篇） | 只重跑**文末文献段**的条目合并 |
    | 笔记/划痕 | **全部删除** | **一条不动**（正文块 id 不变；钉在被合并碎片上的按坐标搬家） |
    | 正文译文 | 全部重译 | 一个字不碰 |
    | 代价 | ≈ 百万 tokens 量级 | 只译新增条目（生产那篇 ≈30 万） |

    为什么「重新提取」不能替代它：批注锚在 `(block_id, lang, start, end)` 上，重解析后
    块 id 与文本都会变，所以那个出口必须把批注删掉 —— 对已经读过并做了批注的篇目即等于
    "拿不回以前的工作"。详见 `server/refsfix.py` 的模块 docstring。

    ## 异步而非同步

    一次几百条条目要跑好几分钟（每条一次 LLM 调用），所以走**常驻队列**
    （`pending_job='refs'` → `converter._run_refs_fix`），与转换共用并发闸门、
    共享启动恢复。返回 202 后前端看 `conv_state` 即可（与重新提取同一套观感）。

    幂等：已经修过的篇目再点一次是 no-op（不落库、不调模型，`changed=false`）。
    """
    paper = require_paper(conn, paper_id, user)
    if paper["conv_state"] == "doing":
        raise HTTPException(status.HTTP_409_CONFLICT, "该文献正在转换中")
    if paper["conv_state"] != "done":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"该文献还没有可修复的产物（当前状态：{paper['conv_state']}）")
    enqueue(conn, paper_id, job="refs", reset_attempts=True)
    log.info("paper=%s 重建参考文献：已排入队列（只动文末文献段，笔记与划痕保留）", paper_id)
    background.add_task(convert_paper, paper_id, None)
    return {"ok": True, "conv_state": "queued", "job": "refs"}


@router.post("/papers/{paper_id}/rebuild-assets", status_code=202)
def rebuild_assets(paper_id: int, background: BackgroundTasks,
                   conn: sqlite3.Connection = Depends(get_conn),
                   user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """**修复插图**（存量出口）：把这一篇缺的图片文件补回来。零 token。

    ## 它补的是哪一类洞（离线实测，`tests/test_assets_rebuild.py` 钉住）

    `#7`（`84e94bc`）让「命中解析缓存时把资产搬进本篇目录」成立，但那段代码**只在整条
    转换里跑**（`converter._run`，全仓库唯一调用点）⇒ 已经 `done` 的篇目**永远不会再进它**。
    于是磁盘上少一张图就是**永久 404**，而现存三条出口没有一条能把文件放回去：

    | 出口 | 跑什么 | 图片会回来吗 |
    |---|---|---|
    | `POST /papers/{id}/convert` | `job=None` → 整条管线（重译，百万级 token） | 否 |
    | `POST /papers/{id}/reextract` | 清缓存 + **`DELETE FROM notes/highlights`** | 是，但批注全没 |
    | `POST /papers/{id}/rebuild-refs` | 只重跑文末文献段 | 否 |
    | `POST /papers/{id}/rebuild-assets`（本入口） | 只复制缺的图片文件 | **是** |

    可发生面不是"新用户"：`#7` 之前转换的存量篇目（生产上有）、`p<id>/assets/` 被磁盘
    清理或恢复丢失、同一份 PDF 的**同源那篇被删文献**（整目录被收走，缓存行却还在）。

    ## 为什么不自己做身份判断，而是要么"来源确定"要么"就 409"

    资产文件名**按页编号**（`p1_img1.png`），与篇无关 ⇒ 随手去别处找个同名文件搬过来，
    修出来的不是"裂图"而是**别人的图**（`#8` 修的正是这个失败模式的分享页版本：肉眼极难
    发现）。所以本入口只承认两种来源：解析时记进 `meta["assets_dir"]` 的那一个目录，或者
    "同一份 PDF 在同一个 `PARSE_VERSION` 下重新解析、且块 id 与原文逐字相同"的产物。
    后者量不过就**放弃**，并点名「重新提取」及其代价 —— 破坏性出口由用户自己选，不由我们代签。

    ## 异步

    重新解析那一支是秒级但会阻塞，所以走常驻队列（`pending_job='assets'` →
    `converter._run_assets_fix`），与转换共用并发闸门与启动恢复；请求当下只做能立刻判定的
    「没有产物可修」409。幂等：插图齐全时再点是 no-op（不复制、不解析，`changed=False`）。
    """
    paper = require_paper(conn, paper_id, user)
    if paper["conv_state"] == "doing":
        raise HTTPException(status.HTTP_409_CONFLICT, "该文献正在转换中")
    if paper["conv_state"] != "done":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"该文献还没有可修复的产物（当前状态：{paper['conv_state']}）")
    enqueue(conn, paper_id, job="assets", reset_attempts=True)
    log.info("paper=%s 修复插图：已排入队列（只补图片文件，笔记与划痕保留，零 token）", paper_id)
    background.add_task(convert_paper, paper_id, None)
    return {"ok": True, "conv_state": "queued", "job": "assets"}


@router.get("/papers/{paper_id}/export")
def export(paper_id: int, request: Request, lang: str = "dual", inline: bool = True,
           conn: sqlite3.Connection = Depends(get_conn),
           user: dict[str, Any] = Depends(current_user)):
    """导出单文件 HTML（决策⑳：JSON → HTML 是派生轨道）。

    `inline=True`（默认）把图片内嵌成 data URI → **真正单文件**，可离线分发、可直接打印。
    `inline=False` 时图片走 `/papers/<id>/assets/...`（便于只看不下载）。

    内联那条走**流式**（`app.py::stream_inlined_export`）：峰值不再随图片总量增长，
    总体积还有 `PAPERSHELF_MAX_EXPORT_MB` 兜底（超限 413，错误里点名 `inline=0`）。
    """
    from fastapi.responses import HTMLResponse, StreamingResponse

    from ...pipeline import synth
    from ..repo import load_doc

    paper = require_paper(conn, paper_id, user)
    doc = load_doc(conn, paper_id)
    if doc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "转换尚未完成")
    mode = "dual" if lang == "dual" else ("en" if lang == "en" else "zh")
    # 始终先指向可 HTTP 取到的资产 URL；内联模式再把它们换成 data URI
    html = synth(doc.blocks, mode, title=doc.meta.get("title_zh") or doc.meta.get("title_en", ""),
                 meta_line=doc.meta.get("authors", ""),
                 asset_prefix=f"/papers/{paper_id}/assets")
    headers = {
        "Content-Disposition": f'{("inline" if request.query_params.get("view") else "attachment")}; '
                               f'filename="{paper_id}-{mode}.html"'
    }
    if not inline:
        return HTMLResponse(html, headers=headers)
    return StreamingResponse(request.app.state.stream_inlined_export(html, paper_id),
                             media_type="text/html; charset=utf-8", headers=headers)


@router.get("/papers/{paper_id}/html")
def paper_html(paper_id: int, conn: sqlite3.Connection = Depends(get_conn),
               user: dict[str, Any] = Depends(current_user)) -> dict[str, str]:
    """调试用：返回带标记的英文 HTML（校验器与管线自检需要）。"""
    from ..repo import load_doc

    require_paper(conn, paper_id, user)
    doc = load_doc(conn, paper_id)
    if doc is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "转换尚未完成")
    return {"en": en_html(doc, typeset=False)}
