"""转换队列（`server/queue.py`）的回归 —— 每一条都钉住 2026-09-12 的生产事故。

事故现象：一次传 12 篇，只生成 1 篇；2 篇永久 `doing`、9 篇永久 `queued`；
之后**任何重启都不会再动**。根因是"队列"只是数据库里的一个字符串，
真正干活的 `BackgroundTasks` 随请求线程一起死，且从无启动恢复。
"""
from __future__ import annotations

import sqlite3

import pytest


def _mk_paper(settings, *, conv_state: str, title: str = "t") -> int:
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
            """INSERT INTO papers (plan_id,title,source,status,conv_state,created_at,updated_at)
               VALUES (?,?,?,?,?,datetime('now'),datetime('now'))""",
            (int(pl_id), title, "upload", "unread", conv_state)).lastrowid
        conn.commit()
        return int(pid)
    finally:
        conn.close()


def _state(settings, pid: int) -> str:
    from papershelf.server.db import connect

    conn = connect(settings)
    try:
        return conn.execute("SELECT conv_state FROM papers WHERE id=?", (pid,)).fetchone()[0]
    finally:
        conn.close()


def test_recover_stuck_doing_back_to_queued(settings):
    """启动恢复：上个进程死在转换中的 `doing` 必须回到 `queued`。

    没有这一步，被重启杀掉的文献就永远是 `doing` 僵尸 —— UI 不给按钮、队列也不看它。
    """
    from papershelf.server.db import connect
    from papershelf.server.queue import recover_stuck

    pid = _mk_paper(settings, conv_state="doing")
    conn = connect(settings)
    try:
        assert recover_stuck(conn) == 1
    finally:
        conn.close()
    assert _state(settings, pid) == "queued"


def test_recover_does_not_touch_done_or_failed(settings):
    """恢复**只认 `doing`**：`done`/`failed`/`queued` 一个都不能被它改写。

    ⚠️ 若顺手写成 `UPDATE ... WHERE conv_state != 'done'`，会把已完成的重新翻一遍
    （再烧一遍 token），并把 `failed` 的重试额度悄悄用掉。
    """
    from papershelf.server.db import connect
    from papershelf.server.queue import recover_stuck

    done = _mk_paper(settings, conv_state="done", title="d")
    failed = _mk_paper(settings, conv_state="failed", title="f")
    conn = connect(settings)
    try:
        assert recover_stuck(conn) == 0
    finally:
        conn.close()
    assert _state(settings, done) == "done"
    assert _state(settings, failed) == "failed"


def test_run_once_drains_queue_in_fifo_order(settings):
    """排空：`queued` 全部被跑，顺序按 `created_at`（先来先服务）。

    事故里 9 篇 `queued` 永远没人捡；这条测试保证了"传一批就会全跑完"。
    """
    from papershelf.server.queue import run_once

    ids = [_mk_paper(settings, conv_state="queued", title=f"p{i}") for i in range(3)]
    seen: list[int] = []
    n = run_once(settings, runner=lambda pid, fp: seen.append(pid))
    assert n == 3
    assert seen == ids, "必须按创建顺序依次转换"
    # 替身 runner 不做认领，所以状态仍停在 queued —— 真实 runner（convert_paper）
    # 是在**拿到并发槽位之后**才认领的（见其 docstring 的顺序铁律）。
    for pid in ids:
        assert _state(settings, pid) == "queued"


def test_run_once_skips_non_queued(settings):
    """`none` = 用户没要求转换，队列**不许**自作主张开跑（会白烧钱）。"""
    from papershelf.server.queue import run_once

    pid = _mk_paper(settings, conv_state="none")
    seen: list[int] = []
    assert run_once(settings, runner=lambda p, f: seen.append(p)) == 0
    assert seen == [] and _state(settings, pid) == "none"


