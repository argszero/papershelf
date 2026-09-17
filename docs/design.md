# papershelf 技术设计

> 需求依据：`.emrg/memory/design-decisions.md`（决策总表 ①–㉒，**唯一事实来源**）
> 本文档覆盖「怎么搭」。按决策 ㉒，**写到「转换管线」即开始实现**，用真实产物反过来校正本文档。

---

## 1. 一句话定位

自托管的文献精读工作台：**丢进 PDF（或 arXiv 链接）→ 自动产出带块标记的英文 HTML 与对应中文 HTML → 左右并排双向精读 → 笔记 / 进度 / 只读分享**。

成败标准只有一条：**转换质量 + 双语配对正确性**（决策 ②③④⑥⑯ 全押在这里）。应用外壳是确定性劳动，可后置。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│ 前端 SPA (决策⑨)                                              │
│  总览 / 文献库 / 进度看板 / 阅读计划 / 分享管理 (㉖)            │
│  导入抽屉 / 只读分享（同站只读化）/ 最小管理页 (㉑)             │
│  ⚠️ 侧栏**没有**独立「阅读器」条目：阅读器是文献库的下级页       │
│     （站在阅读器里时高亮仍落在「文献库」，与原型一致，㉚）        │
└───────────────┬─────────────────────────────────────────────┘
                │ REST JSON  (/api/*)
┌───────────────▼─────────────────────────────────────────────┐
│ FastAPI 单体 (决策⑨)                                          │
│  · auth（注册/登录/会话）        · plans / papers / notes      │
│  · docs（块级 JSON 读改）         · share（实时只读 + 撤销）    │
│  · admin（用户管理，㉑）          · files（PDF/图片静态服务）   │
│  · 任务投递（入队）                                            │
└───────┬──────────────────────────────┬──────────────────────┘
        │ SQLite (决策⑨)                │ 任务表 / 队列
┌───────▼──────────┐          ┌────────▼───────────────────────┐
│  SQLite          │          │ 转换 Worker（独立进程，同栈）    │
│  users/plans/    │◄─────────┤  管线见 §5（fitz + LLM）        │
│  papers/docs/... │          └────────┬───────────────────────┘
└──────────────────┘                   │ OpenAI 兼容协议 (决策⑧)
                             ┌─────────▼──────────┐
                             │ LLM 服务商          │
                             └────────────────────┘
```

- **运行时**：Python 3.13（宿主环境）+ uvicorn；worker 与 API **同代码库、同容器**，以独立进程启动
- **存储**：SQLite（元数据 + 块级 JSON）+ 文件目录（原始 PDF、图片资源）
- **部署**：单容器，`docker compose up`；数据目录挂载持久化

---

## 3. 数据模型（SQLite）

归属链固定 **User → Plan → Paper → Doc**（决策①⑦，无 Team/Org）。

### 3.1 表设计

```sql
-- 用户（决策⑭⑮）
users(
  id            INTEGER PRIMARY KEY,
  email         TEXT UNIQUE NOT NULL,      -- 须匹配白名单（默认 *.edu.cn）
  password_hash TEXT NOT NULL,             -- bcrypt/argon2
  display_name  TEXT,
  status        TEXT NOT NULL,             -- active | disabled（⑭ 改成验证码后注册即激活，不再有 pending）
  is_admin      INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL,
  activated_at  TEXT
)

-- 会话（决策⑨：签名 cookie session）
sessions(
  token     TEXT PRIMARY KEY,
  user_id   INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
)

-- 阅读计划（一等公民，决策⑦ 的归属载体）
plans(
  id          INTEGER PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  name        TEXT NOT NULL,
  goal        INTEGER,                     -- 目标篇数
  description TEXT,
  created_at  TEXT, updated_at TEXT
)
-- 计划设置（⑫ 术语表、进度语义 ⑰ 等）：1:1 侧表，避免主表膨胀
plan_settings(
  plan_id   INTEGER PRIMARY KEY REFERENCES plans(id),
  glossary  TEXT,                          -- JSON: [{en, zh, note}]
  UNIQUE(plan_id)
)

-- 文献（计划私有副本，决策⑦）
papers(
  id          INTEGER PRIMARY KEY,
  plan_id     INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  title       TEXT, authors TEXT, venue TEXT, year INTEGER,
  tags        TEXT,                        -- JSON 数组，自由标签（⑲）
  source      TEXT NOT NULL,               -- upload | arxiv
  source_ref  TEXT,                        -- arXiv ID / 原文件名
  pdf_path    TEXT,                        -- 原始 PDF 落盘路径
  arxiv_html  INTEGER DEFAULT 0,           -- 是否走 arXiv HTML 直达（⑱）
  status      TEXT NOT NULL DEFAULT 'unread',   -- unread|reading|read|reviewed (⑰)
  status_at   TEXT,                        -- 状态变更时间戳（⑰ 节奏图数据源）
                                           -- ⚠️ 自动翻「在读」时**只在翻转那一刻**盖一次（㊱）
  progress    INTEGER NOT NULL DEFAULT 0,  -- 0-100（⑰ 滚动自动；标记已读→100）
  progress_mode TEXT DEFAULT 'auto',       -- auto（滚动）| manual（已读锁定）
  last_read_at TEXT,                       -- 「最近阅读」（㊱）：**只有滚动上报写它**
                                           -- ⚠️ 别拿 updated_at 代替：那个被任何 PATCH 刷新
  conv_state  TEXT NOT NULL DEFAULT 'none',-- none|queued|doing|done|failed (②)
  conv_error  TEXT,
  conv_attempts INTEGER DEFAULT 0,
  created_at  TEXT, updated_at TEXT
)
CREATE INDEX idx_papers_plan ON papers(plan_id);

-- 文档（块级 JSON 为单一事实来源，决策⑳）
docs(
  paper_id   INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  meta       TEXT,          -- JSON: {zh_title,title_en,authors,affil,journal,doi,pages...}
  assets     TEXT,          -- JSON: [{name, path, width, height}]
  block_count INTEGER,
  version    INTEGER DEFAULT 1,
  created_at TEXT, updated_at TEXT
)

-- 块（决策④ 标记 / ⑳ JSON 单一事实来源 / ⑯ 块级修订）
blocks(
  id         TEXT PRIMARY KEY,   -- 稳定块 ID，如 'b-0042'（贯穿标记/渲染/笔记/修订）
  paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  ord        INTEGER NOT NULL,   -- 顺序
  type       TEXT NOT NULL,      -- h1..h4 | p | figure | table | eq | abstract | meta
  level      INTEGER,            -- 标题层级
  section    TEXT,               -- 所属章节（大纲/锚点用）
  en         TEXT,               -- 英文原文（标记穿透的源）
  zh         TEXT,               -- 中文译文
  zh_source  TEXT DEFAULT 'mt',  -- mt（机器译）| human（人工改，⑯）| none
  payload    TEXT                -- JSON：图/表/公式额外字段（src, caption, latex, number...）
)
CREATE INDEX idx_blocks_paper ON blocks(paper_id, ord);
-- ⚠️ 待定项 12：重跑转换时 zh_source='human' 的块不得被覆盖

-- 笔记（⑯ 块级锚点；㉛ 可再锚到**任意字符区间**）
notes(
  id        INTEGER PRIMARY KEY,
  paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  block_id  TEXT,                 -- 锚定块（可为空＝文献级笔记）
  lang      TEXT,                 -- ㉛ 'en' | 'zh'（区间在**哪一侧**的坐标系里）
  start     INTEGER,              -- ㉛ 区间起点（裸文本字符偏移，0 起、闭开）
  end       INTEGER,              -- ㉛ 区间终点
  hl_id     INTEGER,              -- ㉛ 这条笔记写在哪道划痕上（划痕被擦掉时置 NULL，
                                  --     笔记**不跟着消失** —— 它有自己的坐标）
  sid       TEXT,                 -- ㉚ 遗留列：句锚点已退役，仅旧数据迁移期可能非空
  quote     TEXT,                 -- 该段文字的原文快照，卡片直接显示，不再回查文档
  content   TEXT NOT NULL,
  created_at TEXT
)

-- 划痕（㉛）：高亮的单位 = **任意字符区间 + 一支颜色笔**。
-- 坐标是块**裸文本**（blocks.en / blocks.zh）的字符偏移 [start, end)，与
-- `pipeline.markup.prose_html` 渲染 `<span class="o" data-o="N">` 锚点时用的是同一套
-- —— 服务端渲染，前端只把 DOM 选区换算成这对数字。**不存文本**：文本永远从服务端
-- 渲染的块 HTML 里取，重跑管线后区间仍在，划痕跟着新译文走（存文本则必然与译文漂移）。
highlights(
  id        INTEGER PRIMARY KEY,
  paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  block_id  TEXT NOT NULL,        -- 锚定块
  lang      TEXT NOT NULL,        -- 'en' | 'zh'
  start     INTEGER NOT NULL,
  end       INTEGER NOT NULL,     -- 恒有 end > start（空选区在 API 层即 400）
  color     TEXT NOT NULL,        -- amber | green | blue | pink（**不带含义**，四支等价）
  created_at TEXT
)

-- ⚠️ 旧数据迁移（`db._migrate_highlights_to_ranges`）：㉚ 时代的 `highlights(paper_id, sid)`
-- 在启动时被**确定性换算**成区间 —— 服务端用当时同一份切句规则重跑 `split_en/split_zh`
-- （`markup.sentence_offsets`），把 `b-0010:c4` 解成"该块中文第 5 句的字符区间"。
-- 换算依赖切句规则的**逐字稳定性**，所以那两个函数被保留并被测试钉住，不做"顺手优化"。

-- 阅读计划里的文献队列（②：转换队列，计划作用域）
-- 实现上不单独建表：直接以 papers.conv_state 查询即可

-- 分享（⑩⑪：实时只读 + 可撤销/有效期）
shares(
  token      TEXT PRIMARY KEY,    -- 长随机，不可猜
  plan_id    INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT,                -- 有效期**上限 24 小时**（2026-09-11）；启动迁移会把 NULL 收敛
  revoked_at TEXT                 -- 非空=已撤销（⑪）
)
```

### 3.2 块类型与 payload

| type | payload | 渲染 |
|---|---|---|
| `h1`–`h4` | `{level}` | 标题（`h2.sec` / `h3.sub` / `h4`） |
| `p` | — | 段落 |
| `abstract` | — | 摘要框 |
| `figure` | `{src, caption, wide}` | `<figure><img><figcaption>` |
| `table` | `{caption, caption_zh, rows, rows_zh}` | 阅读器 `.booktbl`（逐格双语）、**导出/分享** `<table class="datatable">`（服务端 `markup` 渲染）；`rows`/`rows_zh` 是**等宽二维数组**（第一行表头），前端按三模式**逐格**选英/中（中英同形的格子只渲一支，㊴） |
| `eq` | `{latex, number}` | `<div class="eq">\[…\tag{N}\]</div>` |
| `meta` | `{authors, affil, journal, doi}` | 页眉信息块 |

### 3.3 与原型数据模型的对应

| 原型 | papershelf | 变化 |
|---|---|---|
| `Paper.st/cv/pr/upd` | `papers.status/conv_state/progress/status_at` | 同义；`upd`→`updated_at` |
| `Paper.tags[]` | `papers.tags` JSON | 自由标签（⑲），LLM 自动生成 |
| `Plan.status{} progress{} notes{}` | 下沉到 `papers` / `notes` 表 | 计划私有（⑦），不再双份存储 |
| `Doc.pageList[].blocks[]` | `docs` + `blocks` 表 | **升级为块级 ID + 双语字段**（④⑳） |
| `Plan.queue[]` | `papers.conv_state` | 不再单独维护队列数组 |
| 分享 URL 内嵌快照 | `shares` 表 | **改为实时只读**（⑩） |

---

## 4. API 契约（草案）

```
# 认证（⑭⑮）—— 注册与找回密码均为「邮箱验证码」
# 形态对齐原型：注册是单表单，找回是两步向导
POST /api/auth/register/code {email}
                             → 配了 SMTP：发 6 位验证码，返回 {sent, cooldown}
                             → 未配 SMTP：注册口关闭，返回 403（改由管理员开号）
POST /api/auth/register      {email, code, password, display_name, agree}
                             → agree 为 false/缺省 → 400（**服务端强校验**，前端拦不算）
                             → 校验通过 → **注册即登录**，直接下发 cookie；
                               同时把条款版本写进 users.consent_version / consent_at（2026-09-11）
GET  /api/auth/config        → 注册开关 + 域名白名单 + **用户协议正文**（terms_version/summary/body）
                             # 条款文案的唯一事实来源是 server/terms.py，前端只渲染不复制
POST /api/auth/reset/code    {email}
                             → 无论邮箱是否注册都返回同样的成功提示（**防探测**）
POST /api/auth/reset         {email, code, password}     # 验证码一次性，用完即废
POST /api/auth/login         {email, password} → 设置签名 cookie
POST /api/auth/logout

# 验证码参数（可用环境变量覆盖）
#   6 位数字 / 有效期 10 分钟 / 单次有效 / 同邮箱 60 秒冷却 / 失败 5 次作废 / 库里只存哈希

# 计划
GET/POST        /api/plans
GET/PATCH/DELETE /api/plans/{id}
GET/PATCH       /api/plans/{id}/settings     # 术语表（⑫）

# 文献
POST   /api/plans/{id}/papers/upload         multipart（多文件）
POST   /api/plans/{id}/papers/arxiv          {ref}   # ⑱
GET    /api/plans/{id}/papers                # 列表（筛选/排序）
PATCH  /api/papers/{id}                      # status/tags/元数据
GET    /api/papers/{id}/doc                  # 块级 JSON（阅读器取数，⑳）
PATCH  /api/docs/{paper_id}/blocks/{block_id}  {zh}   # ⑯ 编辑译文
POST   /api/docs/{paper_id}/blocks/{block_id}/retranslate  # ⑯ 重译此块
GET    /api/papers/{id}/export?lang=zh|en|dual # 合成单文件 HTML（⑳）

# 笔记
#   GET 的顺序 = **锚点在原文里的位置**（不是写入时间）：blocks.ord → 整段在前 → 字符位置 → 原文列在前
#   无落点的（文献级笔记）排最后 —— 列表是顺着原文读的（宿主 2026-09-15）
GET/POST        /api/papers/{id}/notes        # POST body 含 {content, block_id?, lang?, start?, end?, hl_id?, quote?}
PATCH/DELETE    /api/notes/{id}

# 划痕（㉚ → ㉛ 改为任意字符区间）
GET    /api/papers/{id}/highlights           # → [{id, block_id, lang, start, end, color, created_at}]
POST   /api/papers/{id}/highlights           # body {block_id, lang, start, end, color} → 201；end<=start 即 400
PATCH  /api/highlights/{hl_id}               # body {color} 换一支笔（端点不动 → 笔记锚点不受影响）
DELETE /api/highlights/{hl_id}               # 擦掉（幂等：删不存在的行也 200）
# ⚠️ 坐标是块**裸文本**的字符偏移，由**服务端**渲染器吐出的 `<span class="o" data-o="N">`
#    锚点定义；前端只用它把 DOM 选区换算成数字，绝不自己生成坐标（㉛）。
# ⚠️ 擦划痕只把笔记的 hl_id 置 NULL（`repo.delete_highlight`）—— 擦掉荧光笔 ≠ 撕掉批注。

# 分享（⑩⑪⑯；2026-09-12 分享管理）
GET    /api/shares                           # **跨计划**列出本人全部分享（带 plan_name / label）
POST   /api/plans/{id}/shares                → {token, url, expires_at}   # body {hours,label}，上限 24
POST   /api/shares/{token}/renew             # 重置有效期（从此刻重新起算，≤24h）
DELETE /api/shares/{token}                   # 取消（对访客立即 410）
GET    /api/shares/{token}                   # 计划 + 全部文献 + share meta（含 expires_at）
# ⚠️ 撤回与续期走**登录态**（作者本人），只读端不写；中间件对这两个写路由单独放行（⑯）
GET    /api/shares/{token}/plans             # 侧栏计划切换器（单元素数组）
GET    /api/shares/{token}/papers/{id}       # 块级文档（实时，不是快照）
GET    /api/shares/{token}/papers/{id}/notes # 分享者的笔记（只读）
GET    /api/shares/{token}/papers/{id}/highlights # 分享者的划痕（只读，㉛）
GET    /api/shares/{token}/papers/{id}/export
GET    /share/{token}/…                      # 只读页面（前端路由，`*` 通配整张路由表）
# ⚠️ 约束：所有写操作在只读分享上下文必须由**后端**拒绝（⑪）
# ⚠️ 约束：匿名端点的返回**形状必须与登录态一一对应**（⑩「一模一样」）——
#    分享页复用整站页面，少一个字段页面就渲染出 undefined，而 TS 不报错。

# 管理（㉑）
GET    /api/admin/users
POST   /api/admin/users                      # 手动开号（⑮ 无 SMTP 时的唯一入口）
PATCH  /api/admin/users/{id}                 # 禁用/启用、重置密码
```

**只读分享的写保护**：以中间件按会话上下文判定 —— 请求携带 share token 时，除 GET 白名单外一律 403。

---

## 5. 转换管线（本节为**首个实现目标**，㉒）

### 5.1 全流程

```
① 接入        上传 PDF  ┃  arXiv ID（⑱）
                          └→ 优先抓 arxiv.org/html/<ID>v<N>；无 HTML 版回落下载 PDF
② 解析        fitz：文本块 + 图片块 + 坐标（表格**不在这里做**，见 ①c）
①c 原文校对   LLM agent（㉝）：给**原 PDF 页图**（可 region 放大）+ 抽取结果 + 工具，
              逐页校对分块与结构 —— 拆/合/删/改块、改标题层级（㉝ 补记二）、
              **栏间续段标记**（㊲）、**表格重建**（㊴ `set_table`）。页级续跑（`ok_pages`）
              单次调用的**瞬时故障重试 20 次**（宿主 2026-09-17）：退避 2/4/8/16 → **封顶 30s**，
              最坏总等待 ≲8 分钟。**为什么放宽**：生产实测一次 504 就把 2 次重试用完
              ⇒ 8 页稿剩下 4 页**从未校对**，而界面看起来是成功的（宿主正是据此看到
              "竖向表格提取错乱"——那是解析原样，不是校对结果）。
③ 英文 HTML   生成带块标记的英文 HTML   <p data-b="b-0042">…</p>
③b 公式 LaTeX 化  含数学的英文块过一遍「只排版不改写」的 LLM 通道（决策㉓）
              —— **必须在 ④ 之前**：译文由构造继承同一份 LaTeX，中英公式天然一致
④ 分块翻译    按标记边界切片，注入术语表（⑫），逐片送 LLM
⑤ 标记校验    纯程序：en/zh 标记集合比对（缺失/重复/乱序/漏译）→ 不合格块自动重译
⑥ 块级 JSON   解析为 blocks 落库（⑳）
⑦ 合成 HTML   按需合成双语/单语单文件 HTML（导出、分享）
```

CLI 对应（顺序即依赖）：
```bash
papershelf parse <pdf> -o out          # 解析 → doc.json + en.html
papershelf latex out/doc.json -o out   # 公式 LaTeX 化（决策㉓）；会清空受影响块的译文
papershelf translate out/doc.json -o out --resume   # 翻译（只补缺译的块）
papershelf check out/en.html out/zh.html           # 标记保真校验（纯程序）
papershelf export out/doc.json -o out/dual.html --mode dual
```

### 5.2 解析（③ 沿用的做法 + 已踩坑的加固）

| 事项 | 做法 |
|---|---|
| 文本 | `page.get_text("dict")` 取 blocks/lines/spans |
| 图片 | **image blocks（type==1）的 bbox**，配 (y,x) 排序；**不用 `get_image_rects()`**（双栏时 bbox 全从 y=58 起，不可靠） |
| 图注配对 | 图注 block 与图片 block 按 (y,x) 联合排序；左栏 x≈54 / 右栏 x≈309 |
| **表格**（㊴，2026-09-16） | **解析阶段不识别表格，交给 ①c 校对 agent**（`set_table` 工具）。原因：`page.find_tables()` 三种策略都抓不到这类「有横线、没竖线」的表，`strategy="text"` 还会把双栏正文判成 62×7 大表；而 agent 本来就在逐页看页图。⚠️ **字符不许靠视觉模型认字**：它用 `read_blocks`/`read_block` 拿到抽取到的**逐字原文**，视觉只判**结构**（哪几行一张表、列边界、合并格、跨页、表注归位），再把已有文本装进格子。三条护栏：①每个格子文字必须能在被消费的源块文本里**逐字找到**（找不到整份拒收，防凭图默写）；②行/列数必须一致（允许空尾格，`rows_zh` 与 `rows` 形状不符则回落英文网格）；③源块里不许有成句文字没被消费（`_table_residue`，防把正文段吃进表）。**提示**（`_table_regions`）：横线 + **同一行上横着好几段文字**两条同时成立才报"疑似表区"，实测 37 页报 3 处（真表 3 张）、零误报；只靠横线会报 20+ 处（图框边、页眉装饰线），只靠文字会把双栏正文报成表 —— 提示只买**召回**，判定仍在 agent。单元格**双语**（`rows` + `rows_zh`，跟随阅读器三模式）；**不动 `parse.py`、不涨 `PARSE_VERSION`**。⚠️ v12：格边框常常**逐格单独画**（实测表头线 6 段、全宽 658pt 而任何单段都不到页宽的 25%）⇒ `_h_rules` 必须先把**共线的短段缝成整条线**再按长度筛选，否则「横线」那条判据在这类表上恒为假（实测转正后的第 5 页 `tables: 0`，agent 是**自己看图**才发现有表）|
| **整页旋转的页面**（v12，2026-09-17） | 宿主：「原 pdf 里，有一个竖向的表格，提取后格式错了」。病灶**不在表格识别，在坐标系**：那张表整页是**旋转着画**的（页面 `/Rotate` 仍是 0，`page.rotation`/`rect` 都看不出异常 —— 只有 span 级 `line["dir"]` 说真话：`(0,-1)`），而整条链路（`_by_y`、`_gutter`、①c 的 `read_page(region=…)`）都假定文字水平 ⇒ 表格的**列被当行**：同一行相邻格粘成一句，`Ref` 列的值（`[ 92 ]`）排到表头之前。修法 = **纯坐标变换**：`normalize_rotated_pages` 用 `show_pdf_page(rotate=…)` 把该页原样重画（文字/矢量/图片三层一起转），于是它与一张普通横排表格**完全一样**，不用给表格识别加任何特例。判据按 **span 字符数加权**的多数 `dir`（`_ROT_MIN_CHARS=200` / `_ROT_VERT_RATIO=0.6`，**不信 `/Rotate`**，也不要被一个竖排标注骗到整页躺下）。旋转页**不做分栏判断**（格间空白会被 `_gutter` 当栏间空白，行序被劈成两半）。转正副本落 `papers_dir/p<id>/normalized.pdf`（**不是**全库共享的 `papers_dir/normalized.pdf` —— 那个名字在 `MAX_CONCURRENCY=2` 下会互相覆盖，①c 静默读到别人的 PDF；放 `p<id>/` 下顺带被删文献的整目录 `rmtree` 收走），①c 读**同一份**。同批修掉一处**静默丢内容**：`pending_figure` 在几何配对认领图注后**没复位**，下一页以 "Table N" 起头的表注撞进"给上一个图当图注"的判定而该图已有图注 → 什么都不写、直接 `continue`（实测 8 页稿第 5 页的 `Table 2 …` 在任何块里都不存在）；判据收紧为「**同一页** 且 该图**还没有**图注」。`PARSE_VERSION` → **12** |
| 页眉 logo | 按尺寸启发式（如 325×89）识别并剔除 |
| **页眉/页脚文字**（v10，2026-09-17） | 宿主：「**页眉文字不需要有意丢掉。和 pdf 尽量保持一致**」→ 撤掉「跨页重复就删」的旧过滤（`_running_headers`，它把期刊页眉那行整行删掉）。页眉/页脚照旧留在产物里，可译、可在两种语言里看到。**①c 提示词同步加了一条**：页眉/页脚不属"重复块应删除"，也不许并入正文或升成标题（实测 agent 拿到页眉块**一个没删**）。⚠️ 代价（宿主已知并选择 A「照译」）：模型对这类行**时译时留**（实测 6/8 页原样返回）→ 会被校验判成"疑似漏译"挂上**「待校对」**标记，不阻塞转换 |
| **页边横线**（v10，2026-09-17） | 宿主：「这里少了一条水平线」—— 每页页眉文字下方那条横跨正文宽的细线。`get_text()` 只回文字/图片，**矢量线条一条都不在产物里**（也不是新 bug：解析器从来没处理过线条）。修法：`page.get_drawings()` 取**又细**（≤2pt）的线，判据四条缺一不可（在页边带内 / 横跨正文文字列 / 紧贴某一行 / 细），**宁可漏画不许画错**（画错线比没有线更像排版事故）。挂成 `payload["rule"]="below"\|"above"`（**挂在它所属的那一行块上**，块被 ①c 改写/挪动时线跟着走）。同批还加了 `payload["band"]="top"\|"bottom"`（这行字贴在页面上下边缘）—— 渲染端据此把这行渲成**稍小 + 灰**（PDF 里页眉 8.5pt / 正文 10pt）。两戳**只影响渲染**：`typeset=False`（校验/翻译 prompt/LaTeX 化）产物逐字不变（有测试钉住）。装饰类只在 `typeset=True` 时挂，且必须是**一个** `class` 属性（拆成两个 `class=` 浏览器只认第一个，装饰静默失效）。`PARSE_VERSION` → **10** |
| arXiv HTML | `<math alttext>` 取**未损坏 LaTeX**；图取 `figures/<file>` 原始文件；参考文献取 `<li id="bib.bibNN">` 并清理 `Cited by: §II` 残渣（`<li>` 天然**一条一块** ⇒ 直接是 `ref` 块，不需要 PDF 路线那套碎片合并，㊹） |
| 下载校验 | `content-length` 比对 + PIL 检测底部 25% 纯黑占比 >50% 判截断；`curl -sL -C -` 多轮续传 |
| **标题识别** | **不能靠字号**（IEEE 模板里标题与正文同为 10pt）→ 用「字体 + 编号模式」：`I.` + Helvetica = h2，`A.` + Helvetica-Oblique = h3；无编号的 `REFERENCES` 另行识别 |
| **装饰字剔除** | 期刊 logo 里 29.9pt 的单字母「I」会被误判成标题并顶替真标题 → 按**文本长度**识别装饰字 |
| **图注定位** | 跨栏大图会让图注在阅读序里离得很远 → 按**几何位置**配对；图注可能在图片**之前**（B5-01 的 Fig. 2 即如此）→ 双向回溯 |
| **文末材料** | 参考文献从 `REFERENCES` 小标题起标出（含小写大写伪空格还原 `R EFERENCES → REFERENCES`）；出版社水印页脚（`Authorized licensed use limited to…`）直接剔除 |
| **参考文献条目**（㊹，v13，2026-09-17） | 宿主：「参考文献没有翻译」→ **只译文献标题**（作者名/期刊/卷期页/DOI 保原文：读者要靠它们检索）。要译标题先得**有一条完整的条目**：PDF 抽出来是一条连续文字流，被块检测按栏/行组切成 ~30 个碎片（实测生产那篇 29 片 / 6,189 字符），切口常落在**词中间**（`man-` + `ufacturing`），一块里还塞着下一条的前半截。⇒ `merge_ref_entries` 把碎片按**条目编号**切成整条，每条一个 **`ref` 块**（`refs` 从此只表示"切不出条目的碎片"，仍免中文）。切分判据：`[N] ` 与 `N. `+大写字母两种风格各试一遍、取命中多的；**必须连号**（读到第 1 条之后下一个必须是 2 —— 这一条才挡得住 DOI 里的 `… / 1. Oe. 4582548`）；拼接**只动空白**（非空白字符逐字守恒，有测试钉住），行末断词的连字符**不猜补**（`Laser-directed` 是反例，该不该合是看图那一步①c 的活）；切不出来（无编号/编号中途失效/单条 >2000 字）→ **原样返回**，不丢字也不产出假条目。`PARSE_VERSION` → **13** |
| **数学行合并** | PDF 会把一个矩阵/多行公式抽成**逐行独立块**（`⎡`、`⎢⎢⎢⎢⎣`、单行公式、`,` 各一块）——必须按「连续数学行」合并后再 LaTeX 化，否则 LLM 只见碎片，会拼出**空矩阵**（实测 `\begin{bmatrix}\end{bmatrix}`）。B5-01 合并掉 53 个碎片块（598 → 545） |
| **栏间续段的标记**（㊲，2026-09-15） | 两栏页面上「左栏末块 + 右栏首块」常是**同一句话被栏间切缝劈成两块**（译文于是也断成两截）。解析阶段**只标记**：`payload["seam"]="col-spill"`，由 ①c 校对 agent 看页图决定是否 `merge_block`。判据**几何 + 文字两条同时成立**：紧邻对位于「左栏 → 右栏」的接缝，且前块**无句末标点**、后块**小写/左括号起**。只用文字实测 37 页报 73 处（表格行与页眉全中）；只用几何会把"正常换栏"一起标 —— 两条合用实测 25 处、抽查全真。⚠️ 几何信息**只在解析阶段有**（入库只留 `payload.page`，bbox 丢了），所以只能在那一层算；`PARSE_VERSION` → **7** |

### 5.3 块标记方案（决策④ 的核心）

- 块 ID 格式：`b-0001`（`b-` + 4 位序号，随解析顺序分配）；分页内保证唯一，全文唯一
- 英文 HTML 中每个块带 `data-b`：
  ```html
  <h2 class="sec" data-b="b-0012">II. Foundations</h2>
  <p data-b="b-0013">…</p>
  <figure data-b="b-0020"><img src="assets/fig3.png"><figcaption>Fig. 3 …</figcaption></figure>
  <div class="eq" data-b="b-0021">\[ … \tag{8} \]</div>
  ```
- **标记必须穿透翻译**：翻译 prompt 明令「`data-b` 属性原样保留、不得增删合并块」
- **免中文块标 `data-nt="1"`**（参考文献**碎片** / 公式 / 装饰图）：它们在中文视图里**回落英文原文**，
  从而不被校验器误判为「漏译」。这个标记是「不送模型 ↔ 不判漏译」两处判据的**唯一来源**（见 5.5）
- **部分中文块标 `data-pt="1"`**（参考文献**条目** `ref`，㊹）：它的字面文本**只有标题**是译文，
  作者/卷期页/DOI 一律照抄原文 ⇒ 校验器**不比对数字**（否则每条文献刷一条「数字不一致」）。
  与 `data-nt` 同一套路：标记**随 HTML 走**（校验器只看得到 HTML，没有块类型 ——
  按元素标签判永远不成立，`ref` 渲染出来就是 `<p>`）
- 对照视图（`dual`）的 `data-b` **只挂在行上**（一行 = 同 ID 的中英两块），
  避免同一块标记在文档中出现两次、配对歧义
- **无译文的块在对照视图里横跨两栏只显一次**（纯公式 / 参考文献 / 尚无译文）——
  否则同一条公式会在左右栏各出现一遍，既冗余又误导
- 中文视图**没有译文时回落英文原文**（绝不渲染空元素）：否则 dual 会出现整块右栏空着的洞、
  单语导出也会缺内容。回落对 `data-nt` 的块尤其重要

### 5.4 分块翻译（决策④ 的必要条件）

- **绝不整篇一次送**：长文必被输出长度截断（现有流程靠 `cat >>` 分块写躲过）
- 切片规则：按标记边界累加至约 **12 块 / 6000 字符**（实测一篇 59 万字符合计切 19 片），**块不跨切片**
- **免中文块不进切片**：参考文献**碎片**与公式/符号块（另 12%）**根本不送模型** ——
  既省钱，又避免「送了不译 → 判漏译 → 重译 → 仍不译」的死循环
- **参考文献条目走第三条通道**（㊹，㊴ 表格那条的同一取向）：**送一条、要一条 JSON**
  （`{"title_en": …, "title_zh": …}`），**只译标题**，写回 `payload` 并保留原条目结构。
  合格与否由**程序**判，不由模型自称：标题必须有中文、不许混进 DOI/URL（那是"整条都译了"的
  指纹）、`title_en` 必须能**回原文定位**（模型回抄时会把行末断词拼成一个词，
  所以定位是**折叠后**比对：`man-ufacturing` / `addi- tive` 两侧都折叠成 `manufacturing` /
  `additive`）、标题不许占满整条。不合格重试一次 → 仍不合格就**不写中文**（英文原样回落）
  并标「待校对」，与正文/表格同一套收敛保证
- **图注与标题必须译**（它们再短也是正文内容）：`Fig. 2. Overall structure.` 仅 3 个实词、
  `II. FOUNDATIONS` 仅 1 个实词，都会掉进「实词不足」的豁免口径 → 两者单列规则
- 每片 prompt 注入：
  1. 全局术语表（⑫，计划级）
  2. 前后各 1 块上下文（保持指代与衔接）
  3. 输出格式硬约束（保留 `data-b`、不译公式 LaTeX、**参考文献条目只译标题**、数字与引用编号原样）
- **块级重译**（⑯）：同一 prompt 只送 1 块 + 上下文

### 5.4b 公式 LaTeX 化（决策㉓）

fitz 抽出的数学是 **Unicode 文本且间距被拆散**（`L f V ( x ) := ∂V ( x )`）。
一个独立的「**只排版、不改写**」LLM 通道把数学转成标准 LaTeX：
行内 `\\( ... \\)`、展示 `\\[ ... \\]` + `\\tag{N}`，交前端渲染（与待定项 8 联动）。

| 要点 | 做法 |
|---|---|
| **顺序** | 只作用于**英文块**，且在翻译**之前** —— 译文继承同一份 LaTeX，中英公式天然一致 |
| **送谁** | `looks_math()` 判定：含数学符号 / 带编号的展示公式 / 短符号行 / 希腊字母（实测 197/598 块） |
| **保真校验** | `latex_problems()`：非数学实词不得丢失（连字断行 `defi- nition`、`thatforall` 伪影先归一化）、公式编号一致、必须真的产生定界符；编号按**切片级**比对（跨块拆分公式的 `\\tag` 会落到相邻块） |
| **降级** | 不合格重试 1 次 → 仍不合格**回落原文本**并标 `needs_review`，**绝不阻塞管线**（决策②） |
| **副作用** | en 被改写 → **仅这些块**的 `zh` 清空（`zh_source=human` 的人工修订保留），随后 `translate --resume` 精确补翻 |
| ⚠️ 坑 | 判「en 是否被改写」必须**逐块比对本轮前后的文本**，**不能**用「历史上被 LaTeX 化过」的标记 —— 后者每跑一次就清掉全篇译文、白烧一遍 LLM（实测一次 20k tokens） |

> 「该块是否需要中文」的判据也随之 LaTeX 感知：`prose_core()` 剥掉数学区间与 LaTeX 命令后再看实词 ——
> `where \\( ... \\)` 要译（剩 "where"），`\\[ \\dot{x} = f(x) \\]` 免译（不剩实词），
> 公式里的 `\\text{aclf}` 是**数学标签**不算散文（≥2 词才算）。

### 5.5 标记保真校验（决策④，纯程序）

**核心原则（实测校正）：校验结果会被用来触发自动重译，所以每条判定都必须"重试修得好"。**
凡重试也修不好的（数字/编号改写、本质保持英文的块），一律降级为**警告**——
否则管线会在这些块上反复重译、永不收敛（首轮实测：8 块烧掉 19,591 tokens 空转仍未通过）。

**阻塞级（可判定 + 必然收敛，触发定点重译，重试上限 2 轮）**

| 检查 | 判据 |
|---|---|
| 标记缺失 | `en_markers ⊄ zh_markers` |
| 标记多余 / 重复 | zh 出现 en 没有的块，或块重复出现 |
| 顺序错乱 | 块序列与 en 不一致 |
| 疑似漏译 | **本应有中文**的块（见下）无 CJK 字符 |
| 标签不配对 | `div`/`figure`/`table` 开闭数不等 |

**警告级（不阻塞，交阅读器按需修订，⑯）**

| 检查 | 判据 |
|---|---|
| 数字不一致 | 块内数字序列与 en 不同（中文「2025年4月25日」合法改写数字，故不可阻塞） |
| 公式编号不一致 | `\tag{N}` 与 en 不同（切片级多集合比对） |
| LaTeX 化保真 | 实词丢失 / 编号变化 / 未产生定界符（决策㉓ 的校验，不合格**回落原文本**） |

**「本应有中文」的判定**（校验器与翻译器**共用同一函数**，否则判据不一致又会死循环）：

- 参考文献**碎片**（`refs`） / 公式块（`eq`） / 装饰图（`deco`） → **不要求**（`data-nt` 标记）
- **参考文献条目**（`ref`，㊹）→ **要求**（译出来的就是标题那一段）：中文栏里必须有 CJK，
  否则报「疑似漏译」；但**数字不比对**（中文栏里的作者/卷期页/DOI 是原件照抄的，
  不比数字不等于不比内容）—— 这一条随 HTML 走（`data-pt` 标记），不能按元素标签判
  （`ref` 渲染出来是 `<p>`）。没译出来的那种（`zh` 为空）挂 `data-nt` 回落英文原文，
  出口是翻译器给的 `needs_review`「待校对」，**不**触发整篇重译（否则又是死循环）
- 图注（`<figure>`）与标题（`h1–h4`）→ **只要有实词就要求**（短到 3 个词也是正文）
- 其余块 → 实词（≥3 字母的英文词）**≥4 个**才要求（公式/符号块没有可译的句子）

> 校验**能抓结构，抓不到语义**（误译/幻觉）→ 由 ⑯ 块级修订兜底（决策⑯ 已明确此边界）。

### 5.6 任务与成本（决策②⑧）

- **任务队列**（✅ 已落地，2026-09-12 事故修复）：**进程内常驻队列 + DB 轮询**
  `queued` → `doing` → `done|failed`，自托管偏好轻量，**不引 Celery/Redis**（待定项 3）
  - 实现：`server/queue.py` 的 `ConversionQueue`（常驻线程）+ `_Pool`（`max_concurrency` 个
    常驻工作线程，**并发交付**）+ `recover_stuck`（启动恢复）。
  - **启动恢复**：`lifespan` 启动时把 `doing` 一律复位为 `queued`（上个进程被杀留下的僵尸）。
  - **三处铁律**（都是真事故换来的，有护栏测试钉住）：
    1. **先抢槽位、再认领**：`convert_paper` 必须在 `with concurrency_semaphore():` **之内**
       才做 `queued → doing` 的原子认领（`WHERE conv_state='queued'`）。反过来写会在并发 2、
       队列 8 时**瞬间把 8 篇全标成 doing**（界面显示 6 篇假「转换中」）。
    2. **信号量必须惰性求值**：`concurrency_semaphore()` 是函数而非模块级 `_sem = Semaphore(...)`；
       后者在导入时求值，改环境变量/测试夹具都不生效。
    3. **`queued` 不是"排队中"，是"待认领"**：真正的互斥点在认领那一句，别处不得改 `conv_state`。
- **护栏**（待定项 5）：全局并发上限、`conv_attempts` 上限（如 3）、以及 ①c agent 自己的累计 token 预算
  （`PAPERSHELF_PROOFREAD_TOKEN_BUDGET`，**这一条是活代码**）。
  ⚠️ **2026-09-17 宿主定：不要「单篇总 token 预算」** —— 原先那个 `PAPERSHELF_TOKEN_BUDGET`
  只有 `config.py` 读、**全仓库没有任何地方使用**（真转换实测 107 万 tokens 也拦不下），已删除；
  留着它比没有更糟：文档写着「单篇 token 预算」，让人以为有护栏。
- **去重**（待定项 7 提案）：以 PDF/arXiv ID 的 hash 命中已有 `docs` 时，**复用块级 JSON**，只重放翻译（省 LLM 花费，不改 ⑦ 归属模型）

### 5.6b 元数据抽取（决策⑲，2026-09-12 补做）

⑲ 原文承诺「元数据（标题/作者/会议/年份）由 LLM **自动抽取**、标签 LLM 自动生成 3–5 个、
可手动改」，**自始未实现**（生产表现为文献库全叫 `original`、作者列恒空）。

- **时机**：紧接解析之后、翻译**之前**。这样即使翻译失败，元数据也已写进 `doc.meta`
  （用户打开文献库立刻要看的就是它）。
- **输入**：只送**首屏** 14 块 / 4000 字符（跳过图/公式/参考文献 —— 它们不含元数据）。
  外加解析器给出的**候选标题**（`meta.title_candidates`）。
- **一次调用出全部字段**：`title_en` / `title_zh` / `authors` / `venue` / `year` / `tags`。
- **候选标题是必需的**：`_finalize` 取第一个 h1 当标题，但封面常有两个 h1
  （**期刊名在前、真标题在后**，实测 12 篇里 2 篇如此）。prompt 必须明说
  「第一个往往是期刊名」并列出全部候选，否则模型只能从残块里**编**标题。
- **输出强校验、失败退回不写**：去围栏、抠第一个平衡 `{...}`、`year` 必须落进
  1900–2100 的 `int`（`papers.year` 是 INTEGER，喂字符串会静默变 0）、`tags` 归一后上限 5。
  **宁可不写，不能写错** —— 写进去的垃圾会被"不覆盖用户值"的写回逻辑锁死。
- **抽取失败不打挂整篇**：元数据是锦上添花 → 异常一律吞掉返回 `{}`，`conv_state` 照常 `done`。
- **成本**：实测 **≈1.5k tokens/篇**（4 篇合计 5,930）；对比重跑整篇转换的 5.8 万–24.9 万，
  差约 20–160 倍。回填存量只该走**只抽元数据**这条路。

**⚠️ 「标题是占位值」必须落列（`papers.title_is_placeholder`），不能现场推导。**
第一版判据是「`title == Path(pdf_path).stem`」，看着干净（用户一改标题就不相等，
自动获保护，连迁移都不用）—— 但 PDF 落盘时文件名**加了时间戳前缀**
（`2026-09-11T143352+0000_original.pdf`），`stem` 与 `title='original'` 永不相等，
生产 12 篇会被**全部**判成"用户手填"，真标题依然写不进去（只是从"必错"变成"看起来对"）。
**占位符是一种意图，不是一种值。** 判据单一来源在 `server/titlemeta.py`
（`converter` 与 `db._migrate` 共用；放 `converter` 会与 `db` 成环）；
缺列时退回路径兜底，且**先比 `source_ref`（原始文件名）再比 `pdf_path`**。

**写回优先级：用户手填 > 自动抽取。** `title` 仅当占位时覆盖；
`authors`/`venue`/`year`/`tags` 仅在为空时填。`PATCH /papers/{id}` 一旦收到非空 `title`
即把占位标记置 0 —— 否则用户填的标题会被下次转换顶掉。

**两端「置 0」缺一不可（宿主 2026-09-13 定的口径：回填成功 = 已确认）**：
`_writeback_meta` 写进抽到的真标题时**同时**撤掉占位标记。这一列的名字就是它的语义
（"标题还是导入时那个文件名"），既然已经不是了就该是 0；只改标题不改标记会让**列在说谎**，
直接后果是 `backfill-meta --only-missing`（判据「占位=1 且没作者」）把已回填的 12 篇
每跑一次都重抽一遍（≈1.6k tokens/篇，白烧）。代价是重跑转换不再覆盖已抽出的标题 ——
**刻意如此**：抽到的标题就是这篇的定稿，要改就手工改。反之抽取失败（没拿到标题）时
标记**必须留在 1**，否则这篇永远补不上。

### 5.7 管线自检失败的呈现（待定项 11 提案）

`conv_state=failed` + `conv_error` 摘要；文献库/阅读器显示失败标记与「重试」；不阻塞其他文献。
重试额度用尽的块标 `payload.needs_review = true`，在阅读器里显示「待校对」徽标。

**⚠️ 出口裁决必须与「收敛保证」一致（2026-09-11 生产事故修正）**

`converter` 在翻译后还会**再**跑一次全量 `validate()`。这次判定必须只认**结构性问题**
（丢块/多块/重复/乱序/标签不配对）——「疑似漏译」在翻译器里已经跑满 2 轮定点重译并
把残余块标成了 `needs_review`，走到出口说明**已经收敛**，此处再抛异常等于把整篇已完成的
译文连同那几块一起报废。

*实测*：一篇 400 块论文只有 1 块重试后仍无中文 → 整篇 `conv_state=failed`、
`tokens_used` 还是 0，用户看到「转换失败」而不是「1 块待校对」。这与本节的
「降级 → 回落原文本 + `needs_review`，**绝不阻塞管线**」直接矛盾，故改为出口只拦结构性问题
（`enforce_report()`，守这条的回归测试见 `tests/test_server.py`）。

**配套修正：标题/图注的「本应有中文」判据必须 LaTeX 感知。** 解析会把展示公式的续行误判成
标题（`\(\mathrm{adj} = 0.77 \text{–} 0.96\)) [ 230 ].` 成了 `h2`），它剥掉数学后**一个实词都不剩**，
但裸 `_WORD_RE` 能从 `\mathrm` / `\text` 里数出词来 → 判「本应有中文」→ 模型给不出中文 →
死循环。标题分支现已改用与正文同一套 `prose_core()`。

### 5.8 首轮实测记录（2026-09-10，B5-01-CBF-review）

第一篇用真实论文跑通全流程（`papershelf parse/translate/check/export`）：

| 指标 | 实测 |
|---|---|
| 解析 | 598 块：`p` 394、`refs` 168、`h2` 9、`h3` 17、`figure` 10；图片 18 张（剔除 logo/头像 8 张） |
| 翻译量 | **需翻 216 块 / 58.5k 字符**（免中文块 382 块不送模型 = 原全文的 38%） |
| 消耗 | 43,658 tokens，19 片，**9 分钟**，校验**一次通过** |
| 收敛 | 首轮 7 块重译后即通过；`needs_review` = 0 |
| 校验 | ✅ 通过；警告 40 块（数字改写，如日期「2025年4月25日」），按设计不阻塞 |
| 产物 | `doc.json` 306 KB、`dual.html` 258 KB、`zh.html` 108 KB（10 图 / 9 节 / 17 小节，与参照产物一致） |
| 断点续跑 | `--resume`（跳过已译块）/ `--ids`（定点重译）/ `--dry-run`（预估不调模型） |

**第二轮（决策㉓ 公式 LaTeX 化后，同日）**

| 指标 | 实测 |
|---|---|
| LaTeX 化 | 197 块含数学 / 33 片 / **39,729 tokens**；重试 20 片，**0 块最终回落**；残留 Unicode 数学 **0 块** |
| 公式量 | **display 61 / inline 361**（宿主参照产物 39 / 343）—— 达到并超过现有水平 |
| 翻译 | 259 块 / 22 片 / **56,272 tokens** / 10.5 分钟；重试后仍不合格 2 块（`needs_review`），不阻塞 |
| 校验 | ✅ 标记保真通过；警告 31 块（数字改写，如日期中文化） |
| 保真校验迭代 | 6 处误判被修掉：连字断行被当成"文字丢失"、跨块 `\tag` 编号漂移、公式内 `\text{aclf}` 被当成散文词、PDF 缺空格粘连词（`thecontrol`）被拆开后判成丢失、原文伪影 `forall`、`latex` 命令误清全篇译文 |

---

## 6. 前端（SPA）

**M3 已落地**（`web/`：React + TypeScript + Vite），**M4 已完成、M5 = 复刻原型**。

### ⚠️ 一处已被推翻的判断（留证，免得再犯）

本节原先写着：总览/文献库/看板是「**跨计划**的全局视图」，v1 没做是"**刻意收窄**"，
留待 M5 等真有跨计划需求。

**这是错的。** 回原型核对（`literature-workbench.html` L1479–1483）：

```js
const activePlan = () => state.plans.filter(p => p.id === state.activePlanId)[0] || state.plans[0];
const papers  = () => activePlan().papers;              // 所有取数都走这里
const S       = { get: p => activePlan().status[p.id], set: (id, v) => { activePlan().status[id] = v; } };
```

总览、文献库、看板、阅读器**用的都是 `papers()` = 当前计划的文献**。
侧栏那个计划切换器切的是**当前计划**，页面随之换数据 —— 原型压根没有跨计划聚合。

所以那三个页面**从来不是"跨计划视图"**，"v1 没做"不是收窄而是**漏做**；M5 的定位也随之改变：
**不是"加跨计划能力"，是把原型已有的页面对齐做出来**（范围仍严格限定为计划内）。

| 路由 | 内容 | 作用域 | 现状 |
|---|---|---|---|
| `/login` `/register` `/forgot` | 认证（⑭⑮） | — | ✅ |
| `/` | 总览：指标卡 + 状态甜甜圈 + 阅读节奏柱状图 + 待办 + 最近文献 | **当前计划** | ✅ M5 |
| `/library` | 文献库：筛选 chips（带计数）+ 排序 + 表格 | **当前计划** | ✅ M5 |
| `/board` | 四列拖拽看板（待读/在读/已读/已整理） | **当前计划** | ✅ M5 |
| `/reader/:paperId` | **阅读器（核心）** —— 从文献库/看板/总览的文献行进入，**侧栏不设独立条目**（㉚；与原型一致：阅读器里时高亮落在「文献库」） | 当前计划 | ✅ M5（字号 / 大纲 rail / 对照模式）+ ✅ ㉛ 划痕（任意区间高亮）· 区间笔记 · 点笔记跳回原句 |
| `/plans` | 计划列表 / 新建（含导入·术语表·**新建分享**抽屉） | 全局 | ✅ |
| `/shares` | **分享管理**（⑯）：4 张指标卡 + 筛选 chips + 表格（倒计时 / 状态 / 复制·预览·重置有效期·取消） —— 一条链接可能在多处出现，此页是**跨计划**的唯一管理入口 | 全局 | ✅ 2026-09-12 |
| `/plans/:planId` | 同上（深链形式） | 单计划 | ✅ |
| `/share/:token/*` | **只读分享（⑩⑪）：整站同一套路由 `AppRoutes` 原样挂在这个前缀下** —— 侧栏/顶栏/全部视图复用，只把写入口隐藏、落一条 `.share-banner`；取数走 `/api/shares/{token}*` 白名单（所有非 GET 由后端 403） | 单计划 | ✅ |
| `/admin` | 最小管理页（㉑）：指标卡 + 筛选 chips + 用户表 | 全局 | ✅ M5 |

**移动端（≤920px）**：侧栏**横置为一条顶栏**（不是 `display: none` —— 隐藏会丢掉全部导航），
断点 1200/920 与原型一致。

**"当前计划"是本 SPA 的一等上下文**：侧栏有计划切换器（含最近计划列表）、计划进度卡
（目标数 / 完成率 / 已精读·已入库），总览/文献库/看板/阅读器都读它。切换计划 = 换整屏数据，
路由保持不动（与原型的 `go()` 语义一致）。

后端**基本不需要为这三个页面加接口** —— 所需数据已具备：
`GET /api/plans/{id}/papers`（含 status/progress/tags/conv_state）、`papers.status_at`（节奏图数据源）、
`PATCH /api/papers/{id}`（看板拖拽改状态）。

**PDF 分页容器（已落地，宿主 2026-09-11 选 A）**：原型的阅读器把文档渲染成带页码的
**PDF 页框**（`<section class="pdf-page">` + "第 N 页 / 共 17 页"页脚）。现在**真分页**了：

- **数据**：解析阶段给**每个块**盖 `payload.page` 戳（`parse.py` 的 `_paged_adder`）。
  原先只有 `figure` 块带页码（循环里手写的那一处），所以只能渲成一条流。
  `merge_math_runs` 的合并块沿用首块页码（跨页合并的极少数公式会有 1 页偏差）。
- **失效**：`doc_cache` 的键改为 **PDF hash + `PARSE_VERSION`**（`papers.py`）。
  不改这个的话，PDF 没变就永远命中旧解析产物 —— 新代码看着正确、分页却不出现（踩过类）。
  ⇒ 已转换的文献需**重跑管线**才会带页码（会再花一次 LaTeX 化与翻译的 token）。
- **渲染**：阅读器与分享页同一套（`pages/PagesBody.tsx` + `pages/pageGroups.ts`），
  导出 HTML 也同版式（`synth.synth_dual` 按页切 `<section class="pdf-page">`）；
  **标题区跟在第一页**（原型 `idx === 0` 才渲 `doc-head`）。
  页内的**版面装饰**（页眉/页脚小字 + 页边横线）也走同一份块 HTML：类名由服务端
  `markup._furn_cls` 从 `payload.band` / `payload.rule` 生成（v10），**阅读器不自己算坐标**
  —— 两份 CSS（`markup.CSS` + `web/src/styles.css`）必须成对，有护栏钉住。
- **降级**：老文档（无 `page`）→ 全篇一页、页脚不显示页码，**内容一块不少**（有回归测试）。

**顺带修掉一个真缺陷（分页把它暴露了）**：`figure` 的 `img` 只有 `loading="lazy"` 而没有宽高，
未加载时高度≈0 → 滚动到哪长到哪，整篇高度事后上浮约 7,300px，**大纲跳转的落点偏掉约 7,500px**
（跳完还在页中间）。已改为用资产表里的 `width`/`height` 给 `img` 占位（解析阶段另存 `ratio`）。
⚠️ 这个缺陷**与分页无关**（把页 section 拍平后同样复现），是懒加载图的通病。

两条**不可回退**的前端约束（写在这里免得后来者"优化"掉）：

1. **块 HTML 由服务端给**（`en_html`/`zh_html`，见 §6.2）：前端只 `dangerouslySetInnerHTML`，
   不自己排版公式、不自己拼资产 URL。于是前端零数学运行时、零字体、零 CDN，
   且屏幕上看到的与导出 HTML 是同一套渲染 —— 这是"保版式"这个卖点的实现前提。
2. **改块/重译必须回完整块对象**：只回 `{id, zh}` 会让 `zh_html` 停在旧版本，
   界面"保存成功但不变化"（踩过）。

### 6.2 阅读器（决策⑤⑥⑯⑰ 的汇聚点）

```
┌───────────────────────── 工具栏 ─────────────────────────┐
│ [并排对照|仅中文|仅原文]   字号− +    [标记为已读]  进度 68% │
├───────────────────────┬──────────────────────────────────┤
│  英文（单栏流式）      │  中文（单栏流式）                 │
│  <块 b-0013>          │  <块 b-0013>                     │
│  …                    │  …            ← 按 data-b 对齐滚动 │
│                       │                                  │
│  悬停/点击块：重译此块 · 编辑译文 · 加笔记（⑯）              │
└───────────────────────┴──────────────────────────────────┘
右侧栏：笔记 / 大纲
```

- **单栏流式**，不复刻 PDF 双栏（⑥）
- 对齐滚动：以块 ID 为锚，左侧滚动 → 右侧定位同 ID 块（`⑤`）
- 进度：滚动监听累计已阅块 → `progress`；**标记已读 → 100% 且锁定**（⑰）
- 块渲染器：h/p/figure/table/eq 各一（⑳）；公式渲染**不依赖 CDN**（待定项 8）

**划痕（㉛，2026-09-13；原型同步更新）—— 高亮的最小单位是任意字符区间**

㉚ 的锚点是**句**（`sid`），宿主 2026-09-13 指出那不对：标准读论文的方式是
**先挑一支笔，再随手划住任意一段**，划多长就是多长，不必正好是整句；选中任意一段也能加备注。
于是锚点换成**字符区间**，㉚ 的 `sid` 退役（旧数据在启动时确定性换算，见 §3.1）。

三件事共用一套锚：`(block_id, lang, start, end)` —— 块**裸文本**的字符偏移，闭开区间。

- **尺子由服务端给**：`pipeline/markup.prose_html` 在**文本单元的每个端点**吐零宽锚点
  `<span class="o" data-o="N">`。前端按文档序走一遍文本节点累加字符数、**遇到锚点就把计数拨到 N**
  —— 于是"屏幕上划的那几个字"能变成可持久化的坐标（`web/src/marks.ts`）。
- **公式是原子**：`\\(x\\)` 在服务端变成一整棵 `<math>` 子树，里面的文本节点与裸文本不对应。
  前端遇到 `<math>` 整棵跳过、光标落进去就**吸附到它的两端**；服务端侧与划痕相交的公式
  也整个被包进 `<mark>`（宁可多包一点，也不能把 MathML 从中间劈开）。
  两条规则互为兜底 → 界面上永远不会出现"半个公式"的划痕。
- **四支笔，不带含义**（宿主原话：「好看的几种颜色、没有含义」）：`amber/green/blue/pink`
  完全等价，**不做图例、不做语义编码**（"黄=重点"会让用户担心颜色用错了）。
- **凡是有可选文字的块都有尺子**（2026-09-16 修）：**标题与参考文献条目也算可划区域**。
  原先只有正文段/摘要走 `prose()`，标题与 `refs` 走 `_esc(text)` —— 于是它们既没有零宽锚点
  （前端量不出字符坐标）、也没有 `<mark>`（划痕渲不出来），表现是"选中标题什么都不会发生"。
  真正不可划的只有**没有可选文字**的块：公式（MathML 排版产物）、图片。
  这是**两侧各改一半**的缺陷（缺任一半都表现为"浮条不出现"）：
  ①服务端 `markup.render_block` 的标题/`refs`/`ref` 分支改走 `prose()`；
  ②前端 `Reader.tsx` 的标题分支从裸文本 `<span className="b-en">{b.en}</span>` 改成
  `<span className="b-en"><BlockBody as="span" …/></span>` —— `marks.ts::selectionSegments`
  是**从选区文本节点往上找** `.b-inline[data-lang]` 才拿到坐标系与块 id 的，找不到就整条 `segs` 为空。
- ⚠️ **标题的 HTML 外壳（`<h1..h4>`）由前端出，不由服务端出**（`render_block(wrap=False)`）：
  阅读器要自己渲 `<h2 class="doc-h lvlN" data-b=…>`（挂块 id 与字号层级），服务端若再套一层
  `<h2 class="sec">` 就得到 `<h2><h2>`，**浏览器会把内层甩到标题外面**。
  同理标题分支里**不能用 `<div>`**（`<h2>` 只允许短语内容，`div`/`p` 都会被甩出去）→ 一律用 `span`。
- **两条落笔路径**（都通）：① 工具栏先选笔 → 拖选文字 → 浮条上点颜色；② 直接拖选 → 浮条上点颜色。
  划完浮条留在原地，接着点「加笔记」时笔记就锚在这道划痕上。
- ⚠️ **「点别处收浮条」不能挂在 `click` 上**（2026-09-13 修）：拖选结束时浏览器**还会补一个
  `click`**（target = 落点所在块），若照"点别处 = 收浮条"处理，`mouseup` 刚点亮的浮条会被它当场
  收掉 —— 划过重点主路径整个不可用（表现为"浮条一闪即没"）。原型把收浮条挂在 **`mousedown`**
  （发生在选区形成**之前**）天然躲开；前端是 `click` + **"选区还活着就认定这是拖选的尾巴"** 判据。
  这个缺陷在只发合成事件（`dispatchEvent` / `el.click()`）的测法下**测不出来** —— 真实序列
  `mouseup → click` 才是关键，验收必须走真实鼠标事件。
- **点已有划痕 → 就地小菜单**：换笔 / 加笔记 / **擦掉**。擦掉只把笔记的 `hl_id` 置 NULL
  （`repo.delete_highlight`）—— 擦掉荧光笔 ≠ 撕掉批注。
- **笔记锚到区间**：表单顶行写「第 N 段（中文）· 第 a–b 字」（段号是**正文段**计数，不是块序号：
  块序号里混着标题/图表/公式，直接用会出现"第 137 段"这种对人不友好的数字）。
  卡片显示引用与**笔色圆点**（`.nc-hl.pen-<色>`，与色板共用一份颜色定义）。
- **给选区写笔记 = 顺手自动高亮**（宿主 2026-09-13：「选中添加笔记时，应该同时自动高亮」）：
  选中一段 → 浮条点「加笔记」时若这一段**还没划过**，服务端在**同一个事务**里补一道划痕
  （`repo.ensure_highlight`），颜色取**当前那支笔**；前端保存成功后重拉该块、把 `hl_id` 回填到浮条，
  toast 也明说「笔记已保存，并已高亮」。于是"划了重点却没上色"这个中间态根本不存在
  （也就没有"笔记存了、划痕丢了"的孤儿）。**整块锚 / 文献级锚不加高亮** —— 没划住的那段本来就不该上色。
  为什么 `ensure_highlight` 是"先查再建"而不是直接 `insert_highlight`：后者对**同区间**是
  "删旧插新"，划痕会换个 id，而**别的笔记还锚在旧 id 上**（当场悬空）。
- **点笔记 → 跳到那道划痕**：滚到该 `<mark>` + `.is-flash` 1.4s，卡片 `.is-on`。
  **两级落点**：划痕没了（或被擦掉、或当前语言模式下不可见）时退到整块（`data-b`）。
- **落笔后只重画受影响的那几块**：`<mark>` 是**服务端**渲染进 `en_html`/`zh_html` 的
  （㉛ 的核心不变量：排版/公式/划痕只有一处实现），前端拿 Range 自己包 DOM 会立刻与服务端分叉，
  所以宁可重拉这一块（`GET /api/docs/{pid}/blocks/{bid}`）。**重拉整篇也不行**：900 块的文档
  就是几百 KB —— 划一道重下一次纯浪费。
- **只读分享态**：划痕与笔记**能看不能改**（颜色看得见、卡片在；浮条/色板/`nc-del`/表单都不渲染）。
  写路由由后端 403 兜底（⑪）。

⚠️ 前端**不生成坐标**，只用 `data-o` 锚点换算；渲染规则一旦改，只有服务端知道新坐标。

---

## 7. 部署

**已落地（M4 ✅）**：`Dockerfile`（多阶段）+ `docker-compose.yml` + `docker/entrypoint.sh` + `docs/install.md`。
镜像由 **GitHub Actions 构建并推送 GHCR**
（`.github/workflows/docker.yml`：`test` → 双架构 `build` → `merge` 合成 manifest list → `smoke` → `verify`），
部署机只拉镜像，不需要 Node/Python 工具链。

> ⚠️ **发布流水线里禁止出现"按版本删包"的步骤**（2026-09-11 真实事故）。
> 原本末尾有个 `cleanup` 用 `actions/delete-package-versions@v5` 删"中间标签"，参数写成了
> `delete-only-untagged-versions: false` + `ignore-versions: '^(latest|v?[0-9].*)$'` + `min-versions-to-keep: 0`，
> 而保护名单**不认 `sha-<commit>` / `sha256-<hex>`** → 它把**刚发布的镜像删空了**：
> 同一次 run 里 `merge` 推上 `latest` 并 inspect 成功、`smoke` 拉 `latest` 起容器成功，
> 紧接着 `cleanup` 打印「Total versions deleted till now: 8」，此后 GHCR 上 `latest`/`sha-828114e`/manifest
> **全部 404**、包页面「No tagged versions found」、部署机 `docker pull` 只能 `not found` —— **CI 却全绿**。
> 现已删掉该 job，换成只读的 `verify`（用 `imagetools inspect` 断言 `latest` 与 `sha-<短哈希>` 在远端仍可解析），
> 并加了 `tests/test_workflow_guard.py` 把约束钉住。清理动作的失败模式是"静默删掉交付物"，
> 对交付毫无贡献，因此**直接禁止**而不是调参。


```yaml
# docker-compose.yml（简化）
services:
  papershelf:
    image: ghcr.io/argszero/papershelf:latest
    env_file: [.env]
    environment:
      PAPERSHELF_DATA_DIR: /data          # 容器内固定
    ports: ["8000:8000"]
    volumes: ["papershelf-data:/data"]    # SQLite + PDF + 图片：唯一持久化状态
    healthcheck: { test: ["CMD", "healthcheck.py"], interval: 30s }
```

落地时的几个刻意选择（写下来免得被"优化"掉）：

| 选择 | 理由 |
|---|---|
| **前端在镜像内构建**，不预打包产物 | 产物不入库，而 hatchling 打包时**不会带上被 gitignore 的文件**（实测 wheel 里只有 `favicon.svg`/`icons.svg`，没有 `index.html`）→ "宿主机 build 好再 COPY" 不成立 |
| **不引 nginx** | 静态产物由 FastAPI 同进程托管（§6 约束 1），镜像里也就没有第二套渲染器 |
| **只用单 worker**（不加 `-w`） | 转换队列是进程内的常驻线程（待定项 3），多进程会各跑各的队列；要横向扩得先换队列实现 |
| **入口脚本启动即校验配置** | `PAPERSHELF_SECRET` 缺失/太短、管理员邮箱给了但没给密码 → **直接退出**，与 `ensure_admin` 的失败哲学一致（不静默降级）；数据目录不可写直接提示 `chown 10001:10001` |
| **非 root（uid 10001）+ `/data` 卷** | 单卷备份/恢复；`docker compose exec papershelf papershelf create-admin` 是忘密码的补救路径 |
| **不用 `# syntax=docker/dockerfile:1`** | 只用经典指令；加那行会让每次构建先去 docker.io 拉 frontend 镜像（网络受限时直接失败，实测踩到） |
| **基础镜像可 `--build-arg` 覆盖** | 国内网络可换镜像源，不必改文件（`NODE_BASE` / `PY_BASE`） |

- 首次启动：`PAPERSHELF_ADMIN_EMAIL` + `PAPERSHELF_ADMIN_PASSWORD`（**必须成对**）自动成为管理员；
  亦可用 CLI `papershelf create-admin`
- **无 SMTP 也能用**：自助注册自动关闭，改由管理员在 `/admin` 开号（决策⑮）

---

## 8. 遗留待定项的当前提案（决策㉒ 要求就地给方案）

| # | 项 | 提案 |
|---|---|---|
| 1 | 会话机制 | ✅ **已落地**：服务端 `sessions` 表 + HttpOnly cookie（`sessions.token` 即凭据，签名交由 token 熵值保证，不再叠 itsdangerous）|
| 2 | 前端框架 | ✅ **已落地**：React + TypeScript + Vite，`react-router-dom` 路由。**刻意不引** axios / react-query / 状态库 —— 端点只有十来个，多一层库只会把"错误怎么呈现"藏起来（决策⑨）；错误一律 `ApiError(status, detail)` 原样上屏 |
| 3 | 任务队列 | ✅ **v1 已落地，2026-09-12 改为常驻队列**：进程内 `ConversionQueue`（常驻线程）+ DB 轮询 + 启动恢复（`doing→queued`）+ 原子认领。⚠️ 仍**只起单 worker**（见 §7），加 `-w` 会变成多份互不知情的队列 —— 要横向扩得先换队列实现 |
| 4 | 术语表归属 | **计划级**（与 ⑦ 一致）；提示：同一概念跨计划需各配一次 |
| 5 | 成本护栏 | 并发=2；`conv_attempts` ≤3（**不做单篇总 token 预算** —— 2026-09-17 宿主定；①c agent 自己的累计预算 `PROOFREAD_TOKEN_BUDGET` 保留）；超限置 failed 待人工重试。⚠️ **2026-09-15 才真正生效**：计数原先写在 `_set_state(..., "doing")` 里，而 `doing` 的转换由 `claim_paper` 做 → 那条分支从未被走到，**计数恒为 0**（护栏空转、日志「第 N 次尝试」永远显示 1）。现改为**认领即计数**（`claim_paper` 里 `+1`），且**人工入口归零**（`/convert` 重试、`/reextract`）—— 护栏防的是"自动重试烧钱"，不是防用户，否则"超限待人工重试"这句是空话 |
| 6 | 用量可见性 | v1 不做（㉑）；在 `docs`/`papers` 记录 `tokens_used` 字段**先攒数据**，v2 出面板 |
| 7 | 翻译去重 | ✅ **已落地**：`doc_cache(fingerprint = sha256(pdf) + PARSE_VERSION)` 复用**解析+LaTeX 化**结果（①c 校对结果也在里面），翻译仍按需重放。⚠️ 指纹里必须带 `PARSE_VERSION`：否则改了**解析产物形状**（例如给块加 `payload.page`）而 PDF 没变时会永远命中旧产物（2026-09-11 踩过）。⚠️ 指纹**原本没有单篇失效出口**（唯一手段是改代码里的 `PARSE_VERSION` = 全库一起失效）→ 2026-09-15 补 **`POST /api/papers/{id}/reextract`**（文献库「重新提取」按钮）：删这一篇的缓存行 + 作废它的笔记/划痕 + 重新排队；语义是**全部作废、从零重跑**（宿主选 A） |
| 8 | 公式渲染 | ✅ **已落地，且改为更省的路子**：**服务端 LaTeX → MathML**（`latex2mathml`，纯 Python）。理由见下 |
| 9 | 图表说明 | v2；届时独立决策 VLM 选型与幻觉护栏 |
| 10 | 版权警示 | ✅ 已落地且**位置已调整**（2026-09-11）：README 不再提（与开源项目本身无关）；改由**注册用户协议**约束（服务端强校验 + `users.consent_version/consent_at` 留档），运维事项写在 `docs/install.md` §9 |
| 11 | 自检失败呈现 | ✅ **全链已落地**：`conv_state=failed` + `conv_error` + `POST /papers/{id}/convert` 重试；文献表里**直接展示失败原因全文**（不是 tooltip），未配 LLM 时报"该改哪个环境变量"而不是"疑似漏译" |
| 12 | 人工修订保护 | ✅ **已落地**：转换与重译都跳过 `zh_source='human'` 的块（`converter.py` / `cli.latex` 均只清「英文真变过」的块）|
| 13 | 进度回落语义 | ✅ **已落地并简化**：滚动上报**只增不减**（回退对"读到过哪里"没有意义，且节流上报会把 100% 拽回去，实测踩到）；状态降回「在读」→ `progress_mode` 回 `auto`；标回「未读」→ 进度清零（否则出现"未读 但 80%"的自相矛盾）。**㊱（2026-09-15）补充**：滚动上报 = 此刻正在读 → 自动把「待读」翻成「在读」并记 `last_read_at`；「已读/已整理」仍只能手动（"读懂没有"只有人知道）|
| 14 | 标签词表 | v1 自由标签（⑲）；v2 可加"计划内标签自动补全"，不强制受控 |

### 公式渲染为什么最终选了 MathML（而非 KaTeX 本地化）

用真实论文（`B5-01-CBF-review`，366 条行内 + 57 条展示公式）实测的结论：

| 方案 | 实测 | 判断 |
|---|---|---|
| MathJax CDN | — | ❌ 内网/离线直接失效（决策③ 的自托管前提） |
| KaTeX 本地化 | 需随包分发 ~1MB JS + 字体 | ⚠️ 可行，但**阅读器与导出 HTML 会各用一套渲染器**，长期必然漂移 |
| **服务端 LaTeX → MathML** | **366/366 全部成功，0.09ms/条，纯 Python** | ✅ 采用 |

- 输出 **MathML Core**，Chrome 109+/Safari/Firefox 原生支持 → **前端零 JS、零字体、零构建依赖**；
- 导出单文件 HTML 自带公式语义，可直接打印、可被 Word/LaTeX 工具再加工；
- 渲染是**派生态**：`payload["latex"]` 永远是事实来源，将来换渲染器不必重跑 LLM；
- 少量 `\displaystyle`/`\label` 残留会在入库前清掉；`\tag{n}` 转成显式编号。

### 渲染与校验必须分开（`typeset` 开关）

`render_block(markup.py)` 的 `typeset` 默认 **False**（= 保留 LaTeX 源码），这是刻意的失败安全方向：

- **校验**（`validate.py`）比对的是**带标记的 LaTeX 源码**——编号就写在 `\tag{}` 里，渲染成 MathML 后就找不到了；
- **LLM prompt**（翻译 / LaTeX 化）也必须是 LaTeX 源码，喂 MathML 会让模型去猜对应关系（既烧钱又必错）；
- **给人看的入口**（`document_html` / `synth` / 导出）显式传 `typeset=True`。

---

## 9. 里程碑

| M | 内容 | 验收 | 状态 |
|---|---|---|---|
| M1 | 转换管线最小闭环：解析 → 带标记英文 HTML → 分块翻译 → 标记校验 → 块级 JSON → 合成双语 HTML | 配对 100% 正确 + 译文达到宿主现有水平 | ✅ |
| M2 | 后端 API + 认证 + 计划/文献 CRUD + 导入 + 分享 + 导出 + 管理页 | 能注册登录、建计划、导入、看列表 | ✅ |
| M3 | 阅读器（并排对照 + 块级修订 + 笔记）+ 计划页 + 分享页 + 管理页 SPA | 可实际精读一篇 | ✅ |
| M4 | Docker 化 + 部署文档 | 一条命令起服务 | ✅ |
| M5 ✅ | **复刻原型**：侧边导航壳层（计划切换器 + 进度卡 + 用户区）、总览、文献库、进度看板（后三者均为**计划内**视图，与原型的 `papers()` 取数一致）；阅读器补齐字号/大纲/对照模式；**PDF 分页容器**（每块记 `payload.page`，阅读器/分享/导出三处同版式 + 「第 N 页 / 共 M 页」页脚）；顺带把 **Admin / Share 两个漏迁移的页面**补到新设计系统（Share 页此前引用的 `.pair/.col` 类名已被删 → 渲染成无样式单列） | 真浏览器逐页对齐原型 | ✅ |
| M6 ✅ | **精读交互**：侧栏去掉独立「阅读器」条目（㉚）→ 句子级高亮与句锚笔记（㉚）→ **㉛ 换成任意字符区间**：四支**不带含义**的颜色笔 + 浮条 + 点划痕就地菜单（换色/擦掉/加笔记）+ 区间笔记 + 点笔记跳回原句 + **给选区写笔记时自动补划痕** + **旧 sid 数据启动时确定性迁移** | 真浏览器双端（本地 + 生产）逐点实测 | ✅ 2026-09-13 |

M6 的验收方式：`test_markup.py`（18 项，含公式边界与公式原子性）+ 重写的
`test_highlights.py`（区间 / 分享只读 / 旧 DDL 迁移 / **给选区写笔记自动补划痕**），离线回归 **194 项**；
真浏览器（本地 8012 + 生产 `papershelf.args.fun`）实测：拖选 → 浮条 → 选色 → 落库、
点划痕 → 换色/擦掉/加笔记、区间笔记的落点文案、点笔记 `.is-flash` 跳回、
分享态 `marks 可见 / 浮条 0 / 表单 0`、跨段选区按块拆成多道划痕、公式边界不劈开。
**鼠标交互必须用真实事件驱动**（CDP `Input.dispatchMouseEvent`）：本轮「浮条一闪即没」的缺陷
在只发合成事件时完全测不出来 —— 合成事件**跳过**了浏览器真实序列 `mouseup → click`，
而缺陷恰恰长在那个 click 上（见 §6 ㉛ 的 ⚠️）。

M3 的验收方式：用**真实论文数据**（545 块 / 184 处 LaTeX / 18 张图）在真浏览器里逐页走 ——
登录、计划、导入、并排精读（MathML 在原位渲染）、块级修订（改完立刻生效）、笔记锚定、只读分享（匿名）、管理页开号。
离线回归 `tests/` **56 项**，不调 LLM、不联网（含分页容器、无页码降级、缓存指纹三条回归）。

---

M4 的验收方式：**一条命令起服务** —— `cp .env.example .env` 填三处必填 → `docker compose up -d`
→ 浏览器登录（`docker compose logs -f` 可见入口脚本的配置校验结果）。
镜像由 CI 构建（多架构推 GHCR），部署机零工具链依赖；`docker/healthcheck.py` 打 `/api/health`。

M5 的验收方式：**真浏览器逐页对照原型截图**（`/tmp/ps-shots/proto-*.png` ↔ `/tmp/ps-m5/*.png`），
并且**每个交互都真点一遍**：看板拖拽（含"未转换不许标已读"的拦截与进度锁 100%）、
阅读器三种对照模式 × 字号 A± × 大纲跳转、计划切换器换整屏数据、开号表单、只读分享加载 545 块真实论文。
验收中修掉的**真缺陷**：Share 页引用了已删除的 `.pair/.col` 类名（渲染成无样式单列）、
移动端侧栏被藏死（原型是横置）、顶栏搜索框与 URL 不同步、计划切换器条目显示"已精读 N"而原型是百分比；
分页收尾时又揪出**懒加载图无宽高导致大纲跳转偏位约 7,500px**（与分页无关的既有缺陷，已用资产宽高占位修掉）。

_最后更新：2026-09-11_

---

## 附：认证方式变更记录（2026-09-11）

注册与找回密码**从「激活链接」改为「邮箱验证码」**，理由：**原型本来就是验证码**
（宿主指出："参考原型……原型就是验证码的方式啊"）。当时的判断失误是**先设计后看原型** ——
这一条已经作为协作纪律写进 `collab-one-question-at-a-time` 的姊妹教训：
**改动前先读原型，原型里已有答案的不要再问、也不要自己发明。**

| | 旧（已删） | 新 |
|---|---|---|
| 注册 | `POST /register` → 发激活信 → `GET /activate?token=` | `POST /register/code` → `POST /register`（**注册即登录**） |
| 找回 | 无 | `POST /reset/code` → `POST /reset`（`/forgot` 两步向导） |
| 账号状态 | `pending` 直到点链接 | 不再有 `pending`（`active` / `disabled`） |
| 库里存什么 | 明文 token | **验证码哈希** |

**防探测**：`/reset/code` 对未注册邮箱也返回成功（否则可用于批量探测哪些邮箱注册过）。

**发信可靠性**（生产实测）：部署机到 `smtp.gmail.com:465` 的 DNS 池有偏，单次连接失败率 **37.5%**，
必须重试（现 5 次 → 残留 0.7%）。详见项目记忆 `mailer-gmail-ip-rotation.md`。
