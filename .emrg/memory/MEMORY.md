# Project Memory Index — papershelf

`papershelf`：开源项目。把「PDF → 保版式中文精读」的文献工作台产品化。
原型：`/Users/argszero/Downloads/t1/literature-workbench.html`（文献台 PaperDesk）。
姊妹项目参考：`pku-paper-3d`（PDF→中文 HTML 管线在此跑通）。

## Memories

| ID | Type | File | Summary |
| --- | --- | --- | --- |
| `3c1d7f02` | user | [collab-one-question-at-a-time.md](collab-one-question-at-a-time.md) | 设计阶段**一次只问一个问题**，宿主每次只答一个；有疑问不要跳过、不要自行假设 |
| `85b5851a` | project | [proto-literature-workbench.md](proto-literature-workbench.md) | 原型拆解：6 模块 + 数据模型（Plan/Paper/Doc）+ 关键实现约束 |
| `b7e2a1c4` | reference | [existing-pdf-to-zh-pipeline.md](existing-pdf-to-zh-pipeline.md) | 宿主机已跑通的 PDF→中文 HTML 管线与踩坑教训；papershelf 是其产品化 |
| `implemented-status` | project | → 见 `design-decisions.md` 末尾「实现落地状态」+「M3 前端 SPA 落地」两段 | **M1管线 ✅ / M2服务端 ✅ / M3前端SPA ✅ / M4 Docker ✅ / M5复刻原型 ✅**；小决策：**公式=服务端 LaTeX→MathML（零 CDN）**、`render_block(typeset)` 默认 False（渲染与校验分流）、管理员引导必须显式密码、导出默认内联图片；**M3 两条前端硬约束**：块 HTML 由服务端给（前端不排版公式/不拼资产 URL）、改块必须回完整块对象（否则"保存成功但界面不变"）|
| `c4d81f30` | project | **[pipeline-v1-real-run.md](pipeline-v1-real-run.md)** | **转换管线 v1 首轮真实跑通**：实测数据（598 块 / 216 需翻 / 43.7k tokens / 9 分钟 / 校验一次通过）+ **三条硬教训**（校验必须收敛、免中文块必须豁免、图注与标题必须译）+ **决策㉓ 公式 LaTeX 化已落地**（display 57 / inline 366，超过参照产物 39/343）+ 渲染修复（数学行合并 / 中文回落 / 单栏横跨） |
| `decisions-master` | decision | **[design-decisions.md](design-decisions.md)** | **设计决策总表 ①–㊹ —— 唯一事实来源；本条只做索引，细节/实测数据/踩坑全在文件里**：归属链 User→Plan→Paper（无 Team）／服务端统一 Key／Python 单体 + SPA（服务端同源托管）／只读分享（持链接 + 可撤销 + ≤24h）／开放注册限 `edu.cn,ac.cn`（⑮ 改邮箱验证码）／导入 = PDF 上传 + arXiv／元数据 = LLM 抽取 + 占位标题落列／**块级 JSON 入库、HTML 为导出格式**／最小管理页（配置走环境变量）／责任归用户（注册协议强校验 + 留档）。**决策号速查**：㉓ 公式全量 LaTeX 化（服务端 MathML，零 CDN）· ㉖ 分享管理 · ㉗ 常驻转换队列+启动恢复+原子认领 · ㉘ 元数据抽取 · ㉙ 删除文献（含磁盘产物）· ㉛ 划痕 = 任意字符区间 + 四支笔（服务端锚点尺子，`anchors` 默认 False）· ㉜ 阅读顺序分栏感知 · ㉝ ①c 原文校对 agent（全量逐页，`minimal` 思考，≈27k tokens/页）· ㉞ 重新提取（解析缓存单篇失效）· ㉟ 笔记按原文位置排序 · ㊱ 待读→在读 + `last_read_at` · ㊲ 栏间续段只标记（agent `merge_block`）· ㊳ 标题/参考文献都可划 · ㊴ 表格重建 = `set_table`（护栏三连：逐字来源/形状/不吃正文）· ㊵ 表格左右并排 + 每格可划 （已上生产 `51f671b`）· **㊶ 页面家具（v8 关键词行内拆分保序 / v9 页边带白字不入产物 / v10 页眉文字不再有意丢掉 + 补回页眉横线，`PARSE_VERSION`→10，`band`/`rule` 两戳只影响渲染；宿主选 A「照译」⇒ 6/8 页页眉会挂「待校对」）** · **㊷ 页面图形（v11，`PARSE_VERSION`→11：横线**照抄 PDF 颜色的粗细** / 矢量标识（Springer 马标、Check-for-updates 徽标）整体栅格化成新块类型 `deco` / **不再删"无图注的图"**（37 页篇曾丢 4 张真图）+ 图注允许小幅重叠且全局最近优先认领 / 色块底纹照抄（主保险=色块里包可见文字，防白字黑框变黑方块）；⚠️ 徽标是「图片层+矢量层」叠的，只取内嵌图 = 一个灰方块）** · **㊸ 整页旋转的表格页转正（解析 v12，`PARSE_VERSION`→12：判据 = span 多数 `dir`（**不信 `/Rotate`**）+ `show_pdf_page(rotate=…)` 原样重画 ⇒纯坐标变换、不给表格识别加特例 / 旋转页 `columns=False` 只按行序 / 表注判据收紧（`pending_figure`跨页存活没复位 → 静默丢 `Table 2 …`）/ 转正副本落 `p<id>/normalized.pdf`（**不是**全库共享名，否则并发互相覆盖）/ `_h_rules` 先缝**共线短线段**再按长度筛）+ ①c 瞬时故障重试 **2→20 次**且**退避封顶 30s**（不封顶则第 20 次睡 `2^20` 秒 ≈ 12 天）；本地已验、**未上生产**）** · **㊹ 参考文献条目 = 新块类型 `ref`（解析 v13，`PARSE_VERSION`→13）：只译标题**（宿主：作者名/期刊/DOI 保原文，读者要靠它们检索）—— 原先 `refs` 整块免中文 ⇒ **从设计上就不译**；真前提是**先有一条完整的条目**：PDF 抽出的是**连续文字流**，被块检测切成 ~200 字符碎片、切口落在词中间（`man-`+`ufacturing`）、一块里还塞着下一条的前半截 ⇒ `merge_ref_entries` 按**条目编号**切整条（**"连号"才是真判据**，光看形状会把 DOI 的 `10.` 全抓进来）；切不出就**原样返回**（`refs` 从此只=碎片）；拼接**只动空白**（逐字守恒）+ 断词连字符**不猜补**（`Laser-directed` 是反例）；翻译走**第三条通道**（送一条/要一条 JSON，四道程序判据，定位用 `fold_for_match` **折叠后**比对）、`ref` 移入新的 **`PARTIAL_ZH_TYPES`**（要有中文但**不比对数字**，靠 `data-pt="1"` 随 HTML 走 —— 按元素标签判永远不生效，实测每条刷一条 digit_mismatch）＋ 13 项遗留待定 + 5 条贯穿性约束 |
| `repo-history-reset` | decision | **[repo-history-reset.md](repo-history-reset.md)** | **仓库历史重置：删库重建 + 单次初始化提交**（2026-09-13 宿主指示）。现远端 = **唯一初始化提交**（155 文件；旧 SHA `6f487c4`/`350f33f`/`52b142e`… 远端已不存在，只留本地 bundle 备份）。**三条实测结论**：①**删仓库不删 GHCR 包**（包是账号级的，删库后匿名拉 manifest 仍 200、生产容器照跑）—— 但**孤儿包的 linked repository 指向已删仓库 → 新仓库 `GITHUB_TOKEN` push 被拒 `denied: permission_denied: write_package`**（`test` 绿、两个 `build` 红）→ 修法 = **先删整个包**（`DELETE /user/packages/container/papershelf`，需 `delete:packages`）再重跑，同名包自动重建并 link 回新仓库；②**别用 `gh auth refresh` 等宿主**（内部轮询 deadline 太短 → `context deadline exceeded`，验证码其实还有效，白等两次）→ 自己跑 device flow 把 ~15 分钟窗口用满；③**本机 `github.com` 直连被墙**（`login/device/*` timeout）而 `api.github.com` 可用，git 全局代理 `http://172.16.0.40:6501`，gh/curl 打网页端点要显式 `HTTPS_PROXY`。验收：CI 六 job 绿 + `latest`/`sha-<该提交>` 双双 200 + 包 `repository: argszero/papershelf` + 生产 `revision`=该提交/healthy/health 200/bundle 指纹未变；**记忆里别钉这个 SHA**（写记忆即改 SHA） |
| `localdev-node-asdf` | reference | [localdev-node-asdf.md](localdev-node-asdf.md) | **本机开发环境的两处坑**：①**node 在 asdf 下、不在 PATH**（`~/.asdf/installs/nodejs/26.5.0/bin`；`brew` 里没有、`python -m tsc` 报的 `No module named tsc` 是**假线索**）→ 前端必须 `export PATH=…:$PATH && npx tsc -b && npx vite build`；产物落 `src/papershelf/static/`，`emptyOutDir` 会删旧 bundle → **浏览器缓存的旧 index.html 指向已 404 的旧 JS**（表现为「改完刷新还是老界面」）→ 必须 `Page.reload(ignoreCache=True)` 硬刷；②**macOS 无 `setsid`/`timeout`** → 用 `(nohup sh scripts/dev.sh &)` + `curl health`；重启前先确认 PID 是 `papershelf.cli serve --port 8012`，**别误杀 emrg server** |
| `m5-replicate-prototype` | decision | **[m5-replicate-prototype.md](m5-replicate-prototype.md)** | **M5 = 复刻原型（计划内视图）✅ 已落地**。①推翻了旧文档"总览/文献库/看板是跨计划视图、v1 刻意收窄"（实测原型 L1479 `papers()=activePlan().papers`，**没有跨计划聚合** → 实为**漏做**）；②**验收修掉 7 个真缺陷**（Share 页引用已删类名→无样式、移动端侧栏被藏死、搜索框与 URL 不同步、切换器格式、自造图例、大纲错显 H 徽标、新建按钮）；③**教训：复刻任务里"删旧样式"必须与"迁移页面"同步核对**（先删后迁会静默降级）；④**PDF 分页容器 ✅ 已落地**（宿主选 A，2026-09-11）：解析阶段给**每个块**盖 `payload.page`（`_paged_adder`）+ `doc_cache` 指纹混入 `PARSE_VERSION`（不混则永远命中旧解析产物、分页静默不出现）+ 阅读器/分享/导出三处同版式；⑤**顺带修掉既有真缺陷**：懒加载图无 `width/height` → 整篇高度事后上浮 7.3k px → **大纲跳转偏位 7.5k px**（把页 section 拍平后同样复现，证明与分页无关；已用资产宽高占位修掉） |
| `ci-publish-pipeline-defect` | decision | **[ci-publish-pipeline-defect.md](ci-publish-pipeline-defect.md)** | **CI 全绿却静默删掉了刚发布的镜像**（2026-09-11，修于 `c0a7996`）。`cleanup` 那个 `actions/delete-package-versions@v5`（`delete-only-untagged-versions:false` + 保护名单只认 `latest`/数字 + `min-versions-to-keep:0`）把 `latest`/`sha-<commit>` 一起删了 → GHCR 全 404、包页面「No tagged versions found」、部署机 pull 只能 not found。时序铁证：`merge` 推 latest 成功 → `smoke` 拉 latest 起容器成功 → cleanup 打印「deleted till now: 8」。**教训：破坏性清理的失败模式是"静默删交付物"，应对是直接禁止而非调参；校验必须放在所有会改远端状态的步骤之后（"刚推成功"不等于"还在"）**。已换成只读 `verify` + `tests/test_workflow_guard.py` 5 条护栏。|
| `ui-interaction-real-events` | reference | **[ui-interaction-real-events.md](ui-interaction-real-events.md)** | **验收 UI 交互必须用真实输入事件**（2026-09-13，㉛ 修订 `350f33f` 的教训）。拖选收尾浏览器**还会补一个 `click`** → 挂在 `click` 上的"点别处收浮条"把 `mouseup` 刚点亮的浮条当场收掉（「浮条一闪即没」，主路径不可用）；**合成事件（`dispatchEvent`/`el.click()`）跳过真实序列 `mouseup→click`，所以上一轮"真浏览器全绿"照样漏**。实操：CDP `Input.dispatchMouseEvent`（`_response_timeout=30`）、**必须先 `activate_tab`**（后台标签收不到输入事件，日志全空 ↔ 像"事件没挂上"）、"一闪即没"用 `MutationObserver` 拍增删、取点用 `getClientRects()[0]`（跨行时 rect 中点在行间空隙）。同族：假 SMTP／只看截图不量 computed style —— **验收要跑在最接近真实的那一层**。**同类病例二（2026-09-15，`19c6f25`）：「仅中文」正文全空白** —— `styles.css` 藏 `.lang-zh .t-en` × `Reader.tsx` 只按 `dual` 渲染中文栏 = 两栏同时不可见；**标题还在**（走 `.b-zh` 内联分支）是完美伪装；判据 = 逐块量 `display!=='none' && getClientRects().length` + `innerText`；附带修好「仅中文下划不了重点」（`.b-inline[data-lang=zh]` 不存在 ⇒ 划痕没坐标系）；护栏 `test_style_guard.py::test_zh_only_mode_always_renders_the_chinese_column`（红-绿验证过） |
| `mailer-gmail-ip-rotation` | reference | **[mailer-gmail-ip-rotation.md](mailer-gmail-ip-rotation.md)** | **发信可靠性：Gmail 的 DNS 池是有偏的**（2026-09-11 生产实测）。4 个 IP 里 `173.194.43.108` **37.5% 概率被抽中且 100% 连不通** → **单次尝试失败率 37.5%**；`smtplib` 只解析一次，所以**必须重试**（3 次仍剩 5.27%，实测每 8 次失败 1 次 → 改为 **5 次 = 0.74%**，`_TIMEOUT=6s`，最坏 ~34s）。**本地测不出来的原因有两道屏障**：假 SMTP 服务器（不轮转）+ 测试夹具把 `mailer._send` 整个换掉（不走 SMTP 层）→ 补 `tests/test_mailer.py` 专打被绕过的那层。**通用教训：换外部服务方就换了一套网络拓扑；抽一个资源、连一个资源且有不可达分支的必须重试；重试次数要先测失败率再算 p^n** |
| `llm-402-quota-swallowed` | reference | **[llm-402-quota-swallowed.md](llm-402-quota-swallowed.md)** | **「一半没译文」真凶是 LLM 池子欠费（402）**（2026-09-11）。911 块只译出 154 块、391 块「待校对」，不是管线 bug —— `aitokenpool` 侧 `balance <= 0 → 402 点数余额不足`（上游调用前预检），而 `translator.translate_blocks` 把 `_chat` 异常**整段吞掉**（`continue`），配额型错误不会自愈 → 每片都失败 → 全篇空白却显示「转换成功」。**排查判据**：`tokens_used` 与块数量级脱节。查余额：`GET /api/wallet`（key 自带身份）；生产 token 库在容器 `aitokenpool:/data/aitokenpool.db`（容器内无 python3/sqlite3，要 `docker cp` 出来用宿主查）|
| `prod-nginx-two-layers` | reference | **[prod-nginx-two-layers.md](prod-nginx-two-layers.md)** | **上传 413 真凶是内层 nginx**（2026-09-11）。链路有**两个** nginx：外层 `nginxproxy/nginx-proxy` + 内层 `website` 容器（`nginx:alpine`）。**改外层 `client_max_body_size` 无效**，必须改内层 bind-mount 的 `/root/app/ali.args.fun/nginx/default.conf`（已加 `256m`，备份 `.bak.20260911-2003`，持久）。应用本身无限制。**定案方法**：`docker logs website` 报 `client intended to send too large body: 14680272 bytes` → 拦截在内层。**仓库代码零改动，全在生产服务器**。教训：**宿主截图不能自证时刻**，要用 nginx access log 的 UTC 时间戳对齐（那次 413 是 11:58:55 UTC；12:03 reload 后同一请求变 401；12:08 真 39MB PDF → 201）|
| `auth-email-code` | decision | → 见 `design-decisions.md` 的 **⑮「修订」** | **注册/找回密码改为邮箱验证码**（2026-09-11 宿主指示 + 对齐原型，原型本来就是验证码）。删 `GET /activate` → `register/code` + `register`（**注册即登录**，`status` 不再有 `pending`）；新增 `/forgot` 两步向导 `reset/code` + `reset`。**防探测**：找回对未注册邮箱也返回成功。参数 6 位 / 10 分钟 / 单次 / 60s 冷却 / 5 次作废 / **只存哈希** |
| `queue-only-request-scoped-defect` | reference | **[queue-only-request-scoped-defect.md](queue-only-request-scoped-defect.md)** | **「生产 12 篇只转 1 篇」真凶：状态机落了库，推它的引擎只活在 HTTP 请求里**（2026-09-12，修于 `d6d688a`／生产 `sha-fed02ab`）。`queued` 只是 DB 字符串，真正跑的是一次请求的 `BackgroundTasks`；并发 2 之外的在**请求线程里阻塞等信号量**；容器重启 → 2 僵尸 `doing` + 9 孤儿 `queued`（§5.6 的「DB 轮询 worker」从未实现）。修法 = 常驻队列 + 启动恢复（`doing→queued`，**不动 attempts**）+ `claim_paper` 原子认领，**⚠️ 铁律：先抢槽位、再认领**（写反则 8 篇瞬间全 `doing`＝事故的另一种版本）。顺带修：模块级 `Semaphore` 导入即求值（改 env 不生效）／前端轮询必须 `busyCount` 门控。生产实测 12 篇全 `done`（1.86M tokens）。**教训：`state='queued'` 必须能回答"谁在什么条件下把它变 doing"** |
| `metadata-gap-19-unimplemented` | reference | **[metadata-gap-19-unimplemented.md](metadata-gap-19-unimplemented.md)** | **「所有文章都没有名字和作者」＝ ⑲ 元数据自动抽取从未实现**（2026-09-12 报告，**同日已修 → 决策㉘**；生产 12 篇待回填）。**五个**叠加缺陷：(1) 上传写的是**文件名**且写回用 `COALESCE(NULLIF(title,''))` → 占位值非空 → 真标题永远写不进；(2) `authors/venue/year` 全仓库**只有消费端没有生产端**（`grep "meta\[" pipeline/` 只有 title_en/dropped_images/refs_*），`parse.py` 也从不解析作者；(3) `title_zh`（中文标题）从无赋值路径；(4) `PATCH /papers/{id}` 后端能改元数据但**前端无入口**（死列）；(5) `_finalize` 取第一个 h1 后**删光其余 h1** → 封面「期刊名在前」时真标题被静默销毁（实测 1/3 篇），模型只能**编**。附带：`relTime` 用 UTC 天数致「今天」显示成「昨天」／`tags` 恒空。**两条教训**：①「有写回代码」≠「有数据来源」，要**反向搜赋值端**；②**修复时踩的第二个坑**：判据「`title == Path(pdf_path).stem`」被落盘时间戳前缀（`2026-09-11T143352+0000_original.pdf`）骗过 → 生产 12 篇全判成"用户手填"，真标题**仍然写不进去**（从"必错"变成"看起来对"）→ **占位符是一种意图不是一种值，必须落列**；且**单测输入要取生产真实形状**，自己编的干净路径恰好绕过唯一的坑** |