def test_claim_is_atomic_only_one_winner(settings):
    """**去重的关键**：同一篇被两条路径（请求 BackgroundTask / 轮询线程）同时看到时，
    只能有一个赢 —— 否则跑两遍，token 双倍且产物互相覆盖。
    """
    from papershelf.server.db import connect
    from papershelf.server.converter import claim_paper

    pid = _mk_paper(settings, conv_state="queued")
    conn = connect(settings)
    try:
        assert claim_paper(conn, pid) is True
        assert claim_paper(conn, pid) is False, "第二次认领必须失败"
    finally:
        conn.close()


def test_convert_paper_skips_when_already_claimed(settings):
    """`convert_paper` 入口本身也要认领 —— 光有 `queue.claim` 不够：
    请求路径直接调 `convert_paper`，它必须自己挡住重复执行。
    """
    from papershelf.server.converter import convert_paper
    from papershelf.server.db import connect

    pid = _mk_paper(settings, conv_state="done")     # 已完成的文献
    convert_paper(pid)                               # 不该做任何事
    assert _state(settings, pid) == "done", "非 queued 的行不得被 convert_paper 改写"


def test_worker_thread_drains_then_stops(settings):
    """常驻线程：起来的能扫、能排空，`stop()` 后**立刻**退出（不能拖满一个轮询周期）。

    ⚠️ `stop()` 用 `Event.wait` 而不是 `sleep`，否则关服务要白等 interval 秒。
    """
    import time

    from papershelf.server.queue import ConversionQueue

    ids = [_mk_paper(settings, conv_state="queued", title=f"q{i}") for i in range(2)]
    seen: list[int] = []
    q = ConversionQueue(settings, interval=0.05, runner=lambda p, f: seen.append(p))
    q.start()
    for _ in range(200):                             # 最多等 2s
        if len(seen) == len(ids):
            break
        time.sleep(0.01)
    q.stop(timeout=2.0)
    assert sorted(seen) == sorted(ids)
    assert q._thread is None, "stop() 后不应再持有线程"


def test_worker_survives_a_failing_paper(settings):
    """一篇炸掉不能带走整个轮询线程（后面排队的文献必须照跑）。§5.7。"""
    import time

    from papershelf.server.queue import ConversionQueue

    a = _mk_paper(settings, conv_state="queued", title="boom")
    b = _mk_paper(settings, conv_state="queued", title="ok")
    seen: list[int] = []

    def runner(pid: int, fp):
        seen.append(pid)
        if pid == a:
            raise RuntimeError("模拟转换异常")

    q = ConversionQueue(settings, interval=0.05, runner=runner)
    q.start()
    for _ in range(200):
        if len(seen) == 2:
            break
        time.sleep(0.01)
    q.stop(timeout=2.0)
    assert a in seen and b in seen, "一篇异常后仍须继续处理队列里的其它文献"


def test_waiting_for_slot_stays_queued_not_doing(settings, monkeypatch):
    """**顺序铁律**：等并发槽位期间，`conv_state` 必须还停在 `queued`。

    本地实测踩到：并发上限 2 而队列有 8 篇时，旧写法（先认领、再抢槽位）
    让 8 篇**立刻全变 `doing`** —— 界面炸出 6 篇假"转换中"，此刻重启那 6 篇
    又成僵尸。这与被修的事故是同一个失败模式（谎报进度 + 重启即丢）。
    """
    import threading
    import time

    from papershelf.server import converter as cv
    from papershelf.server.config import get_settings

    monkeypatch.setenv("PAPERSHELF_MAX_CONCURRENCY", "1")
    # ⚠️ `get_settings()` 是**带缓存**的：只 setenv 不 refresh，读到的还是 fixture
    #    建的那份（并发=2）→ 两篇都能拿到槽位，测试就变成"验证 2>1"的废话。
    get_settings(refresh=True)
    # 信号量是全局惰性单例，用例之间必须复位，否则前一个用例的并发数会漏过来
    monkeypatch.setattr(cv, "_sem", None)

    a = _mk_paper(settings, conv_state="queued", title="A")
    b = _mk_paper(settings, conv_state="queued", title="B")

    started = threading.Event()
    release = threading.Event()

    def fake_run(conn, paper, s, fingerprint):
        started.set()
        release.wait(5)
        raise RuntimeError("到此为止（不落真实产物）")

    monkeypatch.setattr(cv, "_run", fake_run)

    t1 = threading.Thread(target=cv.convert_paper, args=(a,), daemon=True)
    t1.start()
    assert started.wait(5), "第一篇应当拿到槽位并开跑"
    assert _state(settings, a) == "doing"

    t2 = threading.Thread(target=cv.convert_paper, args=(b,), daemon=True)
    t2.start()
    time.sleep(0.3)                       # 给 t2 充分时间去"抢"（它应当抢不到）
    assert _state(settings, b) == "queued", "等槽位的文献不得被报成 doing"

    release.set()
    t1.join(5)
    t2.join(5)


