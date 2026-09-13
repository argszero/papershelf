---
id: queue-only-request-scoped-defect
event_at: 2026-09-12T12:00:00
created_at: 2026-09-12T13:00:00
updated_at: 2026-09-12T13:00:00
type: reference
scope: project
status: active
---

# 「生产只转了一篇」—— 队列只活在请求里 + 无启动恢复

**事故 2026-09-11 晚 → 诊断 2026-09-12 上午 → 修复提交 `d6d688a`（代码）/`fed02ab`（文档）/`sha-fed02ab`（生产）**

## 现象

生产文献库 12 篇，**只有 1 篇「已生成」**，其余像卡住了。UI 也不给
`queued`/`doing` 的重试按钮（`Library.tsx` 只对 `failed`/`none` 给），用户无从下手。

## 病因（三件事叠加，缺一不可）

1. **「队列」其实是那次 HTTP 请求**：上传播时 `background.add_task(convert_paper, ...)`
   —— `queued` 只是 DB 里一个字符串，真正跑任务的是请求线程上的 `BackgroundTasks`。
   **请求一结束就没人看 `queued` 了**。
2. **并发上限 2 + 其余在请求线程里干等**：抢不到信号量的论文不是"排队"，是
   **各自占着一个请求线程阻塞在信号量上**，请求不结束、连接不释放。
3. **无启动恢复 + 无 DB 轮询 worker**：容器重启（`Container papershelf Recreated`）
   杀掉 2 篇跑一半的 → **永久 `doing` 僵尸**；9 篇 `queued` 成**孤儿**。
   `docs/design.md` §5.6 写的「DB 轮询 worker」**从未实现**（全仓库搜索无 `Thread`/`Timer`）。

> 一句话：**状态机写进了 DB，跑状态机的引擎只活在请求生命周期里。**

## 修复（宿主选「治本」）

新增 `server/queue.py`：
- `ConversionQueue` 常驻轮询线程（`lifespan` 启停）；
- `recover_stuck`：启动时 `doing → queued`（单进程下"进程刚起 ⇒ 没人真在 doing"是必然，
  不是猜测）—— **只复位状态，不动 `conv_attempts`**（那次尝试被打断，不该吃掉重试额度）；
- `_Pool`：`max_concurrency` 个**常驻**工作线程 + 有界队列（`submit` 池满即阻塞 = 背压）
  → **并发交付**（早先"轮询里逐个跑"实测是串行的，发现后改）；
- `converter.claim_paper`：`UPDATE … WHERE conv_state='queued'` 用 `rowcount` 判胜负，
  **全局唯一互斥点**（请求路径与轮询路径靠它去重，两条路可共存）。

## ⚠️ 顺序铁律（顺序写反 = 事故的另一个版本）

**先抢并发槽位，再认领状态。**

最初写成「先 `claim_paper()` 再 `with concurrency_semaphore()`」，本地一跑就现形：
并发=2、队列=8 时**8 篇瞬间全变 `doing`** → 界面炸出 6 篇假「转换中」，
且此刻重启那 6 篇又成为僵尸 —— **正是本事故的同一失败模式，只是快了一拍**。

推论：等槽位期间 `conv_state` 必须**老实地停在 `queued`**（说"还没轮到我"）。
护栏测试 `test_queue_claim_happens_inside_the_concurrency_slot` 钉住。

## 顺带修掉的隐藏缺陷

- `_sem = threading.Semaphore(...)` 在**模块导入时**求值 → 改环境变量/测试夹具
  **永远不生效**（两条不同的路径）。已改惰性函数 `concurrency_semaphore()`。
- 前端无进度轮询 → 改成 **`busyCount > 0` 才每 4s 拉一次**，安静后自动停
  （护栏 `test_conversion_polling_is_gated_on_inflight_work`，防无脑轮询）。
- 测试夹具默认关掉常驻 worker（`conftest.py`），否则现有测试被后台线程二次处理、结果不确定。

## 生产验证（部署 `sha-fed02ab`）

部署前**先备份库**（`papershelf.db.bak.20260912-1200`）+ 把 11 篇的 `conv_attempts`
归零（打断 ≠ 失败）。结果：
- 日志 `启动恢复：2 篇卡在 doing（上个进程被杀）→ 重新排队` → 队列接上；
- **12 篇全部 `done`**、12 份 docs、5094 块、累计 1,855,352 tokens（单篇 5.8 万–24.9 万，
  均未触 40 万预算）；
- 浏览器看生产页**无人操作自动变化**（排队 9→0、已生成 1→12，始终恰好 2 篇「转换中」），
  全部完成后 **30s 内 0 次轮询**；
- 阅读器抽检 1/2/3/7/12 全部正常。⚠️ **第 12 篇（544 块 + 26 图）首屏要十几秒**，
  一度被我误判成"卡在加载中" —— **大文档要等，别急着当 bug**（判据：`/api/papers/N/doc`
  返回 200 且日志无异常）。

## 通用教训

1. **状态机落库 ≠ 有引擎在推它**。任何 `state = "queued"` 都必须能回答
   "**谁**在什么条件下把它变成 doing"；答案若是"某个请求的 BackgroundTasks"，
   那就是**没有队列**。
2. **重启恢复是自托管多用户的基本卫生**：自托管机器会重启、会被改 `.env`、
   会被 `docker compose up -d` 换镜像。启动时把"上个进程的半成品"复位，代价一行 SQL。
3. **先抢稀缺资源，再改可观测状态**。搞反了界面会谎报进度，而骗子状态在重启时
   全部变成僵尸 —— 两个缺陷其实是同一个。
4. **并发语义要在真队列上测**（`MAX_CONCURRENCY=2` + 8 篇），别只在 1 篇上测。