（`decision-*.md`、`deploy-selfhosted-multiuser.md`）已 `status: merged` 并入 `design-decisions.md`，保留备查、不再更新。

## 设计与实现进度

> ⚠️ **2026-09-13：仓库历史已重置** —— 远端现存**唯一初始化提交**（宿主指示删库重建，
> 见 `repo-history-reset`）。本文件与各记忆里引用的旧 SHA 均已离线，只留本地 bundle 备份。

①–㉝ **全部 ✅ 需求澄清完成 + 已落地**（详见总表；㉛ 推翻 ㉚ 的②③④；㉝ 推翻 `design-decisions.md:351` 的「不做 VLM 比对」）
**M1 管线 ✅**：真实论文跑通（545 块 / LaTeX 化 184 / 校验一次通过）
**M2 服务端 ✅**：认证·计划·导入（PDF+arXiv）·块级修订·笔记·分享·导出·管理页
**M3 前端 SPA ✅**（`web/` React+TS+Vite，服务端同源托管）
**M4 Docker ✅**：多阶段镜像（非 root uid 10001）+ entrypoint 配置校验 + healthcheck；
CI 原生并行双架构推 GHCR + **真起容器冒烟**（4分51秒）；`docs/install.md`
**发布流水线已上生产 ✅**（最新 2026-09-12 `sha-fed02ab`，含常驻转换队列+启动恢复）；
⚠️ 期间修掉一个**CI 全绿却静默删空 GHCR 镜像**的缺陷（见 `ci-publish-pipeline-defect`）。
**M5 复刻原型 ✅**：侧栏壳层（切换器/进度卡/用户区）+ 总览 + 文献库 + 进度看板（拖拽实测）
+ 阅读器补齐（字号/大纲/三模式）+ **Admin & Share 补迁移**；移动端侧栏横置（≤920px）
**验收 = 真浏览器逐页对照原型截图 + 逐交互实测**（拖拽落库、拖拽拦截、大纲跳转、模式切换、分享 545 块）
**认证改验证码 ✅**（2026-09-11，见 `auth-email-code`）：注册/找回都走邮箱验证码，`/forgot` 两步向导
**㉖ 分享管理 ✅**（2026-09-12，`3f846db`，**已上生产**）：多分享 + 备注 + 跨计划管理页 `/shares`
（4 指标卡 + chips + 倒计时表格）+ 续期（从此刻重新起算）+ 取消立即 410；只读页 banner 带倒计时；
顶栏/计划卡「分享」→ 新建抽屉；`SharePanel.tsx` 退役。真浏览器双端验收（本地 8012 + 生产）
**㉗ 转换队列修复 ✅**（2026-09-12，`fed02ab`，**已上生产**）：病灶 = 任务挂在请求的
`BackgroundTasks` 里 + 无启动恢复 + §5.6 的 DB 轮询 worker 从未实现 → 容器重启后
2 篇僵尸 `doing` + 9 篇孤儿 `queued`，「12 篇只生成 1 篇」。修法 = 常驻队列（`queue.py`）
+ 启动恢复（`doing→queued`）+ 原子认领（`claim_paper`，且**先抢槽位后认领**）；
生产实测 12 篇全部 `done`（1,855,352 tokens），UI 无人操作自动变化、完成后轮询自停
`tests/` **112 项离线回归**（不调 LLM、不联网；含 5 条发布流水线护栏 + **9 条样式护栏** + 5 条发信重试 + 12 条队列）
六路由零控制台报错、零横向溢出
**⚠️ 上轮修掉的三个真缺陷**（都是"本地全绿、生产才现形"）：
① **按钮变体被基类盖成透明**（M5 引入，挂了近一天）：CSS 特异性写反，`.btn, button.btn`(0,1,1) > `.btn-primary`(0,1,0)
② **发信约 37.5% 失败**：Gmail DNS 池有偏 + `smtplib` 只连一个 IP（→ `mailer-gmail-ip-rotation`）
③ **上传 14MB PDF 报 413**：**内层** `website` nginx 的 1MB 默认限制（改外层无效）→ 已放开 256m（→ `prod-nginx-two-layers`）
**㉘ 元数据抽取 + 占位标题落列 ✅**（2026-09-12，`49230d0`，**已上生产**）：`pipeline/metadata.py`
（首屏块 + 候选标题，≈1.6k tokens/篇）+ 元数据编辑抽屉 + `papers.title_is_placeholder` 列；
生产 12 篇**全部回填**（19,181 tokens = 重跑转换的 1.03%）
**㉙ 删除文献 ✅**（2026-09-12，`5da7e18`）：前端入口（hover 显现 + 确认框 + 只读态不渲染）
+ 磁盘产物一起收（不收则重导入命中旧解析缓存）；生产 43 → 42 实测
**㉚ 精读交互 ✅**（2026-09-13，`bce16f1`，**已上生产**）：侧栏删「阅读器」项（停在阅读器时 active 落「文献库」，与原型一致）
+ 句子高亮 + 句锚笔记 + 点笔记跳回原句 —— ⚠️ **同日被 ㉛ 推翻②③④**（①保留）
**㉛ 划痕 = 任意字符区间 + 四支不带含义的笔 ✅**（2026-09-13，`99bb6c2`，**已上生产** `sha-350f33f`）：
`highlights` 表重做（`(id,paper_id,block_id,lang,start,end,color,created_at)`，旧 `sid` 列退役）
+ 旧 sid 在启动时**重跑切句确定性换算**；浮条 + 点划痕就地菜单（换色·擦掉·加笔记）+ 区间笔记
+ 点笔记跳回划痕；**原型同步改完**；离线回归 **194 项**全绿（删 `test_sentences.py`、
新增 `tests/test_markup.py` 18 项）；
**生产 #12 那 4 条 sid 划痕实测迁移成功**（行数不变、区间成 `zh 0–79 / 149–192 / 160–206 / 591–651`、
真实浏览器 4 道划痕渲染且样式为 `oklch(0.8 0.15 85)`）
**㉛ 修订 ✅**（2026-09-13，`350f33f`）：**拖选后浮条一闪即没**（划重点主路径整个不可用）——
拖选收尾浏览器还会补一个 `click`，挂在 `click` 上的"点别处收浮条"把 `mouseup` 刚点亮的浮条当场收掉；
判据改为"**选区非塌缩 ⇒ 这是拖选的尾巴**"（原型把收浮条挂 `mousedown`，天然躲开）。
`→ ui-interaction-real-events`（合成事件测法跳过了 `mouseup → click`，所以上一轮"真浏览器全绿"照样漏）
**㉛ 补全 ✅**（2026-09-13，`6f487c4`，**已上生产** `sha-52b142e`）：**选中加笔记时自动高亮**（宿主：「选中添加笔记时，应该同时自动高亮」）——
`repo.ensure_highlight`（**先查再建**，同区间认旧的，避免别的笔记 `hl_id` 悬空）+ `add_note` 同事务补划痕
（`tx` 不可重入 → 拆出无事务的 `insert_highlight`）；颜色取当前那支笔；**整块/文献级锚不加**；
前端保存后只重拉这一块 + toast「笔记已保存，并已高亮」；**原型 `addNote` 同步改写**；
离线回归 194 项；**生产真浏览器实测**（真实鼠标事件，paper #12）：拖选 → 加笔记 → 划痕 4→5 + toast「…并已高亮」，
**整块笔记不产生划痕**（toast 只有「笔记已保存」）；验收痕迹已清干净
**㉛ 修订二 ✅**（2026-09-14，`b49d57a`，宿主截图圈两处「保持 reader-toolbar 的简洁漂亮」）：
工具栏删 **`rt-s` 副标题行**（论文信息在 `doc-head` + 右栏大纲里都有）与 **`pen-row` 四支笔**
（选笔只在浮条里，㉛）；⚠️ `pen` state 与 `setPen` **保留**（浮条当前笔 + 就地菜单换色还依赖）；
真浏览器真实鼠标事件实测（拖选/上色/就地菜单/擦掉全通）；回归 194 项

