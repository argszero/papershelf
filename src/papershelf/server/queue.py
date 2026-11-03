"""转换队列的**排空器**（决策② 无人值守的必要条件）。

## 为什么必须存在这个模块（2026-09-12 生产事故）

此前 `conv_state='queued'` 只是一个**写进数据库的字符串**：真正让转换跑起来的是
**那次 HTTP 请求上挂的 `BackgroundTasks`**（`routers/papers.py` 三处 `background.add_task`）。
请求一结束，唯一的"队列"——那些还活着的请求线程——就没了。后果实测：

- 一次传 12 篇、`PAPERSHELF_MAX_CONCURRENCY=2` → 只有 2 篇真的在跑（抢到信号量的），
  其余在**各自请求线程里阻塞等信号量**；容器一重启（我改 `.env` 重启了一次），
  这四个线程连同跑了一半的转换**一起被杀** → 1 篇 `done`、2 篇永久的 `doing`、9 篇永久的 `queued`。
- 没有任何代码会在启动时或之后重新看一眼 `queued`（全仓库搜索无 `Thread`/`Timer`/轮询）。
  于是导入 12 篇的效果是「只生成 1 篇」，而且**看起来像是卡住了**——UI 对 `queued`/`doing`
  也不给"继续转换"按钮（`Library.tsx` 只对 `failed`/`none` 给）。

`docs/design.md` §5.6 写的本就是「DB 轮询（`papers.conv_state`），worker 取 `queued` → `doing`」，
本模块就是把那个 worker 补上。

## 语义（三条，缺一条就会退化成事故的另一个版本）

1. **启动即恢复**：把 `doing` 复位为 `queued`。`doing` 只可能来自"上一个进程死在里面"
   （本进程刚起，不可能有活着的转换），所以这不是猜测而是必然 —— 不复位就永远是僵尸。
2. **后台常驻轮询**：定期取 `queued`（FIFO by `created_at`）交给 `convert_paper`，
   受 `convert_paper` 内既有的 `_sem` 限流。**导入 12 篇 = 依次自动跑完**，不再依赖请求线程。
3. **单次认领（claim）**：`queued → doing` 的更新带 `WHERE conv_state='queued'`，
   用 `rowcount` 判断是否真抢到。这样即使 `add_task` 与轮询线程**同时**看到同一篇，
   也只有一个会执行 —— 否则同一篇会被跑两遍（双倍 token，且互相覆盖产物）。
   ⚠️ 认领的**位置**同样关键：必须在拿到并发槽位**之后**。见 `converter.convert_paper`。

## 与 `BackgroundTasks` 的关系：**共存，不是替代**

导入/重试接口**保留** `background.add_task`（响应后立刻开跑，不给轮询周期白等），
轮询器是**兜底 + 恢复**。两条路径靠上面的 claim 去重，不会重复劳动。
"""
from __future__ import annotations

import logging
import queue as _queue
import sqlite3
import threading
from typing import Any

from .config import Settings

log = logging.getLogger("papershelf.queue")

# 轮询间隔（秒）。**不能太小**：每轮都是一次 SQLite 查询；也不能太大——
# 用户导入后若刚好错过一次扫描，等待时间就是它。
POLL_SECONDS = 3.0


def recover_stuck(conn: sqlite3.Connection) -> int:
    """启动恢复：`doing` → `queued`（上一个进程死在中途的文献）。返回复位篇数。

    ⚠️ 这里**不动 `conv_attempts`**：本函数只是把状态放回队列，不制造新的尝试。
    真正的尝试由 `claim_paper` 记（认领即 +1，2026-09-15 修）—— 所以"被重启杀掉的那篇"
    会在**重新被认领时**计一次，而不是在这里计。恢复本身零成本。
    """
    from .db import tx, utcnow

    with tx(conn):
        cur = conn.execute(
            "UPDATE papers SET conv_state='queued', conv_error=NULL, updated_at=? "
            "WHERE conv_state='doing'",
            (utcnow(),),
        )
    return cur.rowcount


