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

    ⚠️ 只改状态、不动 `conv_attempts`：那份工作在上一进程里**没有跑完**，
    不该消耗重试额度（`convert_paper` 自己会在开始和结束各记一次 attempts）。
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
        try:
            run(int(paper["id"]), _fingerprint_for(paper))
        except Exception:                 # noqa: BLE001
            log.exception("队列执行失败 paper=%s", paper["id"])

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
        for i in range(max(1, size)):
            t = threading.Thread(target=self._worker, name=f"papershelf-conv-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, fn: Any, *args: Any) -> Any:
        """投递一个任务，返回可 `result()` 的句柄。

        ⚠️ `self._q.put` 是**有界阻塞**的（maxsize = 线程数），池满时本调用会阻塞 ——
        这正是 `run_once` 要的背压；否则它会把整队列一次性丢进来。
        """
        fut = _Future()
        self._q.put((fut, fn, args))
        return fut

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
        self._stop.set()
        for _ in self._threads:
            try:
                self._q.put(None)
            except Exception:             # noqa: BLE001
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