**2026-09-15 四件 ✅**（本地已实测；前两件已上生产 `f0b3357`/`c954a4d`，后两件见下）：
**㉞「重新提取」**（`ee60c73`）= 解析缓存的单篇失效出口（删该篇 `doc_cache` + 作废笔记/划痕 +
`conv_attempts` 归零重排队；宿主选 A：全部作废从零重跑）+ 顺带修掉 `conv_attempts` **恒为 0** 的护栏空转 ·
**㉟ 笔记列表改按锚点在原文里的位置排序**（`377f9a2`；病灶 = `list_notes` 原用 `created_at DESC`，
边读边记必然错位）· **「仅中文」正文全空白**修复（`19c6f25`：`.lang-zh .t-en` 被藏 × 只按 `dual`
渲染中文栏）· **注册白名单加 `.ac.cn`**（`957770b`）·
**㊱「待读 → 在读」自动翻转 + `last_read_at`**（第一次滚动上报即翻，`status_at` **只盖一次**；
`last_read_at` 只有滚动写它、不能复用 `updated_at`）·
**㊲ ①c 分块校对：栏间续段只标记**（`PARSE_VERSION` → 7；判据 = 几何 + 文字两条同时成立；
agent 看页图后 `merge_block`；端到端实测合并真的落进产物、译文成一整段）
全量离线回归 **324 passed**（新增 `test_last_read.py` 9 项 / `test_block_seams.py` 22 项）
**㊱ + ㊲ 已上生产 ✅**（2026-09-15 22:3x，`0f24241`；CI 六 job 绿；bundle `index-BdNiDVTl.js`；healthy）：
生产实测 `progress 13` → `待读 → 在读` + `status_at` 盖一次 + `last_read_at` 写入、看板 1/1、文献库那格
「已生成 · 创建 09-15 · 最近阅读 今天」；生产两篇的块**都没有** `seam`（`PARSE_VERSION 7` 之前解析的），
**未重新提取**（≈180 万 tokens，等宿主指示）。两条新教训：
① **远端 `docker compose pull` 要挂着跑**（42MB 那层反复 Retrying ≈ 35–40 分钟；SSH 直接等会被 600s 切断、
镜像拉不全，`docker images` 仍是旧的）；② **后台标签页里的「程序化滚动」不发 `scroll` 事件**
（`Input.dispatchMouseEvent` 也 IPC 超时）→ 看着像功能坏了；判据 = 自挂 scroll 监听器先看事件来没来。

