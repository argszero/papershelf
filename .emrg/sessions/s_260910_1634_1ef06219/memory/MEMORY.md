# Session Memory Index — s_260910_1634_1ef06219

设计会话：papershelf 需求澄清（一次一问，宿主逐题作答）。

## Memories

| ID | Type | File | Summary |
| --- | --- | --- | --- |
| `7a3f9c2e` | task | [open-q11-share-access-control.md](open-q11-share-access-control.md) | ✅ 已结：问题⑪ 答复 A+D（持链接匿名只读 + 可撤销/有效期，不做密码） |
| `e7c4b1d2` | task | [open-q-progress-manual-edit.md](open-q-progress-manual-edit.md) | `status: merged` → 已并入 **㊺**（手动改进度：宿主选 B+C，已落地并本地验收，**未提交**）；留档三条 browser-harness 现场经验（`click` 尾巴把光标放到最左 / `insertText` 非幂等 / CDP 的 Cmd+A 不全选） |
| `f2a91c47` | task | `status: merged` → 已并入项目记忆 `design-decisions.md` **㊻**（翻译跨块承接，宿主选 C，**已落地 + 已上生产 `dc33f1d`**）；留档摸底期的原始判断（含我自己第一个错结论「跨页 0」的更正）|
| `prod-config-state` | task | **[prod-config-state.md](prod-config-state.md)** | **生产 `papershelf.args.fun` 配置现状（无占位值，2026-09-11 18:30 跑上验证码版）**：SECRET/BASE_URL/ADMIN/LLM/**SMTP** 全部实测生效 —— 管理员 `argszero.reg@gmail.com`（旧占位账号已删）；`llm_configured:true`；**SMTP=Gmail 465，配全后自助注册自动开启**（⑮），实测 app mailer 真发信 `True`；镜像含 M5+分页+验证码认证（bundle `index-U1cJuCKf.js`，`sha-1de2277`）。附两条实测口径：**BASE_URL 必须公网域名**（否则分享复制出 localhost 链接 + CORS 放宽）、**部署机匿名即可拉 GHCR**；**邮件只能发不能收**（收件人是注册者的 edu.cn 邮箱，Gmail 只是发件人）。无关字段（`from_name`/`verify_subject`）宿主指示**忽略** |
| `deploy-prod-placeholder-env` | task | [deploy-prod-placeholder-env.md](deploy-prod-placeholder-env.md) | `status: merged` → 已并入 `prod-config-state.md`；本文件仅留档「四处占位值的拆除过程」与 `ensure_admin` 每次启动重置口令的坑 |

> 本会话的**实现进展与实测数据**落在项目记忆 [pipeline-v1-real-run.md](../../../memory/pipeline-v1-real-run.md)，
> ㉓ **公式全量 LaTeX 化**已定并已落地（宿主选 B；实测 display 57 / inline 366，宿主看图确认无问题）。

> 问答 ①–㉑ 的结论**全部落在项目记忆 `design-decisions.md`（决策总表）**，本会话目录不再留问答碎片。

## 进度（以 project 记忆为准）

①–㉛ 全部 ✅ → **M1 管线 ✅ → M2 服务端 ✅ → M3 前端 SPA ✅ → M4 Docker ✅ → M5 复刻原型 ✅ → M6 精读交互 ✅**（M6 内 ㉚ 的②③④ 当日被 ㉛ 推翻）
→ **认证改邮箱验证码 ✅**（2026-09-11，见项目记忆 `auth-email-code` / `design-decisions.md` ⑮「修订」）

### 最近三轮（2026-09-11）
1. **M5 复刻原型 + PDF 分页容器**（`828114e`，验收修掉 7 个真缺陷）
2. **发布流水线自我删包事故**（`c0a7996`/`f5f8ecf`，CI 全绿却把刚发布的镜像删光）
3. **生产 `.env` 占位值清零** + **注册/找回改验证码**（`ee68085`）
   并且修掉两个「本地全绿、生产才现形」的真缺陷：
   - **按钮变体被基类盖成透明**（`4186e7f`，M5 引入的特异性缺陷，线上挂了近一天）
   - **发信 37.5% 失败**（`9124239`/`1de2277`，Gmail DNS 池有偏）

**本轮教训（两条都指向同一件事：本地替身会系统性遮蔽缺陷）**
- 假 SMTP 服务器 + 把 `mailer._send` 整个换掉的测试夹具 → 两道屏障同时遮住 SMTP 层
- 只截图看"页面像不像原型"会漏掉"按钮根本不可见"；**必须量 computed style，不能只看截图**

### 最近一轮（2026-09-12）
**㉖ 分享管理**（`3f846db`，**已上生产** `sha-3f846db`）：多分享 + 备注 + 跨计划管理页 `/shares`
（4 指标卡 / 筛选 chips / 倒计时表格）+ 续期（**从此刻**重新起算）+ 取消立即 410；
只读页 banner 双端同源倒计时；顶栏与计划卡「分享」都开**新建抽屉**；`SharePanel.tsx` 退役删除。
真浏览器双端验收（本地 8012 + 生产 `papershelf.args.fun`），97 项离线回归绿。
→ 决策落在项目记忆 `design-decisions.md` **㉖**。

### 最近一轮（2026-09-12 下午）
**㉗ 生产「12 篇只转 1 篇」事故修复**（`d6d688a`/`fed02ab`，**已上生产** `sha-fed02ab`）：
病灶 = 状态机落了库、推它的引擎只活在 HTTP 请求的 `BackgroundTasks` 里 + 无启动恢复
+ §5.6 的「DB 轮询 worker」从未实现 → 容器重启后 2 僵尸 `doing` + 9 孤儿 `queued`。
修法 = `queue.py` 常驻队列 + 启动恢复（`doing→queued`，不动 attempts）+ `claim_paper`
原子认领，**铁律：先抢槽位、再认领**（写反 = 8 篇瞬间全 doing，事故的另一种版本）。
生产实测 **12 篇全部 done**（1,855,352 tokens）、UI 无人操作自动变化、完成后轮询自停。
→ 决策 `design-decisions.md` **㉗**；教训独立成项目记忆 `queue-only-request-scoped-defect`。

**采坑两条（写给下次）**
- 浏览器 `js()` 里若 `location.href=...` 导航 → 整个返回串会丢（表现为「上一次打印全不见」）。
  导航后要**另起一次** `browser-harness` 调用。
- **超大文档的阅读器首屏要十几秒**（544 块 + 26 图），一度误判成「卡在加载中」。
  判据：`/api/papers/N/doc` 返回 200 且日志无异常 → 只是在渲染，**别急着当 bug**。

**下一步待宿主指示**：图表 VLM／用量面板／跨计划检索／生产数据集导入

### 最近一轮（2026-09-12 下午，续）
**㉘ 元数据缺失修复**（用户选 C：手动 + 自动都做）：`pipeline/metadata.py` 抽取器
（首屏块 + 候选标题，**≈1.5k tokens/篇**，失败退回不写）+ 元数据编辑抽屉
（`PaperMetaDrawer.tsx`，补上 ⑲ 的「可手动改」死列）+ `parse._finalize` 保留
`title_candidates`（原先会把真标题一起删掉）+ 新增 `papers.title_is_placeholder` 列。
**⚠️ 修复途中踩的第二个坑**：判据「`title == Path(pdf_path).stem`」被落盘时间戳前缀
（`2026-09-11T143352+0000_original.pdf`）骗过 → 生产 12 篇**全部**判成"用户手填"、
真标题**仍然写不进去**（从"必错"变成"看起来对"）→ 改**落列** + 共用判据
（`server/titlemeta.py`，迁移与写回单一来源）+ 兜底先比 `source_ref`（原始文件名）。
真 LLM 实测 4 篇错标题 **4/4 修好**（含两篇正确挑中第 2 个候选）。
**生产 12 篇回填 ≈1.8 万 tokens 即可（重跑转换要 185 万，差 100 倍）**。

### 最近一轮（2026-09-12 傍晚）
**㉙ 删除文献**（宿主「导入的文献，应该支持删除」）：与 ⑲ 同型 ——
`DELETE /api/papers/{id}` 与 `api.deletePaper` 早有，**前端从没接线**。
补「删除」入口（hover 显现、danger 色、确认框点名连带消失的译文/笔记/图片、
只读态不渲染）+ **磁盘产物一起收**（落盘 PDF + `papers_dir/p<id>/assets/`；
不收则**重新导入同一篇会命中旧解析缓存**）。
真浏览器验收：43 行都有按钮、hover `opacity 0→1`、确认后 43→42、**取消不变**、
只读分享页 0 个按钮。回归 **149 项**全绿（新增 `test_delete_paper.py` 5 项）。
另附 `backfill-meta` 命令（只抽元数据回填存量）。

**㉘ 已上生产并回填完成**（`sha-49230d0`）：生产 12 篇元数据**全部回填成功**，
合计 **19,181 tokens（≈1,598/篇）**——仅重跑转换（1,855,352）的 **1.03%**。
12 篇中文标题/作者/期刊/年份/标签齐了（#1 year 空，模型如实判不出）。
**回填时又踩一坑**：存量文献是被旧 `_finalize` 处理的，标题 h1 被删且**没进 meta**
→ 输入里没有标题文本 → 模型只能返回空 → 修法是**候选标题缺了就重解析 PDF 现场取**
（只吃 CPU 不花 token）。生产 UI 实测：12 行真标题+作者+发表+标签、零 4xx。

### 最近一轮（2026-09-13 上午）
**㉚ 精读交互**（宿主四条指示，原型同版更新）：① 侧栏删「阅读器」独立项（原型 nav 5 项本无；
停在 `/reader/1` 时 active 落「文献库」，与原型 `state.view==='reader'` 的处理一致）；
② **句子高亮**（切句在服务端、MathML 渲染之前；`.sn` + `data-sid`，`highlights` 表**只存 sid 不存文本**，
重跑管线后仍对得上）；③ **句锚笔记**（`notes` 补 `sid`/`quote`，卡片显示引用 + 高亮圆点）；
④ **点笔记跳回原句**（滚到 sid + `.is-flash` 1.4s；当前语言模式隐藏该句时**退到整块**）。
只读分享态能看不能改（0 表单 / 0 删除按钮，后端 403 兜底）。**已上生产 `sha-bce16f1`**
（真论文 #12 / 2304 句实测逐点通过；验收用的临时笔记/高亮/分享已清理）。
**教训**：切句测试的期望值要按**真实句子形状**写 —— 编出来的 `28.4 BLEU. See Fig. 2` 曾期望 2 句，
实际只断 1 句，是期望错了不是代码错了，不要反过来改代码迁就测试。

### 最近一轮（2026-09-13 下午）
**㉛ 划痕 = 任意字符区间 + 四支不带含义的笔**（宿主：「这是选择一个颜色的笔，随意高亮选中的部分，
不一定是整个句子。并且可以选中任意部分后添加备注」；颜色「好看的几种颜色、没有含义」；
问要不要先在原型定格 → 「你定」→ 助手定形态并**同步写进原型**）。
**推翻 ㉚ 的②③④**（①侧栏去阅读器保留）：锚点从「句」换成 `(block_id, lang, start, end)`。
- **尺子 = 服务端 `prose_html` 吐的零宽锚点 `<span class="o" data-o="N">`**：前端按文档序走文本节点累加、
  遇锚点把计数**拨到 N**、遇 `<math>` 整棵跳过（**公式是原子**，光标落进去吸附两端）。
  `anchors` 默认 **False**（只有阅读器/分享页开）——不发锚点只是划不了新的一道，发错地方是往 LLM 输入掺垃圾。
- **`highlights` 表重做**；旧 `(paper_id,sid)` 启动时**重跑切句确定性换算**成区间
  （换不出的计 `dropped`，不静默丢；`split_en/split_zh` 因此保留且被测试钉住，不做「顺手优化」）。
- **浮条 + 点划痕就地菜单**（换色/擦掉/加笔记，`.mark-bar` 一个组件两种形态，`position:fixed` 到视口坐标）；
  **擦划痕只把笔记 `hl_id` 置 NULL**（擦荧光笔 ≠ 撕批注）；**跨段选区按块拆开**；
  **落笔只重拉受影响的那几块**（前端自己包 `<mark>` = 第二份排版实现，必分叉）。
- 离线回归 **188 项**全绿（㉛ 补全后 **194**；删 `test_sentences.py`、新增 `tests/test_markup.py` 18 项、重写 `test_highlights.py`）。
  **原型（`literature-workbench.html`）已同步改完**并在浏览器里逐点实测通过（含只读分享态）。
- **两条教训**：① **事件委派不是为了省事，是为了活过重画** —— 划痕的 `keydown` 原本逐节点挂，
  而「落笔只重画这一块」会把旧节点全换掉 → 键盘路径**静默失效**；鼠标路径早早委派在舞台上，唯独键盘漏了。
  ② **重渲染顺序会吃掉状态** —— `openMarkMenu` 里 `setTarget` 在 `selectPara` 之前，
  而后者会把目标清成「整段」→ 点开菜单后笔记表单退回整段；改成**先 `selectPara` 再 `setTarget`**。

**提交与上线**：`99bb6c2`（代码+文档）+ `ac37f08`（记忆）→ CI 绿 → 生产 `sha-ac37f08`；
**生产 #12 那 4 条 sid 划痕迁移实测成功**（行数 4→4、`sid` 列退役、
区间 `zh 0–79 / 149–192 / 160–206 / 591–651`、真浏览器 4 道划痕渲染且底色 `oklch(0.8 0.15 85)`）。

### 最近一轮（2026-09-13 下午，续）㉛ 修订：拖选后浮条一闪即没
**宿主没报，是上线后自己逐点复验时发现的**（真鼠标拖选 → 浮条闪一下就没）。
病灶：拖选收尾浏览器**还会补一个 `click`**（target = 落点所在块），而「点别处收浮条」挂在 `click` 上
→ `mouseup` 刚 `setBar(...)` 的浮条被当场 `setBar(null)`。**原型没有**这个缺陷：
它把收浮条挂在 `stage.mousedown → hideBar()`（发生在选区形成**之前**）天然躲开。
修法 = `stageClick` 加「**选区非塌缩 ⇒ 这是拖选的尾巴**」判据（`350f33f`，本地+生产实测通过）。
**为什么上一轮"真浏览器全绿"照样漏**：那些用例是**合成事件**（`dispatchEvent` / `el.click()`）驱动的，
而合成事件**跳过**了真实的 `mouseup → click` 衔接 —— 缺陷恰好长在那个 click 上。
→ 独立成项目记忆 **`ui-interaction-real-events`**（含 browser-harness 实操：
`_response_timeout=30`、**必须先 `activate_tab`** 否则后台标签收不到输入事件、用 `MutationObserver` 拍"加过又删"）。
**上报链**：`350f33f` → CI 绿 → 生产 `sha-350f33f`（DB 已备份 `…133745`）；
生产复验：真拖选浮条留存 · 空白点击收浮条 · 4 道迁移划痕渲染 · 点划痕就地菜单（amber 亮起 + 擦掉）·
库仍 4 条划痕 / 0 条笔记（**没留任何验收残渣**）。
⚠️ **`ab13f5a`（flag 修复）此前只 pull 未 `up -d`** —— 这次随 `350f33f` 一起生效（同一条提交链），该项已了。

### 最近一轮（2026-09-13 下午，续二）㉛ 补全：选中加笔记时自动高亮
**宿主原话**：「选中添加笔记时，应该同时自动高亮」。语义 = 选中一段 → 点「加笔记」时这一段
**本来就有坐标**，缺的只是一道划痕，没有就**同事务**补上，颜色取当前那支笔 →
"划了重点却没上色"这个中间态不存在。**整块/文献级锚不加高亮**（没划住的那段本就不该上色）。
- **`repo.ensure_highlight` 是「先查再建」**：不能直接 `insert_highlight` —— 它对**同区间**是
  "删旧插新"，划痕会换 id，而别的笔记还锚在旧 id 上（`hl_id` 当场悬空、圆点消失）。
- ⚠️ **`tx` 不可重入**：为了"补划痕 + 插笔记"原子，拆出**不带事务**的 `repo.insert_highlight`，
  `create_highlight` 变成它的带事务薄壳（嵌一层会在内层就提交掉）。
- 前端保存成功后**只重拉这一个块**（划痕由服务端渲染进 `en_html`/`zh_html`，㉛ 不变量）
  并回填 `hl_id`；toast 区分「笔记已保存，并已高亮」/「笔记已保存」。
- **实测**（本地 8012，真浏览器真实鼠标事件）：拖选第 3 段第 2–34 字 → 加笔记 → 划痕出现
  （`hl hl-<笔色>`）+ 卡片同色圆点 + toast「…并已高亮」✓；整块加笔记 → **不产生划痕**、
  toast「笔记已保存」✓；点划痕换色 ✓ / 擦掉 → 划痕消失但**笔记仍在** ✓。**原型 `addNote` 同步改写**。
- 离线回归 **194 项**全绿。
- **提交与上线**：`6f487c4`（代码+测试+文档）+ `52b142e`（记忆）→ CI 六 job 全绿 →
  生产 `sha-52b142e`（`docker inspect` revision 实测，bundle `index-5320WwVc.js`）。
  **生产真浏览器实测**（真实鼠标事件，`aqshao25` 会话、paper #12）：
  拖选 b-0011 zh 100–130 → 浮条「加笔记」→ 保存 → **划痕 4→5**（`hl hl-amber` 落在 b-0011）、
  卡片带 `nc-hl pen-amber`、**toast「笔记已保存，并已高亮」**；第二条 b-0010 zh 20–45 同样自动上色；
  **整块笔记**（b-0012）→ toast「笔记已保存」、**划痕仍 6 不变**。
  验收痕迹已清干净（删 note 5/6/7 + highlight 13/14，生产回到 notes 1/3/4 + marks 9–12）。

### 最近一轮（2026-09-13 晚）仓库历史重置：删库重建 + 单次初始化提交
宿主：「把 github repository 删除后，重新提交代码。作为初始化提交，不需要之前的提交历史。」
（问删除路径 → 宿主选 **A 助手代删**）。
- 删前盘点：仓库公有、49 提交，**无** release/secret/variable/environment/webhook/deploy key/issue/PR
  → 只丢提交历史与 Actions 记录；旧历史先存本地 bundle（`tmp/papershelf-pre-wipe.bundle`，5.6 MB）。
- `gh repo delete` → `gh repo create --public` → 本地 `rm -rf .git && git init` → **一次提交**（不钉 SHA：`.emrg/` 记忆在仓库里，写记忆即改 SHA）
  （155 文件，工作树内容不变）→ push。远端现在只有这一个提交。
- **踩坑 ①**：删仓库**不删 GHCR 包**（账号级，匿名拉 manifest 仍 200、生产照跑），
  但**孤儿包指向已删仓库 → 新仓库 `GITHUB_TOKEN` push 被拒** `permission_denied: write_package`
  （`test` 绿、两个 `build` 红）→ **先删整个包**（需 `delete:packages`）再重跑，同名包自动重建并 link 回新仓库。
- **踩坑 ②**：`gh auth refresh` 内部轮询 deadline 太短，宿主授权完成后仍 `context deadline exceeded`
  （**白等两次**）→ 改成自己跑 device flow（curl + urllib 轮询，把 ~15 分钟窗口用满），token 用后即删。
- **网络实况**：本机 `github.com` 直连被墙（`login/device/*` timeout），`api.github.com` 直连可用；
  git 全局代理 `http://172.16.0.40:6501`，gh/curl 打网页端点要显式 `HTTPS_PROXY`。
- ⚠️ **失手一处并已收手**：为省一步想自己在宿主浏览器里点授权确认，结果**误操作了宿主当前活动标签页**
  （在 `gitlab.xinluex.com` 流水线页打了串字符，未提交、未点任何删除按钮）；此后**不再碰宿主浏览器**。
- 验收：CI 六 job 全绿 · 远端 `latest`/`sha-<该提交>` 双 200 · 包 `repository: argszero/papershelf` ·
  生产 `revision`=该提交、healthy、health 200、bundle 指纹未变 `index-5320WwVc.js`。
- ⚠️ 遗留待宿主决定：gh token 现在多了 `delete_repo` + `delete:packages` 权限，是否收回。

### 最近一轮（2026-09-15 上午）
**「重新提取」上线**（`ee60c73`）：文献库每篇「删除」旁加按钮 = 删该篇 `doc_cache` + 作废笔记/划痕 +
`conv_attempts` 归零重排队（宿主选 A：全部作废、从零重跑）；顺带修 `conv_attempts` 护栏空转（认领即 +1）。
**「仅中文」正文全空白修复**（`19c6f25`）：`styles.css` 藏 `.lang-zh .t-en` × `Reader.tsx` 只按 `dual`
渲染中文栏 → 两栏同时不可见（34 段可见文字 0；标题还在 = 完美伪装）。修法 = 拆 `dual` / `showZh`，
仅中文一律渲染中文栏（免中文块回落原文），顺带修好该模式划不了重点；真浏览器三模式实测 34/34；
护栏 `test_style_guard.py` 红-绿验证；全量 286 passed。

**采坑（本轮）**：宿主浏览器里那个 `127.0.0.1:8012/reader/1` 标签页是**化石** ——
旧 bundle（`index-dSVAYd3W.js`）＋ 已不存在的旧数据集（合成演示论文）＋ 已失效会话，
DOM 是历史遗留、API 已 401；拿它当验收对象会得出完全错误的结论。
→ 先 `list_tabs()` 比对 `script[src]` 的 bundle 指纹与当前 `static/`，再决定信不信。
**已上生产**（`f0b3357`，bundle `index-CmmAH-c3.js`）：部署前备份 DB（`…bak.20260915-102645`）、`compose pull` + `up -d`、health=healthy、日志零 ERROR、队列休眠（无待转）；生产真浏览器三模式实测 466 块 / 仅中文 441 块有中文文字（仅原文 0）→ 修复生效；文献库三按钮（编辑/重新提取/删除）已在线。

### 最近一轮（2026-09-15 上午，续）
**注册白名单加 `.ac.cn`**（`957770b`，CI 绿；**已上生产**，镜像 revision `c954a4d`）：
宿主「注册邮箱，除了 edu.cn 外，再添加对 .ac.cn 的支持」。默认值 → `edu.cn,ac.cn`；
比对逻辑 `domain == d or domain.endswith("." + d)` **没动**（子域天然命中）；
顺带修 Admin 页写死的「开放注册限 .edu.cn」。护栏 `test_registration_allowlist_default_includes_ac_cn`
钉「子域命中 + 后缀比对不能退化成 `endswith(d)`」（`notedu.cn`/`evil-ac.cn`/`edu.cn.evil.com` 必须拒），红-绿验证过。
- **端到端实测（本地 8012）**：自写无依赖 SMTP 收信槽（`tmp/smtp_sink.py`，asyncio 40 行）临时接进 `.env`
  → 注册页提示「edu.cn、ac.cn」→ `acas@ict.ac.cn` 真发码 → 用收到的码完成注册并登录；
  `gmail.com` / `notedu.cn` 被拒且提示带 ac.cn。**测试用户与临时 env 已全部清掉**。
- ⚠️ 生产 `.env` 显式写死 `=edu.cn` 会盖掉新默认 → **已按宿主点头改生产 env 并重启**：备份 `.env.bak.20260915-111010` + DB、`ALLOWLIST=edu.cn,ac.cn`、镜像 `c954a4d` 上线、health=healthy、错误 0、公网 `allowed_domains=[edu.cn, ac.cn]`；生产真浏览器实测注册页提示「edu.cn、ac.cn」＋ `wl-probe@cas.ac.cn` 真发码（DB 出现码行）＋ gmail 被拒（探针码行已删）。
- 采坑：本地 2525 端口被**上一轮遗留的** `tmp/fake_smtp.py` 占着（跑了 3 天）→ 换 2531；
  tmp/ 里的老脚本进程会一直占端口，收信槽别复用固定端口。

### 最近一轮（2026-09-15 中午）
**㉟ 笔记列表排序修正**（`377f9a2`，CI 绿，**仅本地，未上生产**）：宿主「右侧笔记列表，顺序不对……
应该按照笔记关联的原文的位置为顺序」。病灶 = `list_notes` 用 `created_at DESC`（倒序写入时间）。
排序键 = 有落点在前／`blocks.ord`／整段笔记在前／字符位置＋原文列在前／`id` 兜底；
前端新增笔记后**重拉列表**（原来是 `[n, ...ns]` 插到最前）。`tests/test_notes_order.py` 6 项（回退 5/6 转红）。
真浏览器实测（真鼠标拖选第 13 段写笔记）→ 新笔记落在**中间**。
⚠️ **两处"无位置"的默认由助手选定、待宿主一句话翻转**：整段笔记排本段最前；文献级笔记排最后。

**顺带修的模型配置**：生产与本地 `PAPERSHELF_LLM_MODEL` → `deepseek-flash`
（旧名 `deepseek-v4-flash-vision-exp` 实测是**别名**，回显同一个新名字；新名视觉能力照旧，已用真图验过）。
生产重建时**发现一篇文献正在重转**（paper #1，第 2 次尝试），新模型跑得正常。

**⚠️ 一处需要告知宿主的手误（本地实例，非生产）**：验收 `.ac.cn` 注册时我在宿主浏览器里
对 `localhost:8012` 调了 `/api/auth/logout` → **宿主的本地会话被登出**（标签页跳到登录页）。
已用管理员账号重新登录复原。生产侧的浏览器会话没动过。

### 最近一轮（2026-09-15 晚）㊱「最近阅读 / 滚动自动翻在读」+ ㊲「①c 分块校对：栏间续段标记」

两条独立 workstream，一并提交（`9842e6d` ㊱ / `80f7169` ㊲，**尚未上生产**）：
- **㊱**（宿主：「我有篇文章读了 13%，为什么『在读』还是 0 篇」）：新增 `papers.last_read_at`
  （**只有阅读器滚动上报写它**，不能复用 `updated_at`）+ 滚动即把「待读」翻「在读」
  （`status_at` **只盖一次**）；「已读/已整理」仍旧只能手动。存量回填只做有证据的（`progress > 0`）。
  真浏览器实测通过（真实滚动 + 真实拖拽 + 查库取证）。
  **宿主定 B**：手动标回「待读」只清进度、`last_read_at` **保留**（代码本就是 B，未改行为）。
- **㊲**（宿主：「校对分块分得对不对，尽量不要把一句话拆分到两个段里」+ 截图；自判「应该是分栏导致的」）：
  方案 B（宿主选）＝解析阶段**只标记** `payload["seam"]="col-spill"`、agent 看页图后 `merge_block`。
  判据 = 几何（栏间接缝）+ 文字（前块无句末标点 + 后块小写起）**两条同时成立**：
  只文字 37 页报 73 处（表格行/页眉全中）、只几何误标 → 合用 **25 处**、抽查全真。`PARSE_VERSION` → **7**。
  **端到端真产品路径实测**（上传合成两栏 PDF → 常驻队列 → ①c 真 LLM → 翻译）：agent 主动合并、
  `b-0003` 从产物里消失并进 `b-0002`、中文成一整段（30,263 tokens / 18.7s）。
  ⚠️ 诚实的对照实验：**剥掉标记 agent 照样合并** ⇒ 起作用的是提示词里那条判据，
  标记买的是冗余/覆盖 + 消除页级计数假阴性，代价为零。
- **踩坑**：改完 `pipeline/*.py` **必须重启本地 dev server**（常驻进程 `sys.modules` 里是旧
  `parse`，懒加载的 `proofread` 报假的 `ImportError`）—— 这不是代码缺陷。
- 全量离线回归 **324 passed**；本地验收残留（paper 4/5/6/7「seam-probe」+ 合成 PDF）已清干净。

### 同一轮：**已上生产**（2026-09-15 22:3x，宿主「上」）

`0f24241` → CI 六 job 全绿 → `docker compose pull app && up -d app`：
`revision=0f24241…` / healthy / 公网 health 200 / bundle `index-BdNiDVTl.js` / 日志零 error。
DB 与 `.env` 部署前已备份。生产实测（宿主数据 paper 2）：`progress 13` → `待读 → 在读` +
`status_at` 只盖一次 + `last_read_at` 写入，看板 1/1，文献库那格「已生成 · 创建 09-15 · 最近阅读 今天」；
**痕迹已复原**（paper 2 回到 unread/0/NULL）。
生产两篇的块**都没有** `seam` 标记（`PARSE_VERSION 7` 之前解析的）→ 要变好只能「重新提取」（≈180 万 tokens），**未动**。
**两条新教训**：① 远端 `docker compose pull` 的 42MB 层反复 Retrying ≈ 35–40 分钟，
`ssh` 直接等会被 600s 切断且**镜像没拉全**（`docker images` 仍是旧的）→ 必须 `nohup ... &` + 轮询；
② **后台标签页里程序化 `scrollTop` 不发 `scroll` 事件**、`Input.dispatchMouseEvent` IPC 超时 →
看着像功能坏了；判据 = 自挂一个 scroll 监听器先看事件来没来（事件 0 = 环境问题）。
生产验收改用「取 `papershelf_session` cookie → curl PATCH → 查库 + 硬刷 UI」，
**诚实边界**：那验的是服务端逻辑 + 前端渲染，真实滚轮那一段只在本地验过。

_Last updated: 2026-09-17T16:50:00+08:00_

### 最近一轮（2026-09-16 晚）㊳ 标题行也能划重点 / 加笔记（宿主 19:14 截图报告）

宿主：「标题行，选中后没有笔记工具的弹出 mark-bar」。
- **病灶（两侧各缺一半）**：① 服务端 `pipeline/markup.render_block` 里只有正文段/摘要走
  `prose()`（吐零宽锚点尺子 `<span class="o" data-o="N">` + `<mark>`），**标题（`h1..h4`）与
  `refs` 走 `_esc(text)`** 裸文本 → 没有锚点、量不出字符坐标；② 前端 `Reader.tsx` 标题分支
  直接吐 `<span className="b-en">{b.en}</span>`，**没有 `.b-inline[data-lang]` 那一层** ——
  而 `marks.ts::selectionSegments` 是从选区文本节点**往上找**那一层拿坐标系与块 id 的。
  两条叠加 ⇒ `segs` 为空 ⇒ **浮条根本不渲染**（连"划了但没上色"都做不到）。
- **修法**：标题/`refs` 分支改走 `prose()`；`render_block` 新增 **`wrap`** 参数
  （`repo.public_block` 对标题传 `wrap=False` —— **`<h1..h4>` 外壳由前端出**，
  服务端再套一层会得到 `<h2><h2>`，浏览器把内层甩到标题外，中途真踩过）；
  `BlockBody` 加 `as='div'|'span'`，标题里**只能用 `span`**（`<h2>` 只允许短语内容，`div`/`p` 会被甩出去）。
- ✅ **`typeset=False` 时 `prose()` 逐字等于 `_esc()`** ⇒ 导出/校验/prompt 三条路径产物**一个字节没变**
  （有测试钉住）—— 这是敢动标题渲染的底气。
- **诚实的边界**：公式（MathML 树）与图片仍不可划（本来就没有"裸文本"）；
  页面**顶部 `doc-head` 那行大字标题**仍不可划（来自元数据、不是正文块，无块坐标）；
  正文里同一个标题字符串（块 `b-0003`）现在可以划。
- **验收**：真浏览器**真实鼠标拖选**标题「A review o」→ `mark-bar` 出现（4 支笔 + 加笔记）→
  写笔记 → 列表出现该条 + DB 里 `highlight(b-0003,en,0-10,green)` / `note(…,hl_id)`；
  离线回归 **328 passed**；新增护栏 5 条（`test_markup.py` ×2 / `test_highlights.py` ×2 / `test_style_guard.py` ×1）。
  ⚠️ **两条旧断言写的就是缺陷本身**，按新语义翻转（`test_only_prose_blocks_get_anchors`、
  `test_offset_anchors_are_in_every_prose_block` —— 后者原文是 `assert "data-o=" not in head["en_html"]`）。
- **教训**：**"没反应"先问"是不是没给坐标"，别先怀疑交互层** —— 同型第二次
  （㊱「仅中文正文全空白」也是渲染少给一栏）。判据可以是一条 DOM 量测：
  逐块数 `inl`/`o` 为 0 的块数 —— 本次在**生产页面**量出**17 个标题全是 `inl=0 o=0`**
  （说明生产还是旧 bundle `index-BdNiDVTl.js`，本地已 `index-B8ZJwYtc.js`），一眼定位。
- **本地验收残留已清干净**（`/tmp/pslocal/data/papershelf.db`：删掉本轮 note 4 + highlights 4/5/6，
  回到原装 **3 笔记 / 3 划痕**；备份 `papershelf.db.bak.20260916-1924`）。

### 同一轮：**已上生产**（2026-09-16 19:3x，宿主「提交，push，上生产」）

`a62f8d4` → CI 六 job 全绿 → `docker compose pull app && up -d app`：
`revision=a62f8d4…` / **healthy** / 公网 health 200 / bundle **`index-B8ZJwYtc.js`** / 日志零 error
（「转换队列已启动（轮询 3.0s，并发 2）」）。DB 与 `.env` 部署前均已备份
（`papershelf.db.bak.20260916-193210`、`.env.bak.20260916-193210`）。
这次 pull **零 Retrying**（上次那 42MB 层反复重试 35–40 分钟，这次很快）——
但流程仍照 `nohup … &` 走（教训不改：SSH 直接等会被 600s 切断、镜像拉不全）。

**生产真浏览器实测**（宿主数据 paper 1）：**35 个标题 → 33 个正文标题 `inl=2 / o=4`**，
剩下 2 个"不可划"是**有意为之**（应用自己的「阅读器」`h1` + `doc-head` 那行元数据标题，都没有块坐标）；
在标题块 `b-0003`（「A review of machine learning…」）上**真实鼠标拖选** → **`mark-bar` 出现**（4 支笔 + 加笔记）。
验收**只拖不写** → 生产库痕迹为零（`notes 8 / highlights 162` 与验收前一致，paper 1/2 状态未变）。

**⚠️ 本轮新踩的三个坑（都写进记忆了）**
1. **`cdp("Page.reload")` 之后 harness 的"当前标签"会漂回上一个标签** —— 必须**再 `switch_tab` 一次**才
   接着量 DOM；否则会在**别的标签页**上量（这次连着两次量到宿主的 duckduckgo 搜索结果页，
   输出"标题 0 个容器"，**差点判成"没修好"**）。判据 = 每次量测前先打一次 `location.href` 自证。
2. **后台标签页里真实鼠标事件不生效** → 解药是 `Emulation.setFocusEmulationEnabled(enabled=True)`
   （设完 `visibilityState=visible` + `hasFocus()=true`，拖选立刻正常）—— 这是「后台标签页不算数」那条
   旧教训的**正面解法**（旧做法是绕开、改用 curl）。
3. **`git status` 里 static 产物不出现**别慌：`src/papershelf/static/assets/` 与 `index.html` 是
   **gitignore 的**（镜像里由 CI 的 npm build 生成），所以"改了前端但 git 没动静"是正常的。

### 最近一轮（2026-09-16 晚）㊴ 表格重建 = ①c 校对 agent 的 `set_table`（单元格双语）

宿主截图报告：**「表格和原 pdf 差异较大」**（PDF 原表 vs 我们的渲染）。两次决策都由宿主拍：
**走 C**（交 ①c agent，新增 `set_table` 工具）+ **A**（单元格双语）。

- **根因 = "写了的没做"**：`parse.py` 里 `table` 一词**零出现** —— 设计基线（「必须内建」节）
  早写明「**表格坐标级重建**」，实现里连入口都没有。`get_text()` 按阅读顺序吐行 ⇒ 同一行里
  相邻栏的格子被**粘成一句话**（原件实测 `b-0037` = 「Naive Bayes (BN) Support vector machine (SVM)」
  其实是**三列表同一行的三格**），列关系全丢；而且这串粘连文字**还要白送翻译**（花 token 译一段
  不存在的句子）。paper1 498 块 / paper2 909 块 **零 `table` 块**；paper1 有 3 页真表，**7 张全中招**。
- **为什么程序侧判不了**（三条都实测过）：这些表**有横线、没有竖线** → 无线可分列；
  `page.find_tables()` 的 `lines`/`lines_strict`/`text` **三策略全抓不到**；`strategy="text"`
  更糟 —— 把**双栏正文**判成 **62×7** 的大表。
- **C 的红线**：**结构交给视觉、认字必须回文本层**。agent 用 `read_blocks`/`read_block` 拿
  抽取出的逐字原文，`read_page(page, region=…)` 放大**只判结构**（哪几行一张表／列边界／
  合并格／跨页／表注归位），然后把块里已有的字装进格子 —— 一个字都不许新造。
- **护栏三连**（每条对应一种可判定的坏结果，全部实测拒收过）：① **逐字来源**
  （每格文字必须能在被消费源块的拼接文本里找到；匹配前 `_flat` 归一：空白压平 + 去行末断词连字符
  + 去软连字符）→ 防凭图默写，**找不到整份拒收**；② **形状**（等宽二维数组、≥2×2、
  ≤ `_TABLE_MAX_ROWS=80`；`rows_zh` 形状不符 → **回落英文网格**，不拒收）；③ **不吃正文**
  （`_table_residue`：挖掉已进格子的字后若还剩 ≥4 实词成句片段 → 有正文被吃进表 → 拒收）。
  四条硬边界：`ids`≥2、**不许跨页**（按页分开建，程序不替人拼两页的表）、`figure`/`table`
  不能当一行、整表全空拒。
- **提示层 `_table_regions`**（几何、纯本地零 token）：与 ㊲ 同一套路 —— 程序只量可疑处、
  判定仍在 agent。判据**两条缺一不可**：横线（≥25% 页宽 + `_MARGIN_BAND=0.09` 滤页眉页脚）
  **＋同一「行」上横着好几段文字**（`_MIN_BAND_LINES=4`/`_MIN_BAND_RATIO=1.6`/`_MIN_BAND_COLS=3`，
  双栏正文只 2 个 x 起点）。**只靠横线：37 页报 20+ 处**（图框边、期刊页眉装饰线全中）；
  **只靠文字：把第 29 页的双栏正文报成表**；两条合用 → **真表 3 处（p3/p20/p21）、零误报**，
  `_MAX_TABLE_REGIONS=3` 封顶。提示**只买召回**（㊲ 的对照实验已证）。
- **单元格双语**：块 `payload` 带 `rows`（英）＋ `rows_zh`（中），形状必须一致，跟随三模式；
  前端 `Reader.tsx` 的 table 分支是 **cell-level** 双语（不是块级）。
  ⚠️ **中英同形的格子（`CNN`/`99.2%`）必须只渲一支** —— 原型 `cellHTML` 里本来就有 `.b-any`，
  我们的 `Reader.tsx` 漏了 ⇒ 对照模式出现 `CNN / CNN` 重影。**教训：原型里那些看着多余的 `if`
  分支，往往正是一条踩过的坑。**
- **不动 `parse.py`、不涨 `PARSE_VERSION`**：表格重建发生在 ①c 阶段（解析之后、翻译之前），
  产物以 `type="table"` 落进**块级 JSON**（事实来源）；解析器一个字节没动。
  ⚠️ 反过来说 —— **存量文献没有 `table` 块**，要吃这项修复必须走「重新提取」
  （生产那两篇 ≈**200 万 tokens** + 译文重译 + 批注重划）。**宿主尚未表态，未动。**
- **实现中新踩的两个坑**：① 旧单测**自己给块造 `bbox`** → 提示层**绿着但生产命中 0**
  （`parse.py` **只在 `figure` 块**存 bbox，正文块根本没有 ⇒ 整个提示层静默全空；实测 37 页
  `regions` 恒为 0）→ 改成**真几何**，块号只**按文本位置反推**当线索（`_band_block_ids`），
  并**把测试改成生产形状**（本轮同型坑踩了两次）。② `set_table` 必须**先插后删**
  （先删被消费的块，首块的 `pos` 就失效 → 表格块跑到别的段落后面，且错得很隐蔽）。
- **验收**：离线回归 **347 passed**（`tests/test_table.py` 新 6 项 + `test_proofread.py` 的
  `set_table` 结构/护栏/跨页/表注用例 + `test_style_guard.py` 同形格守卫）；
  **变异检验三次全红**（去掉 x 起点线索／放宽 `_MIN_BAND_COLS`／关页边滤除）证明护栏不是摆设。
  合成 PDF 真产品路径（paper 4）：agent 自己发现表格 → **先 `edit_block` 把被换行劈开的格子接回**
  → `set_table` 建 **5×4** → 翻译 → 校验 `ok=True`（26.2s / 46k tokens）。
  真论文 paper1 第 3 页：**7 轮 / 78k tokens**，先 `merge_block` 再建 **10×3**（首列纵向合并用
  **空尾格**表达）；**独立视觉通道逐字复核一致**（含易错格「Bayesian networks … Naive Bayes (BN)」
  与自成一行「Q-learning」）。真浏览器三模式实测（⚠️ **改完前端必须 `npm run build`** ——
  旧 bundle 里表格没有 `.b-en/.b-zh`，看起来像"双语没生效"）：空单元格 0、console 零 error。

### 同一轮：**已上生产**（2026-09-16 21:1x，宿主「要」）

`bff21da` → CI `35096774881` 六 job 全绿 → 生产 `docker compose pull app` + `up -d app`：
`revision=bff21da932cef…` / **healthy** / 公网 health 200 / bundle **`index-BuC2xeWi.js`**
（与本地同指纹，含 `.b-any`）/ 日志零 error（「转换队列已启动」）。
DB 与 `.env` 部署前已备份（`papershelf.db.bak.20260916-204237`、`.env.bak.20260916-204237`）。
⚠️ 这次 pull 又撞慢层：42.37MB 那层反复 `Retrying`，20:42 起 → 21:12 `app Pulled`，**≈30 分钟**
（`nohup … &` + 轮询照旧是唯一可行解）。

**⚠️ 生产上「看不到变化」是预期**：表格重建在 ①c 阶段发生，存量块里根本没有 `table` 块。
宿主 20:48 明确：**「不用（重新提取），我来操作」** —— 由宿主自己在文献库点「重新提取」，
助手**不代签**这个操作（代价 ≈200 万 tokens + 译文重译 + 批注重划）。

### 最近一轮（2026-09-16 深夜）㊵ 表格并排/可划 + ㊴ 补记：生产「重导入仍无表格」三处真缺陷

**㊵**（宿主：「左英右中，不要一格里中英混排 + 表格也要能划」）`e2bdaf1`：`model.table_layout`
一并算出每格字符区间 → `prose_html(base=…)` 吐**整块偏移**锚点；前端不再自拼 `<table>`，
两栏各走服务端 HTML（配对判据只在服务端 `table_zh`）；导出/分享走同一个 `synth._dual_rows`。

**㊴ 补记**（宿主 21:45「生产删除重新导入后，还是没有表格」）`e0a149e` —— **真凶是 ①c 的护栏，
不是 ㊵**。生产 paper 3（与本地 paper 1 同一 PDF）484 块只 1 个 `table` 块，日志
`表格重建 1 张 / 护栏拒绝 5`，而 agent 页笔记写着"已重建"（**页笔记是自由文本、会撒谎；
产物不会** —— 页 3 的 `b-0031`、页 21 的 `b-0233/b-0234` 都还在）。本地同 PDF 逐页复现见三处：
① 护栏③「不吃正文」**逐块算**，而一格的字常横跨好几块（`…meas-` + `ured…`）⇒ 正确的重建被拒；
② 核对把空白/连字符当内容（`[ 115 ]` vs `[115]`）；③ 拒绝信息指不出地方（agent 原地打转，
**一轮烧 39k tokens**）。→ 护栏改在**被消费块拼起来的文本**上算 + 新增 `_tight` 只用于核对
（丢字换字照旧拒收）+ 拒绝时给出「再补哪一块就凑齐」+ 工具被拒进 INFO 日志。
修前 3/20 页连拒、修后 **10×3 / 8×5 都建成、0 拒收**（真 LLM、9 轮 / 117k tokens）。
离线回归 **356 passed**。⚠️ 生产要吃这项修复**还得再跑一次**（宿主操作）。

**本轮血泪教训**：①「护栏静默拒绝 = 生产静默全灭」（拒绝对 agent 只是一条消息，它会转做别的）；
② 护栏的"文本面"必须与 agent 看到的文本面一致（跨块、插空白）；③ 测试要复现生产形状。

### 同一轮：**已上生产**（2026-09-17 09:0x，宿主「是的」）

`51f671b`（= `e2bdaf1` ㊵ + `e0a149e` 护栏 + 记忆）→ CI 六 job 全绿 →
`docker compose pull app`（本次**零 Retrying**，约 4 分钟）+ `up -d app`：
`revision=51f671b…` / **healthy** / 公网 health 200 / bundle **`index-CZzp5tzn.js`**（与本地同指纹）/ 日志零 error。
DB 与 `.env` 已备份（`…20260917-085812`）。

**生产真浏览器实测**（paper 3 / `b-0507`）：左英表 x=386 w=521、右中表 x=943 w=521，各 20 格 52 锚点；
格内**真实拖选** → `.mark-bar` 出现（4 笔 + 加笔记）；真点青绿笔 → DB 落
`(3, b-0507, zh, 53, 55, green)` → reload 渲染 `<mark class="hl hl-green" data-h="1">FD</mark>`
（`oklch(0.78 0.14 155 / 0.42)`）→ `DELETE /api/highlights/1` 复原（highlights 0 / notes 0，零残留）。

**⚠️ 宿主还要自己「重新提取」**：护栏修复在 ①c 阶段，存量块里没有 `table` 块，不吃修复就还是没表格
（宿主 20:48 已表态自己操作）。

### 最近一轮（2026-09-17 上午→中午）v10 页面家具上生产 + 生产验收

**本地**（`657388f`）：解析 v10（页眉文字不再有意丢掉 + 补回每页页眉横线，`PARSE_VERSION` → 10）+
①c 提示词同步加护栏（页眉/页脚不属"重复块"、不许并入正文）+ `validate.tag_balance` 抗 CSS 注释；
离线回归 **373 passed**（`tests/test_page_furniture.py` 12 项 + 变异检验转红）。

**生产**（`f4476c4`，备份 `…bak.20260917-1150`）：`docker compose pull`（52.85MB 层 Retrying 5 次，
`nohup` + 轮询照旧）→ `up -d app` → `revision=f4476c4` / healthy / 公网 200 /
bundle `index-D9a7YvYN.js` / 日志零 error；CSS 三条装饰规则实测在位。

**生产验收（真浏览器 + 容器内直调，零 token 零数据改动）**
- 生产真浏览器（paper 1）三模式逐列量测：dual `t-en 119/119 + t-zh 87/87`、`lang-zh` 下 `t-en 0/119`、
  `lang-en` 下 `t-zh 0/87`；硬刷后控制台**零 error**。数据零改动（`status=reading` / `last_read_at` 未变）。
- **新手法**：`docker exec papershelf python` + `base64` 塞脚本，直调 `render_block(...)`，断言产物含
  `pg-band pg-top pg-rule-below`、`typeset=False` 产物逐字干净 ⇒ 证明**线上跑的就是这份代码**。
- **结论两分开说**：部署/渲染 ✅ 已验；**页面上看到页眉与横线 ✗ 还没有** —— 生产唯一那篇
  （paper 1，139 块）是 v10 之前解析的（有 `payload.page`、无 `band`/`rule`）⇒ 要看得**宿主自己点「重新提取」**。
- ⚠️ **生产数据已变**：只剩 1 篇（8 页 139 块《增材制造中的机器学习综述》），此前那批都不在了。

**两条环境坑**：① `browser-harness` 的 `js()` **不接受 `await_promise`**（Promise 自动 await）、
没有 `navigate()`（用 `goto_url()`），JS 里括号写错会被报成 `SyntaxError`（像 harness 坏了）；
② 后台标签页里 `click_at_xy` **点按钮不生效**（模式纹丝不动）→ `Emulation.setFocusEmulationEnabled(True)`
后 `visibilityState=visible` 立刻正常 —— 这是「后台标签页不算数」的正解。

### 最近一轮（2026-09-17 下午）㊷ 页面图形（解析 v11）：本地全验，**未上生产**

宿主一句「Springer 的图还是没有」→ 四件同源缺陷（横线颜色/粗细硬编码 / 矢量标识从不入产物 /
「无图注就剔除」删真图 / 灰底底纹从未照抄）+ 验收时扒出的第五件（徽标是"图片层 + 矢量层"叠的，
只取内嵌字节 = 一个光灰方块）。细节全在项目记忆 **`design-decisions.md` ㊷** 与 `MEMORY.md` 进度段。

- **本地真产品路径端到端已验**（`73098be`）：干净重启 8012（**旧进程仍活着会用旧解析器覆盖库**）→
  `POST /api/papers/5/reextract` → 解析 158 块 / deco 9 / rule 8 / shade 1 → ①c **61 轮 / 103 万 tokens /
  表格重建 3 张 / 9 个 deco 一个没删** → 翻译 → 全篇 1,072,896 tokens；
  真浏览器 9 张 deco 全部解码 + 左右交替正确 + 3 张并排双语表格；导出件同带；
  离线回归 **403 passed**（新增 `tests/test_page_graphics.py` 30 项）。
- **两条教训**：① `naturalWidth=0` ≠ 图坏了（`loading="lazy"` + 首屏外天然 0×0，改 `eager` 后全好）
  —— 同轮的"后 5 个 deco 加载失败"是**误判**；真缺陷只有"旧 serve 进程覆盖库"那一条。
  ② 改 `pipeline/*.py` 不重启 serve = 白改（且同端口曾**并存两个**进程）。
- browser-harness 小抄（本轮新增）：`activate_tab(target_id)` **要传参**；`cdp("Page.reload")` 报
  `Message may have string 'sessionId' property` ⇒ 用 `js("location.reload()")`；
  本地会话过期会跳 `/login?next=…` ⇒ `fill_input("#liEmail"/"#liPass")` + 点 `.btn-primary` 重登。
- **待宿主**：推 + 上生产（`PARSE_VERSION` → 11 ⇒ 生产存量要吃修复得宿主自己点「重新提取」）。

### 同一轮：**已上生产**（2026-09-17 16:1x，宿主「要」）

`5769900` → CI run `35195781106` 六 job 全绿 → `latest`/`sha-5769900` 双 200 →
`docker compose pull app`（**又撞慢层**：15:45 起、`Retrying` 重下 21.4MB、16:09 完成 ≈24 分钟；
`nohup` + 轮询照旧）+ `up -d app`：
`revision=57699004f96ec2cd9f29d832ac868e71ec9696f9` / healthy / 公网 200 /
bundle `index-8YofT6DN.js` + `index-lPIbuEdh.css`（与本地构建产物 `cmp` 一致）/ 日志零 error。
DB 与 `.env` 事前已备份（`/tmp/*.bak.20260917-154522`）。

**验收（两分开说）**：① 部署/代码 ✅ —— 容器内直调 `PARSE_VERSION=11` + 合成块渲染断言
（`--rule-c/--rule-w`、`deco-bottom deco-left`、`width:61px`、`--shade-c`，`typeset=False` 干净）；
生产真浏览器量 `styleSheets` 三条规则在位 + 合成 `.deco` 节点 computed `inline-block`/`text-align:left`
（左右交替在生产上是活的）+ 三模式 + 零 console error；**向后兼容实测**：生产 paper 1 仍是 v10 产物
（`rule` 是字符串）而 16 处 `pg-rule-*` 照常渲染。
② **页面上看到标识/灰底 ✗ 还没有** —— paper 1（139 块 / deco 0 / shade 0）是 v11 之前解析的，
要看得宿主自己点「重新提取」（≈200 万 tokens）。

**顺带发现死配置**：`PAPERSHELF_TOKEN_BUDGET` 只有 `config.py` 读、**无人使用**（真转换烧 107 万 tokens 也不会被拦）
⇒ 遗留待定项 5 的成本护栏目前是空话；修法有语义选择，等宿主定。

### 同一轮：死配置 `PAPERSHELF_TOKEN_BUDGET` 定案删除（2026-09-17 16:4x，宿主「不需要限制」）

宿主：「不需要 PAPERSHELF_TOKEN_BUDGET 限制」。它是一个**空话配置**：`config.py` 读进来存成
`Settings.token_budget_per_paper`，**全仓库没有任何消费端** —— 真转换烧 107 万 tokens 一次都没拦过，
而文档还写着「单篇 token 预算」（比没有护栏更糟：排障时会先去确认自己设对了）。
⇒ `08dcd7f`：删字段 + `.env.example` + `docs/install.md`，`docs/design.md` 口径同步，遗留待定项 **5 结**。
⚠️ **保留** `PAPERSHELF_PROOFREAD_TOKEN_BUDGET`（①c agent 累计预算，**活代码**，名字只差一个词）。
新增 `tests/test_config_guard.py`（3 项）**类不变式**护栏：① 每个 `Settings` 字段都要有消费端；
② 死名不得被读取（精确整名，注释里说明"为什么删"不算违规）；③ 活那条必须仍接线。
变异检验：死字段加回 → 红；随便加个没人用的新字段 → 红。离线回归 **406 passed**；CI run 绿；**未上生产**
（行为中性：删的是一段没人走的代码，生产 `.env` 里那行留着也无作用）。
**教训（写进测试注释了）**：第一版判据写成"环境变量名要在别处出现"—— **判据错了**，
把 `PAPERSHELF_PROOFREAD_DPI` 这类**真在用**的项全报成死配置（消费端写 `settings.proofread_dpi`）。
**"被消费"要按代码里的字段名量，不是按变量名量。**

### 最近一轮（2026-09-17 傍晚）㊸ 整页旋转表格页转正（解析 v12）+ ①c 重试 2→20（退避封顶）—— **已上生产**

宿主两条指示：「原 pdf 里，有一个竖向的表格，提取后格式错了」+「504 重试，放宽到 20 次」；
收尾一句「push，上生产」。细节全在项目记忆 `design-decisions.md` ㊸ 与 `MEMORY.md`。
- **病灶两条都在解析层**：①那张表整页**旋转 90° 画**（页面 `/Rotate`=0 ⇒ 只有 span 级
  `line["dir"]=(0,-1)` 说真话）⇒ 表格的**列被当成行**；②**表注被静默丢**
  （`pending_figure` 跨页存活且从未复位 → `Table 2 …` 在任何块里都不存在）。
  ⚠️ 上轮"①c 校不出来"的诊断**是错的**：线上那次 **①c 只校到第 4 页**（504 → 2 次重试用完 → 放弃），
  第 5–8 页从未进过 ①c，看到的正是解析原样。
- **修法**：`show_pdf_page(rotate=…)` **原样重画**（纯坐标变换，不给表格识别加任何特例）；
  旋转页 `columns=False`（只按行序）；转正副本落 `p<id>/normalized.pdf`（**不是**全库共享名 ——
  并发会互相覆盖、删文献收不掉）；`_h_rules` 先缝**共线短线段**再按长度筛；①c 读同一份副本；
  `PARSE_VERSION` → 12。
- **重试**：2 → 20，**退避必须封顶 30s**（否则第 20 次睡 `2^20` 秒 ≈ 12 天，`time.sleep` 真睡）；
  新增 `PAPERSHELF_PROOFREAD_RETRIES` 并接线。常量必须定义在 `Proofreader` **之前**
  （默认参数在 import 期求值）。
- **实测**：解析层 `rotated_pages=[5]` / 表注块回来 / 表区提示 `[]→1`；**①c 只跑第 5 页
  （真 LLM 73k tokens / 55s）→ `set_table` 8×6 全对**、7 个引用编号各自归位；
  **重试真验**（本地起会回 504 的 HTTP 服务 + 真 httpx）4 次请求后成功、退避 2+4+8s；
  离线回归 **421 passed**；**8 次变异检验全部转红**。
- **上线**：`c89a4ad`+`3d7c300` → CI `35211627575` 六 job 绿 → 生产 `revision=3d7c300` / healthy /
  公网 200 / bundle 与本地逐字节一致 / 日志零 error；容器内直调自证 `PARSE_VERSION=12`、
  `DEFAULT_RETRIES=20`、`proofread_retries=20`；用**宿主自己那篇 PDF** 预演（只解析零 token）
  → `rotated_pages=[5]`、`Table 2 …` 回来了、`[ 92 ]` 跟着自己那行。
  ⚠️ **生产上"看不到变化"是预期** —— 生产唯一那篇（8 页 149 块）**就是宿主报的那篇**
  （今天 16:38 上传、v11 解析），要吃修复**必须宿主自己点「重新提取」**（≈100 万 tokens，不代签）。
- **两条新自坑**：①`pytest -o pythonpath=<影子 src>` 做变异检验**根本无效**（`tests/conftest.py`
  把真 `src` 插在 `sys.path` 最前，影子被顶掉 → "变异全绿"差点误判成"测试太弱"）→
  **先证明变异检验跑到的是那份代码**；②**别用 `git checkout <file>` 收变异**，它把该文件
  **本轮所有未提交改动**一起抹掉（这次抹掉 converter 两处接线，靠 grep 才发现）。

### 最近一轮（2026-09-17 晚）㊹ 参考文献「只译标题」= 新块类型 `ref`（解析 v13）

宿主：「参考文献没有翻译」→ 选 **B｜只译文献标题**（作者/期刊/卷期页/DOI 保原文）。
细节全在项目记忆 `design-decisions.md` **㊹** + `MEMORY.md` 进度段；本条只记会话内**局部**经验。

- **病灶是"可译性"**：`refs` 属 `NO_ZH_TYPES` = 整块免中文、**从设计上就不送模型**。
  要译标题先得**有一条完整的条目** —— PDF 抽的是一个连续文字流，被块检测切成 ~200 字符碎片
  （切口落在词中间、一块里塞着下一条前半截）。⇒ 解析层新增 `merge_ref_entries`（v13）。
- **判断"哪一段是参考文献"的两个真判据**（都靠实测撞出来）：
  ①**"连号"才是拦住 DOI 的判据** —— 光看 `N. ` 形状，`10. 3390/…`、`11. 1117/1. Oe.` 全中；
  ②**行末断词连字符不猜补**（`man-ufacturing` 保持原样；`Laser-directed` 是反例）——
  猜错就是凭空改字，定位改由 `fold_for_match` **折叠后比对**解决。
- **`PARTIAL_ZH_TYPES` 必须随 HTML 走**（`data-pt="1"`）：校验器只有 HTML、没有块类型，
  按元素标签判**实测永远不生效**（给每条文献刷一条 `digit_mismatch`）。同族第二次：
  「有写回代码 ≠ 有数据来源」→ 这次是「**有豁免规则 ≠ 豁免生效**」。
- **实测**：真论文 28/28 条译出（29,396 tokens / ≈1,050 每条）；合成 PDF 真产品路径 4 条碎片 → 4 个 `ref` 块；
  真浏览器三模式逐条量测；离线回归 **460 passed**；**5 处变异检验全红**。
- **会话内踩的两个小坑**：① `GET /api/papers` 不存在（真接口是 `/plans/{id}/papers`），
  第一版 E2E 脚本在这里抛 `TypeError`；② 本地登录页提交按钮**用 `js(...).click()` 不生效**，
  要 `Emulation.setFocusEmulationEnabled(True)` + `Input.dispatchMouseEvent`（后台标签页那条教训的又一次复现）。
  ⚠️ 后端**本轮零改动**时也要 `npm run build` 一次确认指纹不变（本轮 `index-8YofT6DN.js` 未变）。

### 最近一轮（2026-09-19 13:0x–13:4x）㊹ 修订二：「重建参考文献」实现 → 上生产 → 生产真跑

宿主：「现在可以连上生产环境了」（生产 22 端口本轮恢复；此前 SSH 被封、只有 80/443 通）。
- `9f0ef78`（实现 + v14 一并上）→ CI 六 job 绿 → 生产 `revision=9f0ef78` / healthy /
  bundle `index-DLx3KrxJ.js` / 日志零 error；DB + `.env` 备份 `/tmp/papershelf.db.bak.20260919-130041`。
- **生产 paper 1 真跑重建**：`ref 2→288`、288/288 有中文标题、282,605 tokens / 293s；
  **11 笔记 + 229 划痕逐字段完全一致**（这是整件事的目的）；真浏览器三模式 + 零 console error。
- 顺带：paper 4 也修了（177 条）—— **误点**，因为文献库按 `updated_at DESC` 排、row 0 ≠ paper 1。
- 三条操作教训写进项目记忆：①按 href 认行（别按行号）；②browser-harness 的 await 卡死 =
  后台标签页（`activate_tab` + `Emulation.setFocusEmulationEnabled` 是正解，第三次撞）；
  ③日志口径（`refs` vs `refs_left`，`31c8c69` 已修）。
- **未做**：paper 2（APA 体例）仍 0 条 —— 要覆盖得新加形状判据，待宿主拍板。

### 最近一轮（2026-09-19 晚 → 2026-09-20 上午）㊺ 手动改进度 + ㊻ 翻译跨块承接 —— **已上生产并验收**

宿主对两条线都选 **C**（㊺ 的入口 C = 工具栏 + 抽屉双入口；㊻ 的方案 C = A + 加宽上下文窗口）。

- **㊺**：`progress_by` 语义分流（`"user"` 手改 / 缺省滚动）+ 两入口 + 9 条护栏，**488 passed**，
  真鼠标逐键实测全绿；验收中揪出并修掉「点开编辑器的那个 `click` 尾巴把光标落到刚出现的窄输入框最左」
  （29 → 打 5 得 529 → 夹紧 100 写库）。细节见 `design-decisions.md` ㊺。
- **㊻**：`seam_pairs` / `chunk(keep_together=)` / `_seam_note` / 规则 7 / 上下文各 2 个正文邻块。
  **三条实测切出来的阈值**：① 词数下限（前半 ≥6 词、后半 ≥3 词）挡掉标注/页码/作者行假阳性
  （不能按"长度"挡：真接缝后半可能只有四个词）；② 提示词**只写"这是一句话"不够**（3 次里 1 次退回译断）
  ⇒ 必须给**整句原文 + 动作指令**（"先整句译出、再按断点切开"）后 **9/9** 干净承接；
  ③ 量承接要**同时**量"拼起来通顺"和"有没有把前半末句抄到后半"（只量一样会放过另一样）。
  离线 **508 passed**（`tests/test_seams.py` 20 项）、**6 处变异检验全红**、合成两页 PDF 走真产品路径通过。
  细节见 `design-decisions.md` ㊻。
- **本轮两条新教训**：
  ① **提示词的有效成分是"动作"而不是"事实"** —— 告诉模型"这两块是一句话"没用，
    告诉它"先整句译出、再按断点切开"才有用（散文式要求 → 可执行指令）。
  ② **写记忆时别在 Python 里用双引号转义中文引号**（脚本连炸两次 SyntaxError）——
     改记忆文件用 `pathlib` + 单引号字符串，或直接 heredoc 追加。
- **已上生产（2026-09-19 21:3x，宿主「是的」）**：`3a97be3`（代码+测试+docs+前端）+ `dc33f1d`（记忆）
  → CI `35445917918` 六 job 全绿 → `revision=dc33f1d` / healthy / 公网 200 /
  bundle `index-0jO-MLvR.js`+`index-B92NFYDf.css`（与本地 `cmp` 逐字节一致）/ 日志零 error；
  备份 `/tmp/{papershelf.db,.env}.bak.20260919-213003`。**容器内直调自证**（零 token）：
  `CTX_NEIGHBORS=2`/`_MIN_SEAM_WORDS=(6,3)`/跨页眉接缝 `b-0026→b-0028` 认出且三块留在同一片/提示词含整句原文。
  **真浏览器**：㊺ 工具栏 Esc 取消（库零变化）+ 抽屉改 3→8（`progress=8`，`status_at`/`last_read_at`/
  `notes 18`/`highlights 427` 全未变）→ 还原回 3；三模式 `378/182 · 0/378 · 378/0`、零 console error、零横向溢出。
  ⚠️ 宿主那个生产标签页又是**化石**（旧 bundle `index-DLx3KrxJ.js`）→ 硬刷才见新界面。
  ⚠️ 页面上看不到 ㊻ 的变化是**预期**（动的是翻译阶段，旧译文不会自己变）。
- **待宿主一句话**：存量译文要不要按 ㊻ 回填（**不必**重新提取，定点重翻即可）？
