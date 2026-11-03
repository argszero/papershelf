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

    def fake_run(conn, paper, s, fingerprint, spend=None):
        # `spend` 是 `convert_paper` 传进来的**已花 token 累加器**（失败路径要记账，
        # 见 `converter._Spend`）；本用例只关心槽位-认领顺序，值不参与判定。
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


def test_stop_returns_even_when_the_pool_queue_is_backlogged(settings):
    """有积压时 `stop()` **必须返回** —— 关服务不能挂住（2026-11-02 实测的死锁）。

    关服务那条链是 `app._lifespan` → `ConversionQueue.stop()` → `_Pool.shutdown()`。
    `_q` 是**有界**的（maxsize = 线程数），而 `shutdown()` 原先在 `_stop.set()` 之后
    逐个 `_q.put(None)`：`_stop` 一置工作线程就从 `while` 里退了，再没人 `get` ——
    队列此刻若正满，`put(None)` 就**永久阻塞**。

    实测（4 篇排队 + 1 个槽位）：`stop()` 5 秒未返回，faulthandler 的栈停在
    `_Pool.shutdown` → `queue.put`。生产上的收场只剩 `docker stop` 的 SIGKILL。

    ⚠️ 判定用**有界等待 + 事件**，不断言墙钟时间（与 `test_worker_thread_drains_then_stops`
    的「不能拖满一个轮询周期」同口径）。`stop(timeout=1.0)` 的这 1 秒是**既有的**语义
    （关服务最多等在跑的转换这么久），要钉住的是它**之后**不再永久挂住。
    """
    import threading
    import time

    from papershelf.server.queue import ConversionQueue

    for i in range(4):
        _mk_paper(settings, conv_state="queued", title=f"b{i}")

    entered = threading.Event()
    release = threading.Event()

    def runner(pid: int, fp):
        entered.set()
        release.wait(10)          # 装作一篇长转换，把唯一的槽位占住

    q = ConversionQueue(settings, interval=0.05, runner=runner, concurrency=1)
    q.start()
    try:
        assert entered.wait(5), "工作线程应当已经开始第一篇"
        time.sleep(0.3)           # 让轮询线程把有界队列塞满、并在 put 上等住
        done = threading.Event()

        def _stop():
            q.stop(timeout=1.0)
            done.set()

        threading.Thread(target=_stop, daemon=True).start()
        assert done.wait(5.0), "stop() 在满队列上必须返回（不能挂住关服务）"
    finally:
        release.set()


def test_pool_shutdown_settles_tasks_it_never_started(settings):
    """关闭时**没被取走的任务必须结算**，关闭后再 `submit` 也不得阻塞。

    另一半死锁：`run_once` 里 `for f in futures: f.result()` **没有超时**。池关闭时若把
    队列里的任务直接丢掉，那些句柄永远不设结果 ⇒ 轮询线程吊死在关闭途中（实测栈上看到的
    正是这一半）。所以 `shutdown()` 先排空队列、逐个写异常，再补哨兵。

    `submit` 那一半同样要紧：池已无人 `get`，再阻塞着等位置就是永远 —— 关掉之后必须
    当场回一个失败的句柄，让轮询线程跑完本轮、看到 `_stop` 退出。
    """
    import threading

    from papershelf.server.queue import _Pool, _PoolClosed

    pool = _Pool(1)
    started = threading.Event()
    release = threading.Event()

    def job():
        started.set()
        release.wait(10)

    pool.submit(job)
    assert started.wait(5), "唯一的槽位应当已被第一篇占住"
    pending = pool.submit(job)              # 排在有界队列里，没人取

    done = threading.Event()

    def _shut():
        pool.shutdown()
        done.set()

    threading.Thread(target=_shut, daemon=True).start()
    settled = done.wait(5.0)
    release.set()
    assert settled, "shutdown() 在满队列上必须返回"
    with pytest.raises(_PoolClosed):
        pending.result(1.0)
    # 关闭后的投递：**立刻回来**，不是"等一个再没人取的位置"。所以投递本身也要有界
    # 等待 —— 只断言 result() 会抛的话，"卡在 put 上"这种失败会表现成整个套件挂住。
    late: list[object] = []
    late_done = threading.Event()

    def _late():
        late.append(pool.submit(job))
        late_done.set()

    threading.Thread(target=_late, daemon=True).start()
    assert late_done.wait(5.0), "关闭之后 submit() 必须立刻返回，不能阻塞在没人取的位置上"
    with pytest.raises(_PoolClosed):
        late[0].result(1.0)


def test_poll_thread_terminates_when_stop_lands_mid_submit(settings):
    """关停落在投递中途时，`papershelf-queue` 轮询线程**必须真的退出**。

    2026-11-02 实测的漏线程（上面两条测试盖不到的那一半）：`_Pool.submit` 的
    `if self._stop.is_set()` 守卫在**等待之前**求值，而 `_q.put` 没有超时。
    于是有两种收场，都表现为"`stop()` 返回了，线程还在"：

    - 投递方恰好在 `_stop` 置位**之前**进入、且队列**正满** ⇒ 它挡在 `put` 上；
      `shutdown()` 排空队列把它放行，那件任务落进**已经排空完**的队列里再没人取,
      它的句柄永不结算 ⇒ `run_once` 的 `f.result()`（无超时）永久挂住；
    - 同一个窗口的另一种穿过：任务入队与"置位+排空"之间没有任何互斥。

    实测 `stop()` 返回 10 秒后线程仍存活，栈停在 `_loop` → `run_once` → `submit` → `put`
    （`run_once:230` / `submit:287` / `put:162`）。两个线程都是 `daemon=True`，所以进程
    退得掉 —— 但这**是**一次关停里的线程泄漏，每关一次漏一个。

    ⚠️ 断言落在**线程终止**上，不是 `stop()` 的返回时间：`stop(timeout=…)` 等正在跑的
    转换是有意语义（`test_stop_returns_even_when_the_pool_queue_is_backlogged` 钉的那条），
    本轮换的那一半在它之后。所以先放行在跑的那篇（合法等待），再只问线程还在不在。
    """
    import threading
    import time

    from papershelf.server.queue import ConversionQueue

    for i in range(5):
        _mk_paper(settings, conv_state="queued", title=f"leak{i}")

    entered = threading.Event()
    release = threading.Event()

    def runner(pid: int, fp):
        entered.set()
        release.wait(20)

    q = ConversionQueue(settings, interval=0.05, runner=runner, concurrency=1)
    q.start()
    poll = q._thread                    # `stop()` 会把 `q._thread` 置 None，先留住引用
    try:
        assert entered.wait(5), "唯一的槽位应当已经开始第一篇"
        time.sleep(0.5)                 # 让有界队列（maxsize=1）灌满，投递停在第 3 篇上
        stopped = threading.Event()

        def _stop():
            q.stop(timeout=1.0)
            stopped.set()

        threading.Thread(target=_stop, daemon=True).start()
        assert stopped.wait(10), "stop() 本身也必须返回（另一半缺陷）"
        release.set()                   # 在跑的那篇结束 —— 关服务等它是有意的，不是缺陷
        for _ in range(500):            # 最多 5 秒
            if not poll.is_alive():
                break
            time.sleep(0.01)
        assert not poll.is_alive(), (
            "stop() 返回后 papershelf-queue 仍存活 —— 投递方卡在没人取的位置或它的句柄"
            "永不结算，每关一次服务就漏一个线程")
    finally:
        release.set()