**下一步待宿主指示**：图表 VLM／用量面板／跨计划检索／生产数据集导入

**㊳ 已上生产 ✅**（2026-09-16 19:3x，`a62f8d4`；CI 六 job 全绿；bundle **`index-B8ZJwYtc.js`**；healthy；
公网 health 200）。生产真浏览器实测：**35 个标题里 33 个正文标题的 `.b-inline`/`o` 锚点齐了**
（剩下 2 个"不可划"是**有意为之**：应用自己的「阅读器」`h1` 与 `doc-head` 那行元数据标题）；
在标题块 `b-0003` 上**真实鼠标拖选** → **`mark-bar` 出现（4 支笔 + 加笔记）**；验收只拖不写，
生产库痕迹为零（`notes 8 / highlights 162` 不变）。⚠️ **新踩坑（第三条，写下来）**：
**`cdp("Page.reload")` 之后 harness 的"当前标签"会漂回上一个标签** —— 必须 `switch_tab` 再取一次，
否则会在**别的标签页**上量 DOM（这次连着两次量到了宿主的 duckduckgo 结果页，差点误判成"没修好"）；
另：**后台标签页里真实鼠标事件要么不生效要么被丢** → 判据 = `Emulation.setFocusEmulationEnabled(enabled=True)`
（`visibilityState=visible / hasFocus()=true` 之后拖选立刻正常）—— 这是「后台标签页不算数」那条教训的**解药**。