def pending(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """待转换队列（FIFO）。`queued` 之外不碰：`none` = 用户没要求转换。"""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM papers WHERE conv_state='queued' ORDER BY created_at, id").fetchall()]


def inflight_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """`conv_state` → 篇数。给"队列到底在不在动"提供一眼可读的快照。

    为什么要它：生产汇报「一直显示转换中」时，日志里连"有几篇在跑"都没有，
    运维只能猜。轮询每轮打一次这个快照，配合 `convert_paper` 的开始/完成行，
    整条时间线自洽可查。
    """
    return {str(r["conv_state"]): int(r["n"]) for r in conn.execute(
        "SELECT conv_state, COUNT(*) AS n FROM papers GROUP BY conv_state")}


def _fingerprint_for(paper: dict[str, Any]):
    """与路由里 `_pdf_fingerprint` 同源：有 PDF 才算指纹（arXiv 路线走 None）。"""
    from pathlib import Path

    from .routers.papers import _pdf_fingerprint

    p = paper.get("pdf_path")
    path = Path(p) if p else None
    return _pdf_fingerprint(path) if path and path.exists() else None


def record_preclaim_failure(settings: Settings, paper_id: int,
                            exc: BaseException) -> bool:
    """认领**之前**就失败的投递也要记账，否则这篇文献永远回到队首。返回是否记了账。

    ## 它补的是哪一段（2026-10-30 实测）

    `run_once` **刻意不认领**（`doing` 只能出现在拿到并发槽位之后，见模块 docstring
    与 `convert_paper` 的顺序铁律），而 `conv_attempts + 1` **只**发生在 `claim_paper`
    里 —— 于是「认领之前」抛出的异常既不计数、也不写 `conv_error`、也不落终态：

    - `_fingerprint_for` 在 `_deliver` 里**先于 `run`** 执行，它要读 PDF 才出指纹，
      「存在但读不出」（`pdf_path` 指向目录 / 权限被改 / 卷掉了 / 磁盘 EIO）就在这里抛；
    - 那一刻 `claim_paper` 还没跑 ⇒ 只 `log.exception` 的话，这一行原样停在
      `queued`、`conv_attempts` 恒 0、`conv_error` 恒 NULL ⇒ **下一轮再投一次，永远**。
      实测 5 轮交付 5 次、`convert_paper` 一次没进；真轮询线程 2 秒投了 78 次
      （生产 `POLL_SECONDS=3.0` ⇒ 20 次/分、1200 次/小时）；
    - `max_conv_attempts` 这道成本护栏因此**结构上够不着**，界面也拿不到任何按钮
      （`queued` 是光秃秃的状态药丸，`conv_error` 又是 NULL ⇒ 连原因都没得看）。
      症状与 2026-09-12 那次事故同族（「9 篇永久 queued、UI 不给按钮」），成因不同。

    ## 为什么「仍是 `queued`」就是「认领没跑到」的证明

    认领之后的每个出口都写状态：`doing`（`claim_paper` 自己）、`done`/`failed`
    （`_set_state`，`convert_paper` 的 `except` 与终态分支都走它）。所以异常落回来时
    这一行**仍是 `queued`** ⇔ `claim_paper` 从未成功。这条等价关系就是下面那条
    `WHERE conv_state = 'queued'`，而把它**写成一条语句**是要害：判据与计数同时成立
    或同时不成立，两条投递路径不可能各记一次。

    `rowcount == 0` ⇒ 这一行已归别人（认领后失败自己记过账）⇒ **一个字段都不碰**。
    少了这个判据，认领之后的异常会被重复计一次，重试预算凭空少一半。

    ## 到顶就用**真实原因**落终态

    不落终态的话，`convert_paper` 下一次仍会被交付、由它的 `>= max` 分支写成
    「超过最大重试次数」—— 那句话对排障等于什么也没说（运维要知道的是**为什么**）。
    所以这里自己把 `Type: message` 写进 `failed`，界面同时得到按钮和可读的原因。

    ⚠️ 记账用的是**本次新开的连接**：`_deliver` 跑在队列线程上，`run_once` 的连接
    在交付之前就已经 `close()` 了。

    ⚠️ 崩溃窗口：计数提交与终态写入是两条事务（`db.tx` 不可重入，`_set_state` 自带
    事务），极端情况下进程在两者之间死掉 ⇒ 行停在 `queued` + 计数已到顶 + 原因在，
    下一轮由 `convert_paper` 的 `>= max` 分支兜成 `failed`（仍是终态，只是原因换成
    通用那句）。**不会**退回无限重投。
    """
    from .converter import _set_state
    from .db import connect, tx, utcnow

    # 与 `converter` 落 `conv_error` 的写法**逐字同源**：`Type: message`。
    # 两种写法会让人以为是两种故障，而界面（⑪）与排障读的都是这一列的原文。
    reason = f"{type(exc).__name__}: {exc}"
    conn = connect(settings)
    try:
        with tx(conn):
            cur = conn.execute(
                """UPDATE papers SET conv_attempts = conv_attempts + 1,
                          conv_error = ?, updated_at = ?
                   WHERE id = ? AND conv_state = 'queued'""",
                (reason, utcnow(), paper_id),
            )
            bumped = cur.rowcount == 1
            row = (conn.execute("SELECT conv_attempts FROM papers WHERE id=?",
                                (paper_id,)).fetchone() if bumped else None)
        if not bumped:
            return False
        attempts = int(row["conv_attempts"]) if row is not None else 0
        if attempts >= settings.max_conv_attempts:
            _set_state(conn, paper_id, "failed", reason)
        return True
    finally:
        conn.close()


def run_once(settings: Settings, *, limit: int | None = None,
             runner: Any = None, pool: "_Pool | None" = None) -> int:
    """扫一轮：把 `queued` 交给转换器。返回本次**交付**的篇数。

    `runner` 可注入替身（测试不调 LLM、不联网）。
    `pool` 非空时**并发**交付（受 `max_concurrency` 限制），否则同步依次交付。

    ⚠️ **这里刻意不认领状态**：认领发生在 `convert_paper` 拿到并发槽位之后
    （见其 docstring 的顺序铁律）。若在这里先认领，8 篇队列会立刻全变成 `doing`，
    实际只有 2 篇在跑 —— 界面上炸出 6 篇假"转换中"，正是事故的失败模式。

    也因此**不需要在这里去重**：两条投递路径（请求 `BackgroundTask` 与轮询线程）
    都走到 `convert_paper` 的槽位内认领，那一步是原子的、只有一个赢家。
    """
    from .converter import convert_paper
    from .db import connect

    run = runner or convert_paper
    done = 0
    conn = connect(settings)
    try:
        todo = pending(conn)
    finally:
        conn.close()
    if limit is not None:
        todo = todo[:limit]

    def _deliver(paper: dict[str, Any]) -> None:
        # 兜底只为「绝不因一篇炸掉整个轮询线程」；正常失败由 convert_paper 自己
        # 落到 `conv_state=failed`（§5.7）。
        #
        # ⚠️ 但这条 `try` 里**先于 `run`** 还有 `_fingerprint_for`，它要读 PDF 才出
        #    指纹 —— 「存在但读不出」的 PDF 就在这里抛，那一刻 `claim_paper` 还没跑。
        #    只 `log.exception` 的话，这一行会原样留在 `queued` 并被**无限重投**
        #    （实测 2 秒 78 次、计数恒 0、无原因可看）。所以兜底出口也要记账，
        #    记完到顶就落 `failed`：原因见 `record_preclaim_failure`。
        try:
            run(int(paper["id"]), _fingerprint_for(paper))
        except Exception as exc:          # noqa: BLE001
            log.exception("队列执行失败 paper=%s", paper["id"])
            try:
                record_preclaim_failure(settings, int(paper["id"]), exc)
            except Exception:             # noqa: BLE001
                # 记账自己失败**绝不能**带走轮询线程 —— 那是这条兜底原本唯一的职责。
                log.exception("认领前失败记账失败 paper=%s", paper["id"])

    if pool is None:
        for paper in todo:
            _deliver(paper)
            done += 1
        return done

    # ── 并发交付 ──
    # ⚠️ 必须**逐篇提交**并让池自己排队，不能"一次性全 submit"：那样等于把
    #    `max_concurrency` 又架空成"同时跑 N 篇"（run_once 会瞬间返回，
    #    下一轮又把同一批再投一遍 —— 靠 convert_paper 的认领去重才不至于重复劳动，
    #    但会白白空转）。这里让 `_Pool.submit` 在池满时阻塞，天然形成背压。
    futures = []
    for paper in todo:
        futures.append(pool.submit(_deliver, paper))
        done += 1
    for f in futures:
        try:
            f.result()
        except Exception:                 # noqa: BLE001
            log.exception("队列并发任务异常")
    return done


class _PoolClosed(RuntimeError):
    """池已关闭（或正在关闭）时投递 / 未执行的任务得到的异常。

    为什么用异常而不是静默丢弃：`run_once` 里 `future.result()` 会把它抛出来，被那条
    **只记日志**的 `except` 接住（`队列并发任务异常`），轮询线程照常跑完本轮、再看
    `_stop` 退出 —— 既不静默少跑一篇，也不会把线程吊死在 `result()` 上。
    """


class _Pool:
    """`max_concurrency` 个常驻工作线程的极简池（**不用 `run_once` 里临时建线程**）。

    为什么自己写：`ThreadPoolExecutor` 每次 `run_once` 新建一个池 = 每轮新建/销毁
    线程，且"池满"时排队的任务在新旧池之间有窗口。常驻工作线程 + 有界队列更简单可靠。

    ⚠️ 真正的并发上限仍由 `convert_paper` 内的 `concurrency_semaphore()` 决定 ——
    请求路径（导入/重试的 `BackgroundTasks`）也共用那一个信号量。本池只是**投递侧**
    的节流，保证队列不会一次性把几百篇全丢给信号量去排队（那样 `queued` 会瞬间全变
    `doing`…… 不会，因为认领在槽位之后。这里纯粹是为了不让线程数失控）。
    """

    def __init__(self, size: int) -> None:
        import queue as _q

        self._q: Any = _q.Queue(maxsize=max(1, size))
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        # 投递与关闭互斥的那把锁（见 `submit` / `shutdown` 的注释）。
        # ⚠️ 它**只**圈住"入队"这一瞬间：持锁期间绝不阻塞等待，否则关闭会被投递挡住。
        self._lock = threading.Lock()
        for i in range(max(1, size)):
            t = threading.Thread(target=self._worker, name=f"papershelf-conv-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _closed_future(self) -> Any:
        fut = _Future()
        fut.set_exception(_PoolClosed("转换队列已关闭，任务未执行"))
        return fut

    def submit(self, fn: Any, *args: Any) -> Any:
        """投递一个任务，返回可 `result()` 的句柄。

        ⚠️ `self._q` 是**有界**的（maxsize = 线程数），池满时本调用会**等**一个位置 ——
        这正是 `run_once` 要的背压；否则它会把整队列一次性丢进来。

        ⚠️ 但**池一关闭就再没人 `get`**（2026-11-02 实测）：那时再等位置就是永远。
        只用"进去之前查一次 `_stop`"不够 —— 守卫在**等待之前**求值，而 `put` 没有超时：
        若本调用恰好在 `_stop` 置位**之前**进入、且队列**正满**，就没有任何东西会唤醒它
        （工作线程正是被 `_stop` 停掉的那批人），于是轮询线程永久卡在 `put` 上
        （实测栈 `_loop` → `run_once` → `submit` → `queue.put`，`stop()` 返回 10 秒后
        `papershelf-queue` 仍存活）。那一轮剩下的交付出不去，轮到的 `future` 也永远不结算。

        所以入队这一步与 `shutdown` 的"置位 + 排空"**共用一把锁**，并且**不等**：

        1. 持锁查 `_stop` —— 已关闭 ⇒ 当场回**失败的**句柄（任务不入队，调用不阻塞）；
        2. 持锁 `put_nowait` —— 要么此刻进队列（⇒ `shutdown` 的排空**一定**会结算它，
           因为排空也在这把锁里），要么 `Full`；
        3. 队列满 ⇒ **放开锁**等一小会儿（`_stop.wait` 既当重试间隔又当唤醒源），再回 (1)。
        关闭一旦发生，这一步立刻就醒 —— 没有任何路径能让本调用等下去。
        """
        fut = _Future()
        item = (fut, fn, args)
        while True:
            with self._lock:
                if self._stop.is_set():
                    return self._closed_future()
                try:
                    self._q.put_nowait(item)
                    return fut
                except _queue.Full:
                    pass
            # 池满：让出锁再等，关闭能立刻把它唤醒。
            # 走到这里 ⇒ 上面那次 `put_nowait` 没成功 ⇒ 这件任务**从未入队**，
            # 也就没有被 `shutdown` 的排空结算过 —— 关掉时它同样要自己结算。
            if self._stop.wait(0.05):
                return self._closed_future()

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except Exception:             # noqa: BLE001  (queue.Empty)
                continue
            if item is None:
                return
            fut, fn, args = item
            try:
                fut.set_result(fn(*args))
            except Exception as exc:      # noqa: BLE001
                fut.set_exception(exc)

    def shutdown(self) -> None:
        """停池：**不阻塞，也不留下永远不结算的任务**（2026-11-02 实测的死锁）。

        原先只有两行 —— `self._stop.set()` 然后逐个 `self._q.put(None)`。可 `_q` 是
        **有界**的（maxsize = 线程数），而 `_stop` 一置，工作线程就从 `while` 里退了，
        再没人 `get`：队列此刻若正满，`put(None)` 就**永久阻塞**。
        实测（4 篇排队 + 1 个槽位）：`stop()` 5 秒未返回，faulthandler 的栈停在
        `_Pool.shutdown` → `queue.put`。生产上这条链是 `app._lifespan` → `q.stop()`，
        即**有积压时容器关闭会挂住**，只剩 `docker stop` 的 SIGKILL 收场。

        两件事按顺序补上：

        1. 把还没被取走的任务**就地了结** —— `run_once` 里 `f.result()` **没有超时**，
           不结算的话关闭时轮询线程会在那上面永久挂住（栈上看到的另一半）；
        2. 哨兵改用 `put_nowait`：**绝不阻塞**。工作线程本来就靠 `_stop` +
           `get(timeout=0.2)` 自己退出，哨兵只是让它早点醒，排不进去也不影响正确性。

        3. **置位与排空在同一把锁里**（`submit` 的入队也用这把）：否则"投递方刚查完
           `_stop`、正要入队"与"这里排空完"之间有个窗口 —— 那件任务会落在**已排空的**
           `_q` 里，再没人取，它的句柄永远不结算，`run_once` 的 `f.result()` 就此挂住
           （2026-11-02 实测的那半条漏线程正是它）。锁把两者变成"要么投递先入队、
           排空必然看见它；要么关闭先发生、投递当场拿到失败句柄"。
        """
        with self._lock:
            self._stop.set()
            while True:
                try:
                    item = self._q.get_nowait()
                except _queue.Empty:
                    break
                fut = item[0] if item else None
                if fut is not None:
                    fut.set_exception(_PoolClosed("转换队列已关闭，任务未执行"))
        for _ in self._threads:
            try:
                self._q.put_nowait(None)
            except _queue.Full:
                pass


class _Future:
    def __init__(self) -> None:
        self._ev = threading.Event()
        self._v: Any = None
        self._e: BaseException | None = None

    def set_result(self, v: Any) -> None:
        self._v = v
        self._ev.set()

    def set_exception(self, e: BaseException) -> None:
        self._e = e
        self._ev.set()

    def result(self, timeout: float | None = None) -> Any:
        self._ev.wait(timeout)
        if self._e is not None:
            raise self._e
        return self._v


class ConversionQueue:
    """常驻轮询线程。`start()` / `stop()` 幂等；测试里可只调 `run_once` 不开线程。"""

    def __init__(self, settings: Settings, *, interval: float = POLL_SECONDS,
                 runner: Any = None, concurrency: int | None = None) -> None:
        self.settings = settings
        self.interval = interval
        self.runner = runner
        # 交付侧的并发度：与 `PAPERSHELF_MAX_CONCURRENCY` 同源（那才是真正的上限，
        # 由 `convert_paper` 内的信号量执行；这里只是不要让投递线程数失控）。
        self.concurrency = max(1, int(concurrency or settings.max_concurrency))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: _Pool | None = None

    # ── 生命周期 ──
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        # ⚠️ 恢复必须**在起线程之前**做完：否则线程可能先扫到一堆 doing，白等一轮。
        conn = None
        try:
            from .db import connect

            conn = connect(self.settings)
            n = recover_stuck(conn)
            if n:
                log.warning("启动恢复：%d 篇卡在 doing（上个进程被杀）→ 重新排队", n)
        except Exception:                 # noqa: BLE001
            log.exception("启动恢复失败（不阻止起服务）")
        finally:
            if conn is not None:
                conn.close()

        self._stop.clear()
        self._pool = _Pool(self.concurrency)
        self._thread = threading.Thread(target=self._loop, name="papershelf-queue", daemon=True)
        self._thread.start()
        log.info("转换队列已启动（轮询 %.1fs，交付并发 %d）", self.interval, self.concurrency)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        # ⚠️ 顺序：先停轮询线程（它可能正在 submit），再收池 —— 反过来的话
        #    轮询线程会往已关闭的池里投递。
        if self._pool is not None:
            self._pool.shutdown()
            self._pool = None

    # ── 循环 ──
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                # 每轮先把全景打出来（有无排队、几篇在跑），再交付 —— 顺序很重要：
                # 交付期间状态会变，先打快照才能与随后的「开始转换 paper=N」对上。
                self._log_round()
                run_once(self.settings, runner=self.runner, pool=self._pool)
            except Exception:             # noqa: BLE001
                log.exception("队列轮询异常（继续下一轮）")
            # ⚠️ 用 Event.wait 而不是 time.sleep：stop() 时能立刻退出，不必等满一个周期。
            self._stop.wait(self.interval)

    def _log_round(self) -> None:
        """有活干时才打（空转每 3 秒一行会把日志刷成噪声 —— 那正是可观测性反噬的老毛病）。"""
        from .db import connect

        try:
            conn = connect(self.settings)
        except Exception:                 # noqa: BLE001
            return
        try:
            counts = inflight_counts(conn)
        finally:
            conn.close()
        if counts.get("queued", 0) + counts.get("doing", 0):
            log.info("队列状态：%s", "，".join(f"{k}={v}" for k, v in sorted(counts.items())))
