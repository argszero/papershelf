"""认领**之前**失败的一篇，必须**有界**地落终态，而不是永远回到队首。

2026-10-30 实测的病灶由三处合起来构成：

1. `queue.run_once` **刻意不认领**（`doing` 只能出现在拿到并发槽位之后 ——
   2026-09-12 事故定下的顺序铁律）；
2. `conv_attempts + 1` **只**由 `converter.claim_paper` 做；
3. `queue._deliver` 的 `except` 原本只 `log.exception`。

于是「认领之前」的异常（`_fingerprint_for` 在 `run` 之前读 PDF，遇到
「存在但读不出」就抛）既不计数、也不写 `conv_error`、也不落终态：行原样停在
`queued`，下一轮再投一次 —— 永远。实测 5 轮交付 5 次、`convert_paper` 一次没进、
计数恒 0；真轮询线程 2 秒投 78 次（生产 `POLL_SECONDS=3.0` ⇒ 1200 次/小时）。

⚠️ 三条**诚实边界**（别把这个缺陷说成比实测更大）：

- **不烧钱**：一次也没走到 LLM，`tokens_used` 恒 0 —— 这是活性 + 可见性缺陷；
- **不占槽位**：`IsADirectoryError` 立刻抛，槽位微秒级还回去（只有读操作真的卡住，
  例如网络卷挂起，才会占住槽位）；
- **触发面很窄**：必须「存在但读不出来」。**文件被删掉是安全的**（用例 3 钉住它）。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path


def _mk_paper(settings, *, conv_state: str = "queued", pdf_path: str | None = None,
              title: str = "t") -> int:
    """建一篇文献（用户与计划**复用**，否则第二个用例就撞 UNIQUE）。"""
    from papershelf.server.db import connect
    from papershelf.server.security import create_user

    conn = connect(settings)
    try:
        row = conn.execute("SELECT id FROM users WHERE email='a@x.edu.cn'").fetchone()
        if row is None:
            create_user(conn, email="a@x.edu.cn", password="password123", status_="active")
            uid = 1
        else:
            uid = int(row[0])
        pl = conn.execute("SELECT id FROM plans WHERE user_id=?", (uid,)).fetchone()
        if pl is None:
            pl_id = conn.execute("INSERT INTO plans (user_id,name,created_at,updated_at) "
                                 "VALUES (?,'p',datetime('now'),datetime('now'))",
                                 (uid,)).lastrowid
        else:
            pl_id = pl[0]
        pid = conn.execute(
            """INSERT INTO papers (plan_id,title,source,status,conv_state,pdf_path,
                                   created_at,updated_at)
               VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))""",
            (int(pl_id), title, "upload", "unread", conv_state, pdf_path)).lastrowid
        conn.commit()
        return int(pid)
    finally:
        conn.close()


def _row(settings, pid: int) -> tuple[str, int, str | None]:
    """(conv_state, conv_attempts, conv_error) —— 界面能看到的三样东西。"""
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        r = conn.execute("SELECT conv_state, conv_attempts, conv_error FROM papers "
                         "WHERE id=?", (pid,)).fetchone()
        return str(r["conv_state"]), int(r["conv_attempts"]), r["conv_error"]
    finally:
        conn.close()


def test_unreadable_pdf_becomes_terminal_instead_of_retrying_forever(settings):
    """**核心回归**（实测形态 A）：`pdf_path` 指向一个目录 —— 存在、但读不出。

    修前：5 轮交付 5 次，`queued` / `conv_attempts=0` / `conv_error=NULL`，
    下一轮还会再来一次（没有终态、没有原因、界面没有按钮）。
    修后：第 `max_conv_attempts` 轮就落 `failed`，计数到顶，原因写明**真实异常**，
    之后的轮次交付 0 篇 —— **有界**。

    ⚠️ 这里**不注入 runner**：跑的就是真 `convert_paper`，异常发生在
    `_fingerprint_for` 里，比任何替身都更接近生产的那条路径。
    """
    from papershelf.server.queue import run_once

    broken = Path(tempfile.mkdtemp(prefix="papershelf-unreadable-"))  # 目录 ⇒ 读即抛
    pid = _mk_paper(settings, pdf_path=str(broken))

    delivered = [run_once(settings) for _ in range(5)]

    state, attempts, err = _row(settings, pid)
    assert state == "failed", "到顶必须落终态，否则这篇永远回到队首"
    assert attempts == settings.max_conv_attempts, "计数必须停在护栏上限，不能超过"
    assert "IsADirectoryError" in (err or ""), f"原因必须是真实异常，实测 {err!r}"
    assert delivered == ([1] * settings.max_conv_attempts
                         + [0] * (5 - settings.max_conv_attempts)), \
        f"落终态之后不得再交付（实测交付序列 {delivered}）"


def test_readable_pdf_keeps_its_single_attempt(settings):
    """**不得重复计数**（实测形态 C）：可读的 PDF 走的是**认领之后**失败。

    这条路径原本就正确（认领即 +1、`convert_paper` 自己写 `failed`），
    新逻辑必须一个字段都不碰。若有人把「所有异常都记账」写宽了，这里会变成 2。
    """
    from papershelf.server.queue import run_once

    pdf = Path(tempfile.mkdtemp(prefix="papershelf-readable-")) / "ok.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% readable but not parsed (no LLM configured)\n")
    pid = _mk_paper(settings, pdf_path=str(pdf))

    assert run_once(settings) == 1
    state, attempts, err = _row(settings, pid)
    assert state == "failed" and attempts == 1, "认领后的失败由 convert_paper 记账"
    assert "RuntimeError" in (err or ""), "未配 LLM ⇒ 认领之后才失败，原因照常可读"

    assert run_once(settings) == 0, "failed 不是队列的活"
    assert _row(settings, pid) == (state, 1, err), "再扫一轮不得改动任何字段"


def test_missing_pdf_keeps_its_single_attempt(settings):
    """**边界**（实测形态 D）：PDF 文件**被删掉**是安全的。

    `path.exists()` 为 False ⇒ `_fingerprint_for` 回 None（不抛）⇒ 正常认领 ⇒
    认领之后的失败。这条用例存在的意义是把「存在但读不出」与「根本不存在」分开 ——
    修法的触发面只有前者。
    """
    from papershelf.server.queue import run_once

    gone = Path(tempfile.mkdtemp(prefix="papershelf-missing-")) / "gone.pdf"
    assert not gone.exists()
    pid = _mk_paper(settings, pdf_path=str(gone))

    assert run_once(settings) == 1
    state, attempts, err = _row(settings, pid)
    assert state == "failed" and attempts == 1
    assert run_once(settings) == 0
    assert _row(settings, pid) == (state, 1, err)


def test_post_claim_exception_is_not_counted_twice(settings):
    """**判据的本体**：认领之后抛出的异常，队列不得再给它记一次账。

    模拟的是真实顺序 —— 先 `claim_paper`（`doing` + 计数），再在认领**之后**炸掉
    （例如 `_set_state` / `save_doc` 自己抛），异常逃到 `_deliver`。
    那条 `WHERE conv_state='queued'` 的 `rowcount` 必须为 0 ⇒ 状态、计数、原因
    一个字段都不动。少了这个判据，重试预算会凭空少一半。
    """
    from papershelf.server.converter import claim_paper
    from papershelf.server.db import connect
    from papershelf.server.queue import run_once

    pid = _mk_paper(settings, conv_state="queued")

    def runner(paper_id: int, fingerprint):
        conn = connect(settings)
        try:
            assert claim_paper(conn, paper_id) is True, "替身也要走真实认领"
        finally:
            conn.close()
        raise OSError("认领之后才炸（模拟 _set_state/save_doc 自身异常）")

    assert run_once(settings, runner=runner) == 1
    assert _row(settings, pid) == ("doing", 1, None), \
        "认领后的异常已由 convert_paper 记过账，队列再记一次就是重复计数"


def test_polling_thread_makes_the_unreadable_paper_terminal(settings):
    """端到端证人：真轮询线程，短间隔。修前 2 秒能投几十次且**永不收敛**。

    修后它在 `max_conv_attempts` 轮内落 `failed`，线程随即不再捡它
    （`pending()` 只看 `queued`）—— 「交付次数」与「终态」两件事一起验。
    """
    from papershelf.server.queue import ConversionQueue

    broken = Path(tempfile.mkdtemp(prefix="papershelf-poll-"))
    pid = _mk_paper(settings, pdf_path=str(broken))

    q = ConversionQueue(settings, interval=0.02)
    q.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _row(settings, pid)[0] == "queued":
            time.sleep(0.02)
    finally:
        q.stop(timeout=5.0)

    state, attempts, err = _row(settings, pid)
    assert state == "failed", f"轮询线程必须把它收敛到终态（实测 {state}）"
    assert attempts == settings.max_conv_attempts
    assert "IsADirectoryError" in (err or "")


def test_bookkeeping_never_kills_the_polling_thread(settings, monkeypatch):
    """兜底出口原本唯一的职责是「一篇炸掉不能带走轮询线程」，记账不能反客为主。

    这里让记账**自己**抛（库被锁 / 磁盘满 —— `sqlite3.OperationalError` 是最可能的形状），
    后面那篇必须照跑。

    ⚠️ 坏掉的必须是**另一篇**且带不可读的 `pdf_path`：`_deliver` 先算指纹再 `run`，
    所以那一篇的异常出在 `_fingerprint_for` 里，注入的 runner 根本不会被调用 ——
    这正是「认领前失败」的形状，也是记账被触发的唯一入口。
    """
    import sqlite3

    from papershelf.server import queue as q
    from papershelf.server.queue import ConversionQueue

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("模拟记账失败")

    monkeypatch.setattr(q, "record_preclaim_failure", boom)

    broken = _mk_paper(settings, conv_state="queued",
                       pdf_path=str(Path(tempfile.mkdtemp(prefix="papershelf-boom-"))),
                       title="boom")
    ok = _mk_paper(settings, conv_state="queued", title="ok")

    seen: list[int] = []
    queue_obj = ConversionQueue(settings, interval=0.02,
                                runner=lambda pid, fp: seen.append(pid))
    queue_obj.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ok not in seen:
            time.sleep(0.02)
    finally:
        queue_obj.stop(timeout=5.0)

    assert ok in seen, "记账抛异常也必须继续处理队列里的其它文献"
    assert broken not in seen, "它死在指纹那一步，`convert_paper` 不该被调用"