**㊴ 表格重建 ✅（已上生产 `bff21da`，2026-09-16 21:1x）**：宿主截图「表格和原 pdf
差异较大」→ 选 **C**（交 ①c agent，新增 `set_table`）+ **A**（单元格双语 `rows` + `rows_zh`）。
解析端**从来没做过**表格识别（`table` 一词零出现，粘连行还要白送翻译）；程序侧三条路都不通
（无竖线／`find_tables()` 三策略全抓不到／`strategy="text"` 把双栏正文判成 62×7）⇒ agent 干，
**视觉只判结构、认字必须回文本层** + 护栏三连（逐字来源／形状一致／不吃正文）+ `_table_regions`
几何提示（两条判据缺一不可，37 页真表 3 处零误报）。**不动 `parse.py`、不涨 `PARSE_VERSION`** ⇒
存量要吃修复必须「重新提取」（≈200 万 tokens）—— **宿主 2026-09-16 定「不用，我来操作」**，
故生产上暂时**看不到变化**是预期的。CI 六 job 绿；生产 `revision=bff21da…` / healthy /
bundle `index-BuC2xeWi.js`（与本地同指纹）/ 日志零 error。离线回归 **348 passed**；
端到端真产品路径 + 真浏览器三模式已验。⚠️ 这次 pull 又撞慢层（42.37MB 重试 ≈30 分钟）。

