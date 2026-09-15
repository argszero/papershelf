"""日志配置 —— 让「转换到底在干什么」可被运维看见（2026-09-14 宿主指示）。

## 为什么必须在这里集中配置

生产实测（2026-09-14）：一篇 502 块 / 19 万 token / 跑了 7 分钟的转换，
`docker logs` 里**一行应用日志都没有**，宿主只能问「一直显示转换中，正常吗？」。
根因不是"没写日志"（代码里 `log.info` 不少：队列启动、切片进度、元数据、启动恢复…），
而是**从来没人调用 `logging.basicConfig`**：

- `uvicorn.run()` 只配置 `uvicorn.*` 自己的 logger，不碰 root；
- 于是 `papershelf.*` 的日志沿 root 冒泡，而 root **没有任何 handler**
  （实测 `root.handlers == []`，`root.level == WARNING`）；
- Python 的 `logging.lastResort` 是 `StreamHandler(WARNING)` —— 所以
  `log.info(...)` 被**静默丢弃**，只有 `log.warning` 及以上才碰巧看得见
  （生产日志里那条「19 块待校对」正是唯一的 WARNING）。

这是"沉默的失败"的典型：功能正常，只是**不可观测**。

## 三条设计取向

1. **一行配置，作用域收窄**：给 root 挂 handler、统一格式与级别；
   `uvicorn.error`（进程级消息）保留，`uvicorn.access`（每请求一行）降到 DEBUG ——
   它此前把日志刷成几千行，真信号全淹没在里面。想看就 `PAPERSHELF_LOG_LEVEL=DEBUG`。
2. **运行时可调**：`PAPERSHELF_LOG_LEVEL` 改级别，不必改代码重新构建。
3. **每篇文献一份日志**：`<data>/logs/p<id>.log`。容器日志是**混流**的
   （多篇并发 + HTTP + 队列），事后问"第 12 篇为什么失败"要翻半天；
   转换过程按 `paper_id` 再落一份，`tail` 一下就有完整时间线。
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# 与 `converter` / `queue` 等模块的 logger 同前缀；只清这个前缀下的 handler，
# 不动 pytest 的 capture handler、也不动 uvicorn 自己的。
APP_PREFIX = "papershelf"
# `%(name)s` 保留（同名 logger 很多，出问题时必须知道是哪一层在说话）；
# `%(threadName)s` 对转换尤其有用 —— 多篇并发时靠它区分是哪篇的工作线程。
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", *, access_log: bool = True) -> None:
    """配置 root handler（幂等：重复调用只更新级别，不叠加 handler）。

    ⚠️ 用 root 而不是 `papershelf` logger：`uvicorn` 的传播链与
    `logging.lastResort` 都挂在 root 上，只配子 logger 的话
    `uvicorn.error` 这类消息仍然走不到我们的格式里。
    """
    lvl = _level_of(level)
    root = logging.getLogger()
    # 幂等：只有我们自己挂过的才复用（`_papershelf_` 标记便于识别，重复调用不叠加 handler）
    ours = [h for h in root.handlers if getattr(h, "_papershelf_", False)]
    if not ours:
        h = logging.StreamHandler(sys.stdout)      # stdout：`docker logs` 直接收
        h.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        h._papershelf_ = True                      # type: ignore[attr-defined]
        h.setLevel(logging.NOTSET)                 # 交给 root 级别统一过滤
        root.addHandler(h)
    root.setLevel(lvl)

    # uvicorn 自带 logger：`error` 留（启动/崩溃信息）；`access`（每请求一行）
    # 只在 DEBUG 级别按行输出 —— 此前它把日志刷成几千行，真信号全被淹没。
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if (access_log and lvl <= logging.DEBUG) else logging.WARNING)

    # 我们自己的 logger 显式跟随 root 级别（避免被历史 `setLevel` 钉死）
    logging.getLogger(APP_PREFIX).setLevel(lvl)


def _level_of(name: str) -> int:
    lvl = logging.getLevelName((name or "INFO").strip().upper())
    return lvl if isinstance(lvl, int) else logging.INFO


@contextmanager
def paper_log(paper_id: int, logs_dir: Path, *, level: int = logging.INFO,
              enabled: bool = True) -> Iterator[None]:
    """把**当前线程**在处理这篇文献时产生的 `papershelf.*` 日志同时写进 `logs/p<id>.log`。

    ⚠️ 为什么用「线程局部 handler + 全局 logger」而不是给转换函数传 logger：
    管线（parse / equations / translator / metadata）里都是模块级 `logging.getLogger`，
    逐层传 logger 要改十几个签名，且很容易漏。转换**每篇跑在自己的线程里**
    （`convert_paper` 由 `BackgroundTasks` 或队列池调用），所以按线程挂/摘 handler
    正好一一对应，不互相串台。

    并发安全：handler 只装在当前线程执行期间（`with` 进出），同一线程同时只跑一篇；
    多线程各自持有**不同文件**的 handler 实例，不存在交错写同一文件。
    """
    if not enabled:
        yield
        return
    path = logs_dir / f"p{paper_id}.log"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        # 日志落盘失败绝不能影响转换本身（磁盘满/权限错都要照跑）
        yield
        return
    fh.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    fh.setLevel(level)
    # 挂到名前缀 logger 上：只有 `papershelf.*` 进文件，
    # 不会把 uvicorn/httpx 的第三方噪音也抄进来。
    logger = logging.getLogger(APP_PREFIX)
    logger.addHandler(fh)
    try:
        yield
    finally:
        logger.removeHandler(fh)
        fh.close()
