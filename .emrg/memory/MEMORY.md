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
| `decisions-master` | decision | **[design-decisions.md](design-decisions.md)** | **设计决策总表 ①–㉝（唯一事实来源）**：自托管多用户／全自动管线／沿用已验证管线／标记穿透配对／左右并排／内容保真单栏／计划私有／服务端统一 Key／Python 单体+SPA／实时只读分享／持链接+可撤销+**有效期上限 24h（㉔ 前身）**／v1 只做术语表／**v1 模块全做**／**开放注册限 .edu.cn**／**SMTP 可选+自动降级开号**／**块级修订兜底**／**进度=滚动自动+状态手动（手动优先）**／**导入=PDF 上传 + arXiv 链接**／**元数据自动抽取+自由标签**／**块级 JSON 入库、HTML 为导出格式**／**最小管理页（配置走环境变量）**／**㉔ 责任归用户：README 不提版权风险，改由注册用户协议强校验+留档**／**㉕ README 面向路人重写（讲清差异、保留有代价的具体事实）** + **㉕修订（`c2d2744`）：截图全部换虚构论文（禁真实版权论文）／「计划管理与跟踪」提为核心能力开篇、总览图进正文／阅读目标 200 篇；并修掉"画布 1000px 但渲染仅 727px 导致图内字被缩到七成"+ Pillow 默认位图字体**。**含 ㉓ 公式全量 LaTeX 化 + ㉖ 分享管理（多分享／备注／跨计划管理页／续期／双端倒计时，`SharePanel` 退役）+ ㉗ 转换队列改常驻队列+启动恢复+原子认领（治「生产只转了一篇」）+ **㉘ 元数据 = LLM 抽取（首屏块+候选标题）+ 占位标题可覆写（须落 `title_is_placeholder` 列，靠 pdf_path 推导会被时间戳前缀骗过）+ 前端可手改** + **㉙ 删除文献 = 补前端入口（后端早有）+ 磁盘产物一起收（不收则重导入命中旧解析缓存）** + **㉚ 精读交互 = 侧栏去阅读器（阅读器是文献库下级页，原型 nav 5 项本无此项）+ 句子高亮 + 句锚笔记 + 点笔记跳回原句（②③④ 当日即被 ㉛ 推翻，① 保留）** + **㉛ 划痕 = 任意字符区间 + 四支不带含义的笔**（宿主：「选一支颜色的笔，随意高亮选中的部分，不一定是整个句子」「好看的几种颜色、没有含义」）：**坐标 = 块裸文本 `(block_id,lang,start,end)`**／**尺子 = 服务端 `prose_html` 吐的零宽锚点 `<span class="o" data-o="N">`**（前端按文档序累加、遇锚点「拨」到 N、**遇 `<math>` 整棵跳过 → 公式是原子**，光标落进去吸附两端；两条规则互为兜底 → 永不出现「半个公式」）／**`highlights` 表重做**为 `(id,paper_id,block_id,lang,start,end,color,created_at)`，旧 `(paper_id,sid)` 启动时**重跑切句确定性换算**（换不出的计 `dropped`，不静默丢；`split_en/split_zh` 因此保留并被测试钉住）／**浮条 + 点划痕就地菜单**（`kind:'sel'` 与 `kind:'mark'` 同一个 `.mark-bar`）／**擦划痕只把笔记 `hl_id` 置 NULL**（擦荧光笔≠撕批注）／**跨段选区按块拆开**（块间无共同坐标系）／**落笔只重拉受影响的那几块**（前端自己包 `<mark>` = 第二份排版实现，必分叉）／**笔记文案「第 N 段（中文）· 第 a–b 字」**、圆点带 `pen-<色>` 与色板共色／**只读分享能看不能改**（色板/浮条/表单/删除全不渲染）；**`anchors` 默认 False**（只有阅读器/分享页开 —— 不发锚点只是划不了新的一道，发错地方是往 LLM 输入掺垃圾）；**㉛ 补全：给选区写笔记时自动补一道划痕**（`repo.ensure_highlight` **先查再建**——直接 insert 对同区间是「删旧插新」，划痕换 id 会让别的笔记悬空；与插笔记**同事务**，`tx` 不可重入故拆出无事务的 `insert_highlight`；颜色取当前那支笔；**整块/文献级锚不加**） + **㉜ 阅读顺序 = 分栏感知（gutter 探测 + 通栏块迭代剔除 + 覆盖度兜底），`PARSE_VERSION` → 4** + **㉝ 原文校对 = LLM agent（管线 ①c）**：给**原 PDF 页图**（可 region 放大）+ 抽取结果 + 工具（`read_page`/`read_blocks`/`read_block`/`check_artifacts`/`edit_block`/`split_block`/`merge_block`/`delete_block`/`reorder_page`/`mark_page_done`/`finish`），`finish_reason=tool_calls` 循环；**修订须逐字来自论文** + 护栏（不许暴涨 3 倍/相似度 <0.55）；**漏页不算完成**（`ok_pages` 页级续跑，重跑只补没校过的页）；**成本实测**：37 页 33 页/170 处修订/899k tokens/715s ≈ 27k tokens/页，钱在**每轮固定开销**（system 729 + tools 2159 + seed 2385 + 页图 3097，每轮重发），per-image 只 ~920；**`reasoning_effort=minimal` 是甜点**（3 页样本 auto 16 轮/106,709 → minimal 8 轮/60,665，修订数不变；`off` 质量崩）→ 三条压制：`PAPERSHELF_PROOFREAD_THINKING=minimal` / **seed 只放页级可疑计数**（明细下移到当页 `flags`，否则每轮 2.7k 白烧）/ 每页两轮硬护栏；**宿主 2026-09-15 定「覆盖范围 = 全量逐页」（选项 A）**，不做抽样/仅可疑页 + **㉝ 补记二（`020f4e4`）：`Abstract` 没识别成标题 = 两因叠加** —— ①**PyMuPDF 把加粗小标题与正文并成一个块**（`Abstract` 在产物里**根本不存在**；`Keywords` 与列表在**同一行**）→ 新增 `_split_runin_heads` 行级拆分（**宁可漏拆不许拆错**：A 整行=具名词、不看加粗／B 前导加粗段=具名词或编号标题或字号更大）+ `_RE_H2_NAMED` 补 `ABSTRACT`/`KEY WORDS`/中文「摘要·关键词」+ **`_RE_H3` 会误伤作者行**（`T. Herzog 1,2 · …` 被劈开）→ `_looks_like_numbered_head` 追加「还要像标题」判据；`PARSE_VERSION` → **6**；**12 篇语料字符集逐字一致 12/12**、8 篇找回丢掉的标题；②**agent 根本没有改类型的工具**（`edit_block` 只改文字、`split_block` 沿用原类型）→ 新增 `set_block_type`（`p`/`h2`/`h3`/`h4`；**禁 h1**、图片／参考文献条目不接受、>160 字符要求先拆）+ 提示词按宿主「**保证样式和排版的一致性是很重要的**」新增专节（拿全篇同类比、同级必须同级、作者行／单位行／图注／页眉页脚都不是标题、**拿不准就别改**）+ 统计 `retyped`；**实测**：paper 2 层级已正确 → agent 主动**不改**（0 块）；合成 paper 3（解析覆盖不到的 `Highlights`）→ agent **先 split 再 set_block_type** → p→h2 + 13 项遗留待定 + 5 条贯穿性约束** |
| `repo-history-reset` | decision | **[repo-history-reset.md](repo-history-reset.md)** | **仓库历史重置：删库重建 + 单次初始化提交**（2026-09-13 宿主指示）。现远端 = **唯一初始化提交**（155 文件；旧 SHA `6f487c4`/`350f33f`/`52b142e`… 远端已不存在，只留本地 bundle 备份）。**三条实测结论**：①**删仓库不删 GHCR 包**（包是账号级的，删库后匿名拉 manifest 仍 200、生产容器照跑）—— 但**孤儿包的 linked repository 指向已删仓库 → 新仓库 `GITHUB_TOKEN` push 被拒 `denied: permission_denied: write_package`**（`test` 绿、两个 `build` 红）→ 修法 = **先删整个包**（`DELETE /user/packages/container/papershelf`，需 `delete:packages`）再重跑，同名包自动重建并 link 回新仓库；②**别用 `gh auth refresh` 等宿主**（内部轮询 deadline 太短 → `context deadline exceeded`，验证码其实还有效，白等两次）→ 自己跑 device flow 把 ~15 分钟窗口用满；③**本机 `github.com` 直连被墙**（`login/device/*` timeout）而 `api.github.com` 可用，git 全局代理 `http://172.16.0.40:6501`，gh/curl 打网页端点要显式 `HTTPS_PROXY`。验收：CI 六 job 绿 + `latest`/`sha-<该提交>` 双双 200 + 包 `repository: argszero/papershelf` + 生产 `revision`=该提交/healthy/health 200/bundle 指纹未变；**记忆里别钉这个 SHA**（写记忆即改 SHA） |
| `m5-replicate-prototype` | decision | **[m5-replicate-prototype.md](m5-replicate-prototype.md)** | **M5 = 复刻原型（计划内视图）✅ 已落地**。①推翻了旧文档"总览/文献库/看板是跨计划视图、v1 刻意收窄"（实测原型 L1479 `papers()=activePlan().papers`，**没有跨计划聚合** → 实为**漏做**）；②**验收修掉 7 个真缺陷**（Share 页引用已删类名→无样式、移动端侧栏被藏死、搜索框与 URL 不同步、切换器格式、自造图例、大纲错显 H 徽标、新建按钮）；③**教训：复刻任务里"删旧样式"必须与"迁移页面"同步核对**（先删后迁会静默降级）；④**PDF 分页容器 ✅ 已落地**（宿主选 A，2026-09-11）：解析阶段给**每个块**盖 `payload.page`（`_paged_adder`）+ `doc_cache` 指纹混入 `PARSE_VERSION`（不混则永远命中旧解析产物、分页静默不出现）+ 阅读器/分享/导出三处同版式；⑤**顺带修掉既有真缺陷**：懒加载图无 `width/height` → 整篇高度事后上浮 7.3k px → **大纲跳转偏位 7.5k px**（把页 section 拍平后同样复现，证明与分页无关；已用资产宽高占位修掉） |
| `ci-publish-pipeline-defect` | decision | **[ci-publish-pipeline-defect.md](ci-publish-pipeline-defect.md)** | **CI 全绿却静默删掉了刚发布的镜像**（2026-09-11，修于 `c0a7996`）。`cleanup` 那个 `actions/delete-package-versions@v5`（`delete-only-untagged-versions:false` + 保护名单只认 `latest`/数字 + `min-versions-to-keep:0`）把 `latest`/`sha-<commit>` 一起删了 → GHCR 全 404、包页面「No tagged versions found」、部署机 pull 只能 not found。时序铁证：`merge` 推 latest 成功 → `smoke` 拉 latest 起容器成功 → cleanup 打印「deleted till now: 8」。**教训：破坏性清理的失败模式是"静默删交付物"，应对是直接禁止而非调参；校验必须放在所有会改远端状态的步骤之后（"刚推成功"不等于"还在"）**。已换成只读 `verify` + `tests/test_workflow_guard.py` 5 条护栏。|
| `ui-interaction-real-events` | reference | **[ui-interaction-real-events.md](ui-interaction-real-events.md)** | **验收 UI 交互必须用真实输入事件**（2026-09-13，㉛ 修订 `350f33f` 的教训）。拖选收尾浏览器**还会补一个 `click`** → 挂在 `click` 上的"点别处收浮条"把 `mouseup` 刚点亮的浮条当场收掉（「浮条一闪即没」，主路径不可用）；**合成事件（`dispatchEvent`/`el.click()`）跳过真实序列 `mouseup→click`，所以上一轮"真浏览器全绿"照样漏**。实操：CDP `Input.dispatchMouseEvent`（`_response_timeout=30`）、**必须先 `activate_tab`**（后台标签收不到输入事件，日志全空 ↔ 像"事件没挂上"）、"一闪即没"用 `MutationObserver` 拍增删、取点用 `getClientRects()[0]`（跨行时 rect 中点在行间空隙）。同族：假 SMTP／只看截图不量 computed style —— **验收要跑在最接近真实的那一层** |
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
**下一步待宿主指示**：图表 VLM／用量面板／跨计划检索／生产数据集导入

### 遗留待定项已结（M3 后全部结清或明确推后）
1 会话机制 ✅ · 2 前端框架 ✅（React+TS+Vite）· 3 任务队列 ✅（**v1 常驻队列 + 启动恢复**，㉗）·
7 翻译去重 ✅（PDF hash 缓存）· 8 公式渲染 ✅（**服务端 MathML**）· 10 版权警示 ✅ ·
11 失败呈现 ✅（**全链**：行内显示失败原因全文）· 12 人工修订保护 ✅ · 13 进度回落 ✅（**auto 只增不减**；未读→清零）
**明确推后**：4 术语表归属（v1 计划级）· 5 成本护栏数值 · 6 用量面板（已攒 `tokens_used`，v2）·
9 图表 VLM（v2）· 14 标签词表（v1 自由标签）

_Last updated: 2026-09-14T20:40:00+08:00_