**㊶ 页面家具 = 页眉文字不再丢 + 页边横线补回（解析 v8/v9/v10）✅ 已上生产**
（2026-09-17，`657388f`/`f4476c4`，生产 `revision=f4476c4`；细节见 `design-decisions.md` ㊶）：宿主一次贴三条截图 ——
①关键词顺序不对（行内拆出的两块各带字形 bbox、y0 差 1.4pt → **纵向归一到源行**，v8）
②`Vol.:(0123456789)` 抽出来了（那串字是**纯白色**画的、白纸上等于看不见 → **页边带白字不入产物**，v9）
③「这里少了一条水平线」（每页页眉下那条**矢量细线**：`get_text()` 从不回线条，**不是新 bug**）→
宿主再补一句「**页眉文字不需要有意丢掉。和 pdf 尽量保持一致**」⇒ 撤掉 `_running_headers`
（跨页重复就删，删掉的正是期刊页眉），并补 `payload["band"]`（页边带）/`payload["rule"]`（页边横线）两戳。
**两戳只影响渲染**（`typeset=False` 产物逐字不变，有测试钉住）；装饰类可由阅读器/分享/导出三处复用。
⚠️ **①c 提示词同步加一条**（页眉页脚不属"重复块应删除"、不许并入正文）—— 不加这条 ①c 会把它们删掉、
v10 白做；实测 agent **一个没删**（2 页 / 52k tokens）。`PARSE_VERSION` → **10**。
端到端实测（paper 5 / 8 页）：解析 149 块 → ①c 后 140 块、**8 个页眉块全存活**；
真浏览器三模式 8/8 页 12px 灰字 + `::after` 1px 线宽 511px；**独立视觉通道比对 PDF 确认一致**；
离线回归 **373 passed**（新增 `tests/test_page_furniture.py` 12 项 + 变异检验转红）。
⚠️ 代价（宿主 2026-09-17 选 **A「照译」**，另一选项 B 是免中文）：模型对页眉行**时译时留**
（实测 6/8 页原样返回）⇒ 那几页会挂「待校对」标记（不阻塞转换）。
⚠️ **存量要吃修复必须「重新提取」**（`PARSE_VERSION` 变了）—— 由宿主自己操作（≈200 万 tokens/篇）。
顺带修：`validate.tag_balance` 的正则会被 CSS 注释里的 `<` 干扰（改一句 CSS 文案就能炸掉整篇转换）。

**→ 已上生产并验收（2026-09-17 12:0x）**：CI 六 job 全绿 → `docker compose pull`（52.85MB 层 Retrying 5 次）
+ `up -d app` → `revision=f4476c4` / healthy / 公网 200 / bundle `index-D9a7YvYN.js` / 日志零 error；
生产 CSS 里 `.pg-band/.pg-rule-below/.pg-rule-above` 三条规则实测在位。
生产真浏览器（paper 1）三模式逐列量测：dual `t-en 119/119 + t-zh 87/87`、`lang-zh` 下 `t-en 0/119`、
`lang-en` 下 `t-zh 0/87`；硬刷后控制台**零 error**。**验收必须两分开说**：部署/渲染 ✅ 已验；
**页面上看到页眉与横线 ✗ 还没有** —— 生产唯一那篇（paper 1，139 块）是 v10 之前解析的
（有 `payload.page`、无 `band`/`rule`）⇒ 要看得宿主自己点「重新提取」。
**新手法（可复用、零 token、不动数据）**：`docker exec papershelf python` + `base64` 塞脚本进容器，
调 `render_block(...)` 断言产物含 `pg-band pg-top pg-rule-below`、且 `typeset=False` 产物逐字干净
—— 比只看 bundle 指纹更直接地证明「线上跑的就是这份代码」。
⚠️ **生产数据已变**：现在只有 1 篇（paper 1 = 8 页 139 块《增材制造中的机器学习综述》），
此前那批（37 页真论文 / 3 页 / 合成 / synth-table）都不在了。

**㊷ 页面图形（解析 v11）✅ 已上生产**（2026-09-17，`73098be`/`5769900`，生产 `revision=5769900`；细节见 `design-decisions.md` ㊷）：
宿主「Springer 的图还是没有」一处截图，扒出**四件同源缺陷**（全是"取过没有"）——
① 页眉横线**颜色/粗细是渲染端硬编码的浅灰**（PDF 是纯黑 0.99pt）⇒ `payload["rule"]` 升级成 `{side,color,width}`，
渲染端用 `--rule-c/--rule-w`（内联样式作用不到伪元素，CSS 变量可以）；
② **矢量标识**（Springer 马标、"Check for updates" 徽标）在 PDF 里是**画出来的填充**，
`get_text()` 与图片层都拿不到 ⇒ 新增 `page.get_drawings()` 通道（`_margin_graphics` 三判据 → `_cluster_rects`
→ `_render_graphic` 透明 PNG）→ **新块类型 `deco`**（免中文、不校验、只渲染，`band`/`align` 贴边 + 左右交替）；
③ `_finalize` 的「**无图注就剔除**」把真图删了（37 页篇丢 4 张）⇒ 整段撤掉 + 图注几何配对改「优先下方、其次上方、
允许小幅重叠、全局最近优先一对一」；④ 灰底 `CRITICAL REVIEW` 底纹从未照抄 ⇒ `_margin_shades`
（**主保险 = 色块里包着可见文字**，防"白字黑框"被抄成黑方块）。`PARSE_VERSION` → **11**。
⚠️ 验收中扒出第五件：那枚徽标是「**内嵌图片层 + 矢量层**」叠的，**只取内嵌字节 = 一个光灰方块**
（226 字节的平底板）⇒ 页边带里的无图注小图**也走整体栅格化**。
**真产品路径端到端已验**（`reextract` → ①c 61 轮 103 万 tokens → 翻译）：158 块 / **deco 9 一个没被删** /
表格重建 3 张 / 真浏览器 9 张 deco 全部解码且左右交替正确 / 导出件同带；离线回归 **403 passed**。
**两条教训**：①`naturalWidth=0` ≠ 图坏了（`loading="lazy"` + 首屏外，改成 eager 全好）；
②改 `pipeline/*.py` 不重启 serve = 白改（旧进程用旧解析器覆盖了刚写入的库，且同端口曾并存**两个**进程）。
**待宿主**：生产已上线（CI 六 job 绿 / healthy / health 200 / bundle `index-8YofT6DN.js` 与本地逐字节一致 /
容器内直调证明线上就是 v11 + `typeset=False` 干净 / 真浏览器三模式与三条 CSS 规则实测在位）；
**页面上看到标识与灰底还要宿主自己点「重新提取」**（生产那篇是 v11 之前解析的）。
**死配置已定案删除**（2026-09-17 宿主「不需要 PAPERSHELF_TOKEN_BUDGET 限制」，`08dcd7f`）：
`Settings.token_budget_per_paper` 全仓库没人用（107 万 tokens 也拦不下）⇒ 字段 + `.env.example` +
`docs/install.md` 三处一起删，遗留待定项 **5 结**；**保留** `PAPERSHELF_PROOFREAD_TOKEN_BUDGET`（活代码）。
新增 `tests/test_config_guard.py` 3 项**类不变式**护栏（配置项必须有消费端 / 死名不得被读 / 活那条必须仍接线）。