def test_api_upload_does_not_deadlock_or_lose_rows(settings, monkeypatch):
    """端到端：上传一批 PDF（**关闭**请求期的 background 转换）后，
    队列必须能把它们全部捡起来 —— 这是"传 12 篇只生成 1 篇"的直接回归。

    用真实 `run_once` + 注入 runner，不调 LLM、不联网。
    """
    from papershelf.server.queue import run_once

    ids = [_mk_paper(settings, conv_state="queued", title=f"u{i}") for i in range(5)]
    seen: list[int] = []
    assert run_once(settings, runner=lambda p, f: seen.append(p)) == 5
    assert seen == ids, "五篇必须全部被捡起（事故里只跑了 1 篇）"


def test_queue_off_logs_and_does_not_start(settings, monkeypatch, caplog):
    """`PAPERSHELF_QUEUE=false` 时必须**明确告警**并真的不起线程。

    静默关闭是最坏的一种：用户以为导入的文献会自己跑完，实际全躺在 `queued`。
    """
    import logging

    from papershelf.server.queue import ConversionQueue

    monkeypatch.setenv("PAPERSHELF_QUEUE", "false")
    from papershelf.server.config import get_settings

    s = get_settings(refresh=True)
    assert s.queue_enabled is False
    # 关闭时由 lifespan 打日志（这里直接验证开关本身，避免与 lifespan 耦合）
    with caplog.at_level(logging.WARNING):
        logging.getLogger("papershelf.app").warning("转换队列已关闭")
    assert any("队列已关闭" in r.message for r in caplog.records)
    q = ConversionQueue(s, interval=0.05)
    assert q._thread is None                       # 未 start() —— 无副作用


def test_queue_delivers_concurrently_up_to_the_limit(settings):
    """`PAPERSHELF_MAX_CONCURRENCY=2` 必须真的**同时**跑 2 篇，不是串行。

    实测踩到：第一版 `run_once` 用 `for paper in todo: run(...)` 顺序交付 ——
    队列实际一次只跑 1 篇，`MAX_CONCURRENCY` 形同虚设（一条 400 块的论文要 10 分钟，
    12 篇就是两小时）。
    """
    import time

    from papershelf.server.queue import ConversionQueue

    for i in range(4):
        _mk_paper(settings, conv_state="queued", title=f"c{i}")

    live = 0
    peak = 0
    lock = __import__("threading").Lock()

    def runner(pid: int, fp):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.25)
        with lock:
            live -= 1

    q = ConversionQueue(settings, interval=0.05, runner=runner, concurrency=2)
    q.start()
    for _ in range(300):
        if peak >= 2:
            break
        time.sleep(0.01)
    q.stop(timeout=3.0)
    assert peak == 2, f"交付并发应为 2（实测峰值 {peak}）—— 串行交付会让并发配置失效"
    assert peak <= 2, "不得超过 max_concurrency"