### 遗留待定项已结（M3 后全部结清或明确推后）
1 会话机制 ✅ · 2 前端框架 ✅（React+TS+Vite）· 3 任务队列 ✅（**v1 常驻队列 + 启动恢复**，㉗）·
7 翻译去重 ✅（PDF hash 缓存）· 8 公式渲染 ✅（**服务端 MathML**）· 10 版权警示 ✅ ·
11 失败呈现 ✅（**全链**：行内显示失败原因全文）· 12 人工修订保护 ✅ · 13 进度回落 ✅（**auto 只增不减**；未读→清零）·
**5 成本护栏数值 ✅ 已结（2026-09-17）**：宿主定「**不做单篇总 token 预算**」→ 那个死配置已删，
成本由 `MAX_CONCURRENCY` / `MAX_ATTEMPTS` / `PAPERSHELF_PROOFREAD_TOKEN_BUDGET`（①c 累计预算，活代码）三处挡住
**明确推后**：4 术语表归属（v1 计划级）· 6 用量面板（已攒 `tokens_used`，v2）·
9 图表 VLM（v2）· 14 标签词表（v1 自由标签）

**㊸ 整页旋转表格页转正（解析 v12）+ ①c 重试 2→20（退避封顶）✅ 已上生产**
（2026-09-17 晚，`c89a4ad`；细节见 `design-decisions.md` ㊸）。宿主两条指示：
「竖向表格提取后格式错了」+「504 重试，放宽到 20 次」。
- **病灶两条都在解析层**：①那张表整页是**旋转 90° 画**的（页面 `/Rotate`=0 ⇒ 只有 span 级
  `line["dir"]=(0,-1)` 说真话）⇒ 表格的**列被当成行**（同行相邻格粘成一句、`Ref` 列跑到表头之前）；
  ②**表注被静默丢**（`Table 2 …` 在任何块里都不存在：`pending_figure` 跨页存活且从未复位）。
  ⚠️ 上轮"①c 校不出来"的诊断**是错的** —— 线上那次 **①c 只校到第 4 页**（504 → 2 次重试用完 → 放弃），
  第 5–8 页从未进过 ①c，看到的正是解析原样。
- **修法**：`show_pdf_page(rotate=…)` **原样重画**（纯坐标变换，不给表格识别加任何特例）→
  转正后它就是普通横排表格；旋转页 `columns=False`（只按行序）；转正副本落
  `p<id>/normalized.pdf`（**不是**全库共享名，否则并发互相覆盖、删文献收不掉）；
  `_h_rules` 先缝**共线短线段**（那张表边框逐格画，单段都不到页宽 25%）再按长度筛；
  ①c 读同一份副本；`PARSE_VERSION` → 12。
- **实测**：解析层 8 页稿 `rotated_pages=[5]` / 表注块回来 / 表区提示 `[]→1`；
  **①c 只跑第 5 页（真 LLM 73k tokens / 55s）→ `set_table` 8×6 全对**、7 个引用编号各自归位；
  **重试真验**（本地起会回 504 的 HTTP 服务 + 真 httpx）4 次请求后成功、退避 2+4+8s；
  离线回归 **421 passed**；**8 次变异检验全部转红**。
- **重试封顶是正确性不是调优**：放宽到 20 次而不封顶 ⇒ 第 20 次失败睡 `2^20` 秒 ≈ 12 天
  （`time.sleep` 真睡 ⇒ 整篇挂死）；封顶 30s 后最坏总等待 ≲8 分钟。新增
  `PAPERSHELF_PROOFREAD_RETRIES`（默认 20）并接线。
- **两条新自坑**：①用 `pytest -o pythonpath=<影子 src>` 做变异检验**根本无效**
  （`tests/conftest.py` 把真 `src` 插在 `sys.path` 最前，影子被顶掉 → "变异全绿"差点误判成
  "测试太弱"）→ 变异检验**要先证明它跑到的是那份代码**；②**别用 `git checkout <file>` 收变异**，
  它把该文件**本轮所有未提交改动**一起抹掉（本次抹掉 converter 两处接线，靠 grep 才发现）。
**→ 已上生产并验收（2026-09-17 18:4x，宿主「push，上生产」）**：CI run `35211627575`
**六 job 全绿** → `docker compose pull`（52.85MB 层**零 Retrying**，≈4 分钟）+ `up -d app` →
`revision=3d7c300` / healthy / 公网 health 200 / bundle `index-8YofT6DN.js`+`index-lPIbuEdh.css`
（与本地逐字节一致，本轮前端零改动）/ 日志零 error；DB + `.env` 事前备份（`…bak.20260917-183946`）。
**容器内直调自证**：`PARSE_VERSION=12` / `DEFAULT_RETRIES=20` / `RETRY_BACKOFF_MAX=30.0` /
`proofread_retries=20` / converter 两处接线都在；现造旋转页喂线上解析器 → `rotated_pages=[1]`、
副本落 `p1/`。**用宿主自己那篇 PDF 预演**（只解析零 token）：`rotated_pages=[5]`、
**`Table 2 …` 表注块回来了**、`[ 92 ]` 跟着自己那行。
⚠️ **生产"看不到变化"是预期**：生产**唯一**那篇（8 页 149 块）是**今天 16:38 上传、v11 解析**的，
而它**就是宿主报的那一篇** —— 库里现状正是病灶（`[ 92 ]/[ 100 ]/[ 101 ]` 排在表头前、每行被
栏间空白劈成两半、**没有块以 "Table 2" 起头**、`proofread.pages=4` 即 504 那次只校了 4/8 页）
⇒ 要吃修复**必须宿主自己点「重新提取」**（≈100 万 tokens，助手不代签）。

### 最近一轮（2026-09-17 晚）㊹ 参考文献「只译标题」：新块类型 `ref`（解析 v13）

宿主：「参考文献没有翻译」→ 两选项（A 整条都译 / B 只译标题）→ **宿主选 B**：只译文献标题，
作者/期刊/卷期页/DOI 保留原文。细节全在 `design-decisions.md` **㊹**。
- **病灶是"可译性"而不是"翻译"**：`refs` 在 `NO_ZH_TYPES` 里 = 整块免中文、根本不送模型（当年为躲
  "送了不译→判漏译→重译"的死循环，且它占正文 26%）。要译标题，前提是**手里有一条完整的条目** ——
  PDF 抽出来的是连续文字流，被块检测切成 ~200 字符碎片（切口落在词中间、一块里塞着下一条的前半截）。
- **解析层 v13**：`_join_ref_fragments`（**只动空白，非空白字符逐字守恒**）+ `_ref_entry_starts`
  （两种编号风格各试取命中多的，**必须连号** —— 这才是挡住 DOI 里 `10.`/`11.` 的真判据）+
  `split_ref_entries`（单条 >2000 字即放弃）。行末断词连字符**故意不猜补**（`Laser-directed` 是反例，
  该不该合是看图那步①c 的活）；切不出就**原样返回**（不丢字、不产假条目）。arXiv 的 `<li>` 天然一条一块 → 直接产 `ref`。
- **翻译层第三条通道**（表格那条的同一取向）：送一条、要一条 JSON `{"title_en","title_zh"}`；
  四道**程序**判据（有中文 / 不许混 DOI-URL / `title_en` 能回原文定位 / 标题不占满整条）；
  定位用 `fold_for_match` **折叠后**比对（空白+大小写+**行末连字符可省** ⇒ 模型把 `man-`+`ufacturing`
  拼成一个词也仍对得上坐标；**产物里的字一个字不改**）。不合格重试 1 次仍不合格 → 不写中文 + 待校对。
- **校验层**：`ref` 移出 `NO_ZH_TYPES`、移入新的 **`PARTIAL_ZH_TYPES`**（要有中文、**不比对数字**）——
  ⚠️ **这条必须随 HTML 走**（`markup` 挂 `data-pt="1"`）：`validate` 拿到的只有 HTML 没有块类型，
  按元素标签判**实测永远不生效**（给每条文献刷一条 `digit_mismatch`）。
- **实测**：真论文（生产那篇 8 页）**28/28 条译出、0 不合格、29,396 tokens（≈1,050/条）**，
  作者/期刊/DOI 逐字保留（连 `%nie&ek`、`https:// doi. org/ …` 的空格噪声都照抄）；真产品路径端到端
  （合成两栏 PDF → 队列 → 真 LLM）4 条碎片 → 4 个 `ref` 块；**真浏览器三模式逐条量测**
  （dual 8 / 仅中文 4 / 仅原文 4，中文栏 4/4 含 CJK、两栏各 511px、悬挂缩进生效）、零 console error；
  离线回归 **460 passed**（`tests/test_refs.py` 新增 38 项）；**5 处变异检验全部转红**。
- **代价/边界**：每条都重发一遍 ~950 tokens 的 system prompt（28 条 ≈2.9 万）—— 改成"一批 10 条"
  可降到约 1/5，本轮**未做**；标题译得好不好仍只能由人看（⑯ 块级修订兜底）。
  ⚠️ **存量没有 `ref` 块** ⇒ 要吃修复**必须宿主自己点「重新提取」**（≈100 万 tokens，不代签）。
**→ 已上生产并验收（2026-09-17 21:1x，宿主「提交，push，上生产」）**：`4f42ff8`（代码）+
`b14d813`（记忆）→**两轮 CI 六 job 全绿**（`35224100637` / `35224627827`）→
`docker compose pull app`（52.85MB 慢层 21:02→21:17 ≈15 分钟、`Retrying` 5 次）+ `up -d app` →
`revision=4f42ff8`（**代码提交**；记忆提交 `b14d813` 的镜像应用内容相同 —— `.dockerignore` 排除 `.emrg/`）
/ healthy / 公网 health 200 / bundle `index-8YofT6DN.js`+`index-lPIbuEdh.css`（本轮前端只加注释）/ 日志零 error；
DB + `.env` 事前备份（`papershelf.db.bak.20260917-210256`）。
**容器内直调自证**（零 token）：`PARSE_VERSION=13` / `NO_ZH_TYPES=['deco','eq','refs']` /
`PARTIAL_ZH_TYPES=['ref']`；现造 3 个碎片 → 切出 2 条；四道护栏逐条试（合格那条回
`…(2024) 金属增材制造（MAM）在车辆零部件生产中的应用——综述. Metals 14(2):195.`，整条都译 → 报
"混进 DOI"，标题没中文 → 报"没有中文"）；`title_span` 对**模型拼回的词**仍定位到 `(44, 79)`；
`ref` 块渲染含 `data-pt="1"`/不含 `data-nt`，未译的 `ref` 才挂 `data-nt`；PROMPT 规则 5 已改口径。
**用生产库里真实的 `refs` 块**（`b-0121`）复验向后兼容：仍挂 `data-nt`、**不带** `data-pt`、
`expects_chinese(refs)=False` / `expects_chinese(ref)=True`。
⚠️ **生产"看不到变化"是预期**：生产唯一那篇（paper 1）**有 29 个 `refs`、零个 `ref` 块**
（v13 之前解析的）⇒ 要看得**宿主自己点「重新提取」**（≈100 万 tokens，不代签）。

_Last updated: 2026-09-17T21:30:00+08:00_
